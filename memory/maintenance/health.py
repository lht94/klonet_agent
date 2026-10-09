"""记忆生命周期 Worker 健康快照（04 计划 §6.6 / 阶段 7 完整实现）。

数据源（**不**新开表——03 阶段 7 的"单一权威"约束）：

- ``memory_maintenance_jobs`` / ``memory_maintenance_runs``：每 Job 的
  last_success / consecutive_failures / 最近一次成功 run 的耗时；
- ``memory_embedding_outbox``：各 profile 的 pending / failed（= abandoned
  累计）/ coverage / 最老 pending 年龄；
- ``memory_records``：expired-but-active 漂移数、待 purge 的 deleted 数；
- ``memory_maintenance.memory_maintenance_proposals``：pending 提案按类型；
- 迁移账本：非终态 reembedding 迁移的 progress ratio。

指标上报走 ``runtime.governance.service.record_run_metric``（追加进
``governance.runtime_events``）——那是唯一权威通道。

健康判定（§6.6 告警建议 + 阶段 0 约定）：

- 任一 Job ``consecutive_failures >= 3`` → 不健康；
- embedding 最老 pending 年龄 ``>= 900`` 秒 → 不健康（积压无人消费）；
- DSN 配置了但库打不开 → 不健康（阶段 7 起真实故障必须阻断 cutover）；
- DSN 没配置 → ``healthy=True`` 的**过渡语义**（模块在但 Worker 未部署，
  与阶段 0–6 的放行口径一致）。

函数**不抛异常**——健康快照采集失败时返回 ``{"healthy": False, ...}``，
切到 cutover 的路径不能因读取健康状态本身而炸。**不输出**正文 / token /
连接密码——错误只留"类型 + 消息"。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

__all__ = [
    "collect_snapshot",
    "flatten_metrics",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_PENDING_AGE_SECONDS",
]

#: 任一 Job 连续失败达到该值即判不健康（§6.6 告警建议）。
MAX_CONSECUTIVE_FAILURES = 3

#: embedding 最老 pending 任务的可容忍年龄（秒）。
MAX_PENDING_AGE_SECONDS = 900.0

_DSN_ENV = "KLONET_AGENT_MEMORY_DSN"


def collect_snapshot(*, database: Any = None, now: datetime | None = None) -> Mapping[str, Any]:
    """返回当前 Worker 的健康快照；键名 = §6.6 的最终 Prometheus 指标名。

    ``database`` 缺省时从环境解析 ``MemoryDatabase``（无 DSN → 过渡语义
    ``healthy=True``；DSN 在但库不可用 → ``healthy=False``）。
    """

    moment = now or datetime.now(timezone.utc)
    owned: Any = None
    try:
        if database is None:
            dsn = (os.environ.get(_DSN_ENV) or "").strip()
            if not dsn:
                return _transition_snapshot(moment)
            from klonet_agent.memory.database import MemoryDatabase

            owned = MemoryDatabase.from_env()
            owned.open()
            database = owned
        return _collect(database, moment)
    except Exception as exc:  # noqa: BLE001 - 健康采集失败必须 fail closed 且不炸调用方
        return {
            "healthy": False,
            "reason": f"健康快照采集失败：{type(exc).__name__}: {exc}",
            "captured_at": moment.isoformat(),
            "memory_maintenance_last_success_timestamp": {},
            "memory_maintenance_run_duration_seconds": {},
            "memory_maintenance_consecutive_failures": {},
            "memory_embedding_pending_total": {},
            "memory_embedding_abandoned_total": {},
            "memory_embedding_coverage_ratio": {},
            "memory_expired_active_total": 0,
            "memory_deleted_waiting_purge_total": 0,
            "memory_proposals_pending_total": {},
            "memory_reembedding_progress_ratio": {},
        }
    finally:
        if owned is not None:
            try:
                owned.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响快照
                pass


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _transition_snapshot(moment: datetime) -> Mapping[str, Any]:
    """未配置 DSN 的过渡语义：模块在、Worker 未部署，不阻断 cutover。"""

    return {
        "healthy": True,
        "reason": "Worker 未配置（无 KLONET_AGENT_MEMORY_DSN）——过渡语义，不阻断",
        "captured_at": moment.isoformat(),
        "memory_maintenance_last_success_timestamp": {},
        "memory_maintenance_run_duration_seconds": {},
        "memory_maintenance_consecutive_failures": {},
        "memory_embedding_pending_total": {},
        "memory_embedding_abandoned_total": {},
        "memory_embedding_coverage_ratio": {},
        "memory_expired_active_total": 0,
        "memory_deleted_waiting_purge_total": 0,
        "memory_proposals_pending_total": {},
        "memory_reembedding_progress_ratio": {},
    }


def _collect(database: Any, moment: datetime) -> Mapping[str, Any]:
    pool = database._require_pool()
    with pool.connection() as conn:
        jobs = {
            str(row["job_name"]): row
            for row in conn.execute(
                """
                SELECT job_name, enabled, consecutive_failures, last_succeeded_at
                  FROM memory_maintenance.memory_maintenance_jobs
                """
            ).fetchall()
        }
        durations: dict[str, float] = {}
        for row in conn.execute(
            """
            SELECT DISTINCT ON (job_name)
                   job_name,
                   EXTRACT(EPOCH FROM (finished_at - started_at)) AS seconds
              FROM memory_maintenance.memory_maintenance_runs
             WHERE status = 'succeeded' AND finished_at IS NOT NULL
             ORDER BY job_name, started_at DESC
            """
        ).fetchall():
            durations[str(row["job_name"])] = round(float(row["seconds"] or 0.0), 3)

        pending: dict[str, int] = {}
        abandoned: dict[str, int] = {}
        coverage: dict[str, float] = {}
        oldest_pending_at: datetime | None = None
        for row in conn.execute(
            """
            SELECT embedding_profile_id AS profile, status, count(*) AS n,
                   min(created_at) AS oldest
              FROM memory_embedding_outbox
             GROUP BY 1, 2
            """
        ).fetchall():
            profile = str(row["profile"])
            n = int(row["n"])
            if str(row["status"]) == "pending":
                pending[profile] = pending.get(profile, 0) + n
                oldest = row["oldest"]
                if isinstance(oldest, datetime) and (
                    oldest_pending_at is None or oldest < oldest_pending_at
                ):
                    oldest_pending_at = oldest
            elif str(row["status"]) == "failed":
                abandoned[profile] = abandoned.get(profile, 0) + n

        # coverage 分母含全部状态（completed / 全部行）。
        for row in conn.execute(
            """
            SELECT embedding_profile_id AS profile,
                   count(*) FILTER (WHERE status = 'completed') AS done,
                   count(*) AS total
              FROM memory_embedding_outbox
             GROUP BY 1
            """
        ).fetchall():
            profile = str(row["profile"])
            total = int(row["total"])
            coverage[profile] = round(int(row["done"]) / total, 6) if total else 1.0

        expired_active = int(
            conn.execute(
                """
                SELECT count(*) AS n
                  FROM memory_records r
                  JOIN memory_versions v ON v.id = r.active_version_id
                 WHERE r.status = 'active' AND v.valid_to IS NOT NULL
                   AND v.valid_to <= now()
                """
            ).fetchone()["n"]
        )
        deleted_waiting = int(
            conn.execute(
                "SELECT count(*) AS n FROM memory_records WHERE status = 'deleted'"
            ).fetchone()["n"]
        )
        proposals: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT proposal_type, count(*) AS n
              FROM memory_maintenance.memory_maintenance_proposals
             WHERE status = 'pending'
             GROUP BY 1
            """
        ).fetchall():
            proposals[str(row["proposal_type"])] = int(row["n"])
        migration_row = conn.execute(
            """
            SELECT migration_id, status, target_profile_id
              FROM memory_maintenance.embedding_migrations
             WHERE status NOT IN ('retired', 'cancelled')
             ORDER BY created_at DESC
             LIMIT 1
            """
        ).fetchone()

    last_success = {
        job: (
            row["last_succeeded_at"].isoformat()
            if row["last_succeeded_at"] is not None
            else None
        )
        for job, row in jobs.items()
    }
    failures = {job: int(row["consecutive_failures"]) for job, row in jobs.items()}

    reembedding_progress: dict[str, float] = {}
    if migration_row is not None:
        completed, eligible = _migration_progress(
            database, str(migration_row["target_profile_id"])
        )
        reembedding_progress[str(migration_row["migration_id"])] = (
            round(completed / eligible, 6) if eligible else 0.0
        )

    # ---- 健康判定 ----
    reasons: list[str] = []
    hot_jobs = sorted(
        job for job, count in failures.items() if count >= MAX_CONSECUTIVE_FAILURES
    )
    if hot_jobs:
        reasons.append(
            f"连续失败 ≥ {MAX_CONSECUTIVE_FAILURES} 的 Job：{','.join(hot_jobs)}"
        )
    oldest_age: float | None = None
    if oldest_pending_at is not None:
        oldest_age = max(0.0, (moment - oldest_pending_at).total_seconds())
        if oldest_age >= MAX_PENDING_AGE_SECONDS:
            reasons.append(
                f"embedding 最老 pending 已 {int(oldest_age)} 秒无人消费"
                f"（≥ {int(MAX_PENDING_AGE_SECONDS)}）"
            )

    snapshot: dict[str, Any] = {
        "healthy": not reasons,
        "reason": "; ".join(reasons) if reasons else "Worker 健康",
        "captured_at": moment.isoformat(),
        "memory_maintenance_last_success_timestamp": last_success,
        "memory_maintenance_run_duration_seconds": durations,
        "memory_maintenance_consecutive_failures": failures,
        "memory_embedding_pending_total": pending,
        "memory_embedding_abandoned_total": abandoned,
        "memory_embedding_coverage_ratio": coverage,
        "memory_expired_active_total": expired_active,
        "memory_deleted_waiting_purge_total": deleted_waiting,
        "memory_proposals_pending_total": proposals,
        "memory_reembedding_progress_ratio": reembedding_progress,
    }
    if oldest_age is not None:
        snapshot["oldest_pending_age_seconds"] = round(oldest_age, 3)
    return snapshot


