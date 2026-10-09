"""记忆生命周期维护调度仓库（04 计划 §5.3 / 阶段 1）。

四个核心方法构成一个完整的"领取 → 推进 → 收尾"循环：

* :meth:`MaintenanceRepository.claim_due_job` — 原子领取一个到期 Job。
  用 ``SELECT ... FOR UPDATE SKIP LOCKED`` 实现多 worker 互斥。
* :meth:`MaintenanceRepository.heartbeat` — 续租约。Job 跑长任务时定期调用。
* :meth:`MaintenanceRepository.complete` — 标记一次 run 成功 + 推进 cursor。
  **只在事务成功提交后才推进** ``jobs.cursor`` 与 ``next_run_at``。
* :meth:`MaintenanceRepository.fail` — 标记一次 run 失败 + 累加连续失败计数。
  失败本身不阻塞 Job；连续失败由 health.py 解读为告警。

设计取舍：

1. **所有时间戳走 ``now()``**——worker 进程时钟可能漂移，但 lease 判定
   必须以数据库时钟为准。``claim_due_job`` / ``heartbeat`` / ``complete`` /
   ``fail`` 写入 ``lease_expires_at`` / ``next_run_at`` / ``started_at`` 时
   全部走 SQL ``now()``，不接 worker 传的时间参数。
2. **``cursor`` 透传**——repository 不解析 cursor 形态，只在 ``complete``
   事务里把 JobResult.next_cursor 推进到 ``jobs.cursor``。Job 自己负责
   序列化成稳定字符串（``str(None)`` / 字典的 JSON 形式均可）。
3. **claim 与 run 一起开启**——``claim_due_job`` 不光"占住"Job，还同时
   INSERT 一行 ``runs`` 状态为 ``running`` 的账本；心跳与收尾直接更新
   这一行。**禁止**没有 run 行的"幽灵 lease"。
4. **唯一 running run 约束**——``memory_maintenance_runs`` 上的
   ``memory_maintenance_runs_one_running_idx`` partial unique 防止历史
   bug 让两个 running 共存；claim 时拿不到锁会被 PG 直接抛
   UniqueViolationError，repository 转成 ``ClaimRace`` 返回上层重试。
5. **连接管理**——直接用传入的 ``MemoryDatabase`` 池。维护表所在的
   ``memory_maintenance`` schema 与业务表不在同一 schema，访问由
   ``0006_memory_maintenance.sql`` 显式 ``GRANT`` 决定（klonet_maint 写、
   klonet_app/klonet_ops 读）；本仓库不**自己**做角色切换——这是
   部署层的事，不是仓库层的事。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.maintenance.base import JobResult

__all__ = [
    "ClaimRace",
    "JobClaim",
    "MaintenanceRepository",
]


class ClaimRace(RuntimeError):
    """``claim_due_job`` 在并发竞态中失败（同一 job 被另一个 worker 抢走）。

    这是**预期内**的失败：两个 worker 同时 ``SELECT ... FOR UPDATE SKIP LOCKED``
    只能有一个拿到行，另一个抛此错。scheduler 应回到轮询，而不是把 worker
    整体停掉。
    """


@dataclass(frozen=True)
class JobClaim:
    """一次成功 ``claim_due_job`` 的结果。"""

    job_name: str
    run_id: str
    worker_id: str
    started_at: datetime
    deadline: datetime
    cursor: str | None
    config: Mapping[str, Any]
    batch_limit: int

    def to_job_context(
        self,
        *,
        run_id: str,
        worker_id: str,
        deadline_seconds: float,
    ) -> "JobContext":  # type: ignore[name-defined]  # noqa: F821
        """转成 ``MaintenanceJob.run`` 接受的 ``JobContext``。

        ``batch_limit`` 从 ``config["batch_limit"]`` 读（默认 100），死线
        由 caller 决定（典型为 ``lease_seconds``，保证 lease 过期前
        必须交回）。
        """

        # 避免循环 import：JobContext 在 maintenance.base 里。
        from klonet_agent.memory.maintenance.base import JobContext

        batch_limit = int(self.config.get("batch_limit", 100))
        return JobContext(
            job_name=self.job_name,
            run_id=run_id,
            worker_id=worker_id,
            started_at=self.started_at,
            deadline=self.deadline,
            batch_limit=batch_limit,
            dry_run=bool(self.config.get("dry_run", False)),
        )


class MaintenanceRepository:
    """维护调度与 run 账本的事务化访问入口。"""

    def __init__(self, database: MemoryDatabase):
        self._database = database

    # ---------------------------------------------------------------- 领取 --

    def claim_due_job(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> JobClaim | None:
        """原子领取一个到期 Job；无 due job 返回 ``None``。

        事务里做四件事：

        1. ``SELECT ... FOR UPDATE SKIP LOCKED`` 选出一行 enabled +
           ``next_run_at <= now()`` 的 job；并发场景下两个 worker 只
           有一个拿到这行，另一个转 ``None``。
        2. 检查 ``lease_expires_at``——如果非空且未过期，跳过（被别人占住）。
           锁的"SKIP LOCKED"已经做了这层保护，但显式再查一次是
           防御性"测试断言一定能复现"的写法。
        3. 推进 ``lease_owner`` / ``lease_expires_at`` / ``last_started_at``。
        4. INSERT 一行 ``runs`` 状态为 ``running`` 的账本；如果该 job 已有
           一个未结束的 running，partial unique index 抛 UniqueViolationError，
           repository 转成 :class:`ClaimRace`。
        """

        if not worker_id:
            raise ValueError("worker_id 不能为空")
        if lease_seconds <= 0:
            raise ValueError(f"lease_seconds 必须为正数，实际 {lease_seconds}")

        lease_seconds_int = int(lease_seconds)
        run_id = str(uuid.uuid4())

        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                # 1) 选到期行。
                row = conn.execute(
                    """
                    SELECT job_name, cursor, config, last_started_at
                    FROM memory_maintenance.memory_maintenance_jobs
                    WHERE enabled
                      AND next_run_at <= now()
                      AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                    ORDER BY next_run_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                ).fetchone()
                if row is None:
                    return None

                # 2) INSERT running run。partial unique index 会拦住重复 claim。
                try:
                    conn.execute(
                        """
                        INSERT INTO memory_maintenance.memory_maintenance_runs
                            (run_id, job_name, worker_id, status, started_at)
                        VALUES (%s, %s, %s, 'running', now())
                        """,
                        (run_id, row["job_name"], worker_id),
                    )
                except Exception as exc:  # psycopg UniqueViolationError 之类
                    # 消息形如 'duplicate key value violates unique constraint'。
                    msg = str(exc)
                    if (
                        "memory_maintenance_runs_one_running_idx" in msg
                        or "duplicate key" in msg
                    ):
                        raise ClaimRace(
                            f"job={row['job_name']} 已有 running run，被并发抢走"
                        ) from exc
                    raise

                # 3) 更新 lease / last_started_at。
                conn.execute(
                    """
                    UPDATE memory_maintenance.memory_maintenance_jobs
                    SET lease_owner = %s,
                        lease_expires_at = now() + (%s || ' seconds')::interval,
                        last_started_at = now()
                    WHERE job_name = %s
                    """,
                    (worker_id, str(lease_seconds_int), row["job_name"]),
                )

                # 4) 读回最终形态（拿 started_at 用 PG 时钟的准确值）。
                result = conn.execute(
                    """
                    SELECT last_started_at, lease_expires_at, config
                    FROM memory_maintenance.memory_maintenance_jobs
                    WHERE job_name = %s
                    """,
                    (row["job_name"],),
                ).fetchone()

                started_at = self._as_utc(result["last_started_at"])
                lease_expires_at = self._as_utc(result["lease_expires_at"])
                config = self._coerce_config(result["config"])
                cursor = self._coerce_cursor(row["cursor"])

        return JobClaim(
            job_name=row["job_name"],
            run_id=run_id,
            worker_id=worker_id,
            started_at=started_at,
            deadline=lease_expires_at,
            cursor=cursor,
            config=config,
            batch_limit=int(config.get("batch_limit", 100)),
        )

    # ---------------------------------------------------------------- 续租 --

    def heartbeat(
        self,
        *,
        job_name: str,
        worker_id: str,
        lease_seconds: float,
    ) -> bool:
        """续租约。返回 ``True`` 表示成功；``False`` 表示该 lease 不属于
        本 worker（已过期被抢走或 job_name 不存在）。

        长任务定期调一次——``lease_seconds`` 必须显著小于"已耗时 + 剩余预算"，
        否则 lease 会在事务未完成时过期、另一个 worker 把同一 job 领走。
        """

        if lease_seconds <= 0:
            raise ValueError(f"lease_seconds 必须为正数，实际 {lease_seconds}")

        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE memory_maintenance.memory_maintenance_jobs
                    SET lease_expires_at = now() + (%s || ' seconds')::interval
                    WHERE job_name = %s
                      AND lease_owner = %s
                      AND lease_expires_at > now()
                    RETURNING job_name
                    """,
                    (str(int(lease_seconds)), job_name, worker_id),
                ).fetchone()
                return row is not None

    # ---------------------------------------------------------------- 收尾 --

    def complete(
        self,
        *,
        job_name: str,
        run_id: str,
        result: JobResult,
        next_run_seconds: float,
    ) -> None:
        """把 ``runs`` 行标为 succeeded，推进 ``jobs.cursor`` 与 ``next_run_at``。

        **cursor 只在本事务内、连同 runs.status='succeeded' 一起推进**——
        如果 ``runs`` 还没写成功就冒泡，``jobs.cursor`` 不会被更新，下次
        claim 时仍从旧位置开始；这是 04 计划 §5.3 "cursor 只在成功完成后
        推进"的契约。

        ``consecutive_failures`` 在 succeeded 时归零。
        """

        if next_run_seconds <= 0:
            raise ValueError(f"next_run_seconds 必须为正数，实际 {next_run_seconds}")

        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                # 1) 把 runs 写为 succeeded。
                run_row = conn.execute(
                    """
                    UPDATE memory_maintenance.memory_maintenance_runs
                    SET status = 'succeeded',
                        finished_at = now(),
                        scanned = %s,
                        changed = %s,
                        proposed = %s,
                        failed = %s,
                        details = %s::jsonb
                    WHERE run_id = %s
                      AND job_name = %s
                      AND status = 'running'
                    RETURNING run_id
                    """,
                    (
                        result.scanned,
                        result.changed,
                        result.proposed,
                        result.failed,
                        json.dumps({"lines": list(result.details)}, ensure_ascii=False),
                        run_id,
                        job_name,
                    ),
                ).fetchone()
                if run_row is None:
                    raise RuntimeError(
                        f"无法完成 run: job={job_name} run_id={run_id} 不是 running 状态"
                    )

                # 2) 推进 jobs.cursor + next_run_at + 清 lease + 归零失败计数。
                conn.execute(
                    """
                    UPDATE memory_maintenance.memory_maintenance_jobs
                    SET cursor = %s::jsonb,
                        next_run_at = now() + (%s || ' seconds')::interval,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_succeeded_at = now(),
                        consecutive_failures = 0
                    WHERE job_name = %s
                    """,
                    (
                        self._dump_cursor(result.next_cursor),
                        str(int(next_run_seconds)),
                        job_name,
                    ),
                )

    def fail(
        self,
        *,
        job_name: str,
        run_id: str,
        error_code: str,
        error_summary: str,
        next_run_seconds: float,
    ) -> None:
        """把 ``runs`` 行标为 failed，累加 ``jobs.consecutive_failures``。

        与 ``complete`` 的差别：``consecutive_failures += 1`` 而非归零；
        ``error_code`` 与 ``error_summary`` 写进 ``runs``；**不**推进
        ``jobs.cursor``（失败不能假装"已经走过"）。
        """

        if not error_code:
            raise ValueError("error_code 不能为空")
        if not error_summary:
            raise ValueError("error_summary 不能为空")
        if len(error_summary) > 1000:
            # 与 0006 的 CHECK 约束一致；不要让 schema 抛错才被发现。
            error_summary = error_summary[:1000]
        if next_run_seconds <= 0:
            raise ValueError(f"next_run_seconds 必须为正数，实际 {next_run_seconds}")

        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                run_row = conn.execute(
                    """
                    UPDATE memory_maintenance.memory_maintenance_runs
                    SET status = 'failed',
                        finished_at = now(),
                        error_code = %s,
                        error_summary = %s
                    WHERE run_id = %s
                      AND job_name = %s
                      AND status = 'running'
                    RETURNING run_id
                    """,
                    (error_code, error_summary, run_id, job_name),
                ).fetchone()
                if run_row is None:
                    raise RuntimeError(
                        f"无法失败 run: job={job_name} run_id={run_id} 不是 running 状态"
                    )

                conn.execute(
                    """
                    UPDATE memory_maintenance.memory_maintenance_jobs
                    SET next_run_at = now() + (%s || ' seconds')::interval,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        consecutive_failures = consecutive_failures + 1
                    WHERE job_name = %s
                    """,
                    (str(int(next_run_seconds)), job_name),
                )

    # -------------------------------------------------------------- helpers --

    @staticmethod
    def _as_utc(value: Any) -> datetime:
        if value is None:
            raise RuntimeError("PG 返回了 NULL 时间戳，schema 可能被改过")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        raise TypeError(f"无法把 {type(value).__name__} 解析为 datetime")

    @staticmethod
    def _coerce_config(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, (bytes, bytearray)):
            return json.loads(value.decode("utf-8"))
        if isinstance(value, str):
            return json.loads(value)
        raise TypeError(f"无法把 {type(value).__name__} 解析为 config dict")

    @staticmethod
    def _coerce_cursor(value: Any) -> str | None:
        """cursor 形态由 Job 自己定义；本仓库不解析，只把它稳住成字符串。"""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8")
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _dump_cursor(value: str | None) -> str:
        """把 JobResult.next_cursor 写回 ``jobs.cursor`` (jsonb) 的形态。

        多数 Job 的 cursor 是 ``"{\"profile\":\"default\",\"id\":\"...\"}"``
        这种 JSON 串——直接当 jsonb 存。空 cursor 写 ``NULL`` 而非 ``"null"``。
        """

        if value is None or value == "":
            return "null"  # SQL NULL 由调用方理解；这里存"无 cursor"的显式标记
        # 如果能解析成 JSON 就当 JSON 存，否则当字符串包成 JSON。
        try:
            json.loads(value)
            return value
        except (TypeError, ValueError):
            return json.dumps(value, ensure_ascii=False)