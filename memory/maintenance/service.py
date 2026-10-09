"""记忆生命周期维护 Worker 主循环（04 计划 §8 / 阶段 2）。

**这个进程与 Agent 对话进程完全分离**：它不 import ``orchestrator`` / ``session`` /
``agent`` 的任何东西，只依赖 ``MaintenanceRepository`` 与一组 ``MaintenanceJob``。
反向约束（主链路不得 import 本包）由 ``scripts/check_maintenance_isolation.py``
与 ``tests/test_maintenance_isolation.py`` 在 CI 层把住。

主循环（``run_forever``）：

```
while not stop_requested:
    claim = repository.claim_due_job(worker_id, lease_seconds)
    if claim 为空：  sleep(poll_seconds)；continue
    job = registry[claim.job_name]
    result = job.run(context, claim.cursor)   # 单 Job 失败不冒泡
    repository.complete(...) / repository.fail(...)
```

四个刻意的取舍：

1. **领取与执行解耦**——``_tick()`` 只负责一次"领一个、跑一个、收尾"。
   ``run_forever`` 关心停止与空闲退避，``run_once`` 关心"跑一次就返回"。
   测试可以只测 ``_tick``，不必启动线程。
2. **单 Job 异常不冒泡**——Job 抛异常时 service 记 ``runs.status='failed'`` +
   累加 ``jobs.consecutive_failures``，然后继续下一轮。这是 04 计划
   §2.1 第 7 条"失败可见"的可执行形式：**不能用吞异常维持绿色**，
   失败必须写进 job run。
3. **停止是"事件"不是"信号"**——``request_stop()`` 只置一个
   ``threading.Event``；信号处理器（``install_signal_handlers``）调它，
   但绝不在处理器里直接碰数据库。主循环在**领取下一批之前**检查该事件，
   在跑完当前 Job 之后才退出。这就是"必须跑完当前事务"的实现。
4. **未知 Job 名**——registry 在**注册阶段**拒绝；DB 里若出现未注册的
   job 名字（例如运维手工 INSERT），``_execute`` 记 ``unknown_job`` 失败
   并释放租约，而不是静默跳过。

不依赖具体 DB：``repository`` 只需鸭子类型具备 ``claim_due_job`` /
``heartbeat`` / ``complete`` / ``fail`` 四个方法。测试用 fake repository
即可测全部循环语义。
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.maintenance.base import JobContext, JobResult, MaintenanceJob

__all__ = [
    "KNOWN_JOB_NAMES",
    "MaintenanceService",
    "ServiceStatus",
    "UnknownJobError",
    "resolve_job_name",
]


# 0006_memory_maintenance.sql 里握手 INSERT 的六个名字。registry 只接受这些
# （阶段 3–7 会为它们各自补上真正的 Job 实现）。
KNOWN_JOB_NAMES: tuple[str, ...] = (
    "embedding_outbox",
    "expiration",
    "purge",
    "consolidation",
    "reembedding",
    "health_report",
)

# CLI / 运维脚本常用的短名映射到规范名（计划 §8.1 写的是 ``--job embedding``）。
JOB_ALIASES: Mapping[str, str] = {
    "embedding": "embedding_outbox",
    "embed": "embedding_outbox",
    "outbox": "embedding_outbox",
    "expire": "expiration",
    "expiry": "expiration",
    "cleanup": "purge",
    "consolidate": "consolidation",
    "reembed": "reembedding",
    "health": "health_report",
    "health_report": "health_report",
}


class UnknownJobError(ValueError):
    """注册了不在 ``KNOWN_JOB_NAMES`` 里的 Job 名，或引用了未知 Job。

    **启动阶段就报错**，不是等运行时才发现——一个名字拼错的 Job 注册进去
    只会永远跑不到，而"跑不到"在日志里看起来跟"还没到调度时间"一样。
    """


def resolve_job_name(name: str) -> str:
    """把短名 / 别名解析成规范 Job 名；未知名字抛 ``UnknownJobError``。"""

    candidate = str(name or "").strip()
    if not candidate:
        raise UnknownJobError("job name 不能为空")
    canonical = JOB_ALIASES.get(candidate, candidate)
    if canonical not in KNOWN_JOB_NAMES:
        raise UnknownJobError(
            f"未知 job name: {name!r}（可选：{', '.join(KNOWN_JOB_NAMES)}）"
        )
    return canonical


@dataclass
class ServiceStatus:
    """一次 ``status`` 查询的可观测快照。"""

    worker_id: str
    enabled: bool
    registered_jobs: tuple[str, ...]
    ticks: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    idle_sleeps: int = 0
    stopping: bool = False
    last_error: str | None = None
    last_job: str | None = None
    started_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "enabled": self.enabled,
            "registered_jobs": list(self.registered_jobs),
            "ticks": self.ticks,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "idle_sleeps": self.idle_sleeps,
            "stopping": self.stopping,
            "last_error": self.last_error,
            "last_job": self.last_job,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


def _default_worker_id() -> str:
    host = socket.gethostname() or "unknown-host"
    return f"{host}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class MaintenanceService:
    """常驻 Worker 主循环。"""

    def __init__(
        self,
        repository: Any,
        config: MaintenanceConfig,
        *,
        jobs: Iterable[MaintenanceJob] | None = None,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        wait: Callable[[float], Any] | None = None,
        logger: logging.Logger | None = None,
    ):
        self._repository = repository
        self._config = config
        self._worker_id = worker_id or _default_worker_id()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._logger = logger or logging.getLogger("klonet.memory.maintenance")
        self._jobs: dict[str, MaintenanceJob] = {}
        self._stop_event = threading.Event()
        # 空闲等待：默认用 ``Event.wait``，SIGTERM 到达时可提前醒来（不必等满
        # 一个 poll 周期）。测试注入一个记录器即可观察"空转了多久"。
        self._wait = wait or self._stop_event.wait
        self._status = ServiceStatus(
            worker_id=self._worker_id,
            enabled=config.enabled,
            registered_jobs=(),
            started_at=None,
        )
        for job in jobs or ():
            self.register(job)

    # ------------------------------------------------------------- 注册 --

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def config(self) -> MaintenanceConfig:
        return self._config

    @property
    def repository(self) -> Any:
        return self._repository

    @property
    def jobs(self) -> Mapping[str, MaintenanceJob]:
        return dict(self._jobs)

    def register(self, job: MaintenanceJob) -> None:
        """注册一个 Job。未知名字 / 重名都直接报错。"""

        name = getattr(job, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise UnknownJobError(f"Job 缺少合法的 name 属性：{job!r}")
        canonical = resolve_job_name(name)
        if canonical != name:
            raise UnknownJobError(
                f"Job 名字必须是规范名 {canonical!r}，不能是别名 {name!r}"
            )
        if canonical in self._jobs:
            raise UnknownJobError(f"Job {canonical!r} 重复注册")
        if not callable(getattr(job, "run", None)):
            raise UnknownJobError(f"Job {canonical!r} 缺少可调用的 run()")
        self._jobs[canonical] = job
        self._status.registered_jobs = tuple(sorted(self._jobs))

    # ------------------------------------------------------------- 停止 --

    def request_stop(self) -> None:
        """请求停止：置事件；主循环在下一个检查点退出。

        刻意**不**直接终止正在跑的 Job——"允许当前事务在 grace period 内完成"
        是 04 计划 §8.2 的硬要求。
        """

        self._stop_event.set()
        self._status.stopping = True

    def install_signal_handlers(self) -> None:
        """安装 SIGTERM/SIGINT 处理器 → ``request_stop``。

        处理器里**只做一件事**（置事件），不碰数据库、不写日志以外的 I/O——
        在信号处理器里提交事务是 SIGTERM 场景下最常见的死锁来源。
        """

        def _handler(signum: int, _frame: Any) -> None:  # pragma: no cover - 信号
            self._logger.info("收到信号 %s，请求停止（等当前 Job 完成）", signum)
            self.request_stop()

        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handler)
                except (ValueError, OSError):  # pragma: no cover - 非主线程
                    # 非主线程安装信号处理器会 ValueError；测试里常见。
                    pass

    def status(self) -> ServiceStatus:
        return self._status

    # ------------------------------------------------------------- 主循环 --

    def run_forever(self, *, max_ticks: int | None = None) -> int:
        """常驻循环。返回本次运行的 tick 数（便于测试断言）。

        ``max_ticks`` 只给测试用——生产不设上限，靠 ``request_stop()`` 退出。
        """

        self._status.started_at = self._clock()
        self._logger.info(
            "维护 Worker 启动 worker_id=%s jobs=%s enabled=%s",
            self._worker_id,
            ",".join(self._status.registered_jobs) or "(none)",
            self._config.enabled,
        )
        ticks = 0
        while not self._stop_event.is_set():
            if max_ticks is not None and ticks >= max_ticks:
                break
            ticks += 1
            did_work = self._tick()
            if self._stop_event.is_set():
                # stop 可能在 _tick 的内部（Job 运行期间）被置上——
                # 收尾已经完成，直接退出，不再领新 Job。
                break
            if not did_work:
                self._status.idle_sleeps += 1
                self._sleep_interruptible(self._config.poll_seconds)
        self._logger.info(
            "维护 Worker 停止 worker_id=%s ticks=%s claimed=%s succeeded=%s failed=%s",
            self._worker_id,
            self._status.ticks,
            self._status.claimed,
            self._status.succeeded,
            self._status.failed,
        )
        return ticks

    def run_once(
        self,
        *,
        job_name: str | None = None,
        force: bool = False,
        max_runs: int = 1,
    ) -> list[JobResult]:
        """跑一次（运维 / 测试）。

        * ``job_name`` 指定时只领该 Job（用 :func:`resolve_job_name` 解析别名）；
        * ``force=True`` 跳过 ``enabled`` 与调度时间，但仍尊重 lease；
        * 不带 ``job_name`` 时循环领 due job，直到无 due 或达到 ``max_runs``。

        返回累计的 ``JobResult`` 列表（无 due 时为空列表）。
        """

        results: list[JobResult] = []
        canonical = resolve_job_name(job_name) if job_name is not None else None
        if canonical is not None:
            self._require_registered(canonical)
        limit = max_runs if canonical is None else 1
        attempts = 0
        # 上限必须数**领取次数**而不是成功数：持续失败的 Job 永远凑不满
        # "1 个成功"，``while len(results) < limit`` 会变成无限热循环
        # （阶段 7 真库实测：签名不匹配的 Job 以 ~60 次/秒的速度刷
        # claim/fail，consecutive_failures 冲到 7 万+）。
        while attempts < limit:
            claim = self._claim(canonical, force=force)
            if claim is None:
                break
            attempts += 1
            result = self._execute(claim)
            if result is not None:
                results.append(result)
            if self._stop_event.is_set():
                break
        return results

    # ------------------------------------------------------------- 内部 --

    def _require_registered(self, job_name: str) -> None:
        if job_name not in self._jobs:
            raise UnknownJobError(
                f"Job {job_name!r} 未注册（已注册：{', '.join(sorted(self._jobs)) or '(none)'}）"
            )

    def _sleep_interruptible(self, seconds: float) -> None:
        """可被 ``request_stop`` 提前唤醒的等待。

        默认实现是 ``threading.Event.wait(timeout)``（不同于 ``time.sleep``）：
        SIGTERM 到达时不必等满一个 poll 周期才退出。测试可注入一个记录器
        观察等待时长。
        """

        self._wait(max(0.0, float(seconds)))

    def _claim(self, job_name: str | None, *, force: bool) -> Any:
        try:
            return self._repository.claim_due_job(
                worker_id=self._worker_id,
                lease_seconds=self._config.lease_seconds,
                job_name=job_name,
                force=force,
            )
        except Exception as exc:  # noqa: BLE001 - ClaimRace 与连接错误都在这里
            self._status.last_error = f"{type(exc).__name__}: {exc}"
            self._logger.warning("claim 失败（将重试）：%s", self._status.last_error)
            return None

    def _tick(self) -> bool:
        self._status.ticks += 1
        claim = self._claim(None, force=False)
        if claim is None:
            return False
        self._execute(claim)
        return True

    def _execute(self, claim: Any) -> JobResult | None:
        """跑一个 claim，收尾写 complete/fail。返回 JobResult（失败时 None）。"""

        self._status.claimed += 1
        self._status.last_job = getattr(claim, "job_name", None)
        job_name = getattr(claim, "job_name", None)

        job = self._jobs.get(job_name)
        interval = self._config.job_interval_seconds(job_name or "")

        if job is None:
            # registry 拒了未知名字，但 DB 里可能被运维手工加了新行。
            self._fail(
                claim,
                error_code="unknown_job",
                error_summary=f"DB 中有未注册的 job: {job_name}",
                next_run_seconds=interval,
            )
            return None

        batch_limit = int(getattr(claim, "batch_limit", 0) or 0)
        if batch_limit <= 0:
            batch_limit = self._config.job_batch_size(job_name)
        dry_run = self._config.dry_run or bool(
            (getattr(claim, "config", None) or {}).get("dry_run", False)
        )
        context = JobContext(
            job_name=job_name,
            run_id=claim.run_id,
            worker_id=self._worker_id,
            started_at=claim.started_at,
            deadline=claim.deadline,
            batch_limit=batch_limit,
            dry_run=dry_run,
        )
        try:
            result = job.run(context, claim.cursor)
        except Exception as exc:  # noqa: BLE001 - 单 Job 失败不拖垮 Worker
            self._fail(
                claim,
                error_code=type(exc).__name__,
                error_summary=str(exc)[:1000] or type(exc).__name__,
                next_run_seconds=interval,
            )
            return None
        if not isinstance(result, JobResult):
            self._fail(
                claim,
                error_code="bad_job_result",
                error_summary=f"{type(result).__name__} 不是 JobResult",
                next_run_seconds=interval,
            )
            return None

        try:
            self._repository.complete(
                job_name=job_name,
                run_id=claim.run_id,
                result=result,
                next_run_seconds=interval,
            )
        except Exception as exc:  # noqa: BLE001 - 收尾失败也要可见
            self._status.failed += 1
            self._status.last_error = f"complete 失败：{type(exc).__name__}: {exc}"
            self._logger.error("%s", self._status.last_error)
            return None
        self._status.succeeded += 1
        return result

    def _fail(
        self,
        claim: Any,
        *,
        error_code: str,
        error_summary: str,
        next_run_seconds: float,
    ) -> None:
        self._status.failed += 1
        self._status.last_error = f"{error_code}: {error_summary}"
        try:
            self._repository.fail(
                job_name=claim.job_name,
                run_id=claim.run_id,
                error_code=error_code,
                error_summary=error_summary,
                next_run_seconds=next_run_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 连 fail 都写不进去时只记日志
            self._logger.error("标记 run 失败也失败：%s", exc)