def _migration_progress(database: Any, target_profile: str) -> tuple[int, int]:
    """与 ``reembedding.backfill_progress`` 同一口径的 (completed, eligible)。"""

    pool = database._require_pool()
    with pool.connection() as conn:
        row = conn.execute(
            """
            WITH eligible AS (
                SELECT v.id AS memory_version_id
                  FROM memory_versions v
                  JOIN memory_records r ON r.id = v.memory_id
                 WHERE r.status = 'active'
                   AND r.active_version_id = v.id
                   AND v.valid_to IS NULL
            )
            SELECT
                (SELECT count(*) FROM eligible) AS eligible,
                (SELECT count(*) FROM memory_embedding_outbox o
                   JOIN eligible e ON e.memory_version_id = o.memory_version_id
                  WHERE o.embedding_profile_id = %s
                    AND o.status = 'completed') AS completed
            """,
            [target_profile],
        ).fetchone()
    if row is None:  # pragma: no cover
        return (0, 0)
    return (int(row["completed"]), int(row["eligible"]))


def flatten_metrics(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """把快照压平成 §6.6 的 Prometheus 键形态，供 ``record_run_metric`` 落账。

    - 标量指标原样：``memory_expired_active_total``；
    - 带标签的字典指标展开：``memory_maintenance_consecutive_failures{job="purge"}``。
    时间戳类指标的值是 ISO 字符串（账本可存；转 epoch 由 exporter 做）。
    """

    labels = {
        "memory_maintenance_last_success_timestamp": "job",
        "memory_maintenance_run_duration_seconds": "job",
        "memory_maintenance_consecutive_failures": "job",
        "memory_embedding_pending_total": "profile",
        "memory_embedding_abandoned_total": "profile",
        "memory_embedding_coverage_ratio": "profile",
        "memory_proposals_pending_total": "type",
        "memory_reembedding_progress_ratio": "migration",
    }
    flat: dict[str, Any] = {}
    for key, value in snapshot.items():
        if not key.startswith("memory_"):
            continue  # healthy / reason / captured_at 等非指标字段不上报
        label = labels.get(key)
        if label is None:
            flat[key] = value
        else:
            for label_value, metric_value in dict(value).items():
                flat[f'{key}{{{label}="{label_value}"}}'] = metric_value
    return flat
