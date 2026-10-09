"""记忆生命周期 Worker 健康快照（04 计划阶段 0 落地 + 7 完整化）。

阶段 0 时这是一个**仅协议**模块：`collect_snapshot()` 返回字典，键名与最终
Prometheus exporter 的指标名一致（避免后续重映射）。所有字段当前是占位值
``0`` 或 ``"unknown"``——这是有意的"模块存在但未配置"语义。

阶段 7 时这里会接 ``memory_maintenance_runs`` 与 ``runtime.governance.service``，
把所有 Job 的 `last_succeeded_at`、`consecutive_failures`、embedding coverage、
expiration backlog 等真实数字填进快照。

``memory_cutover.py`` 在 ``evaluate_worker_health`` 已经 ``try-import`` 本模块；
只要模块存在就调用 ``collect_snapshot()``，由返回的 ``healthy`` 字段决定是否
阻断 cutover。模块**不**存在的过渡态（阶段 0–6）由 try-import 兜底放行。

设计要点：

- 函数**不抛异常**——健康快照采集失败时返回 ``{"healthy": False, ...}``，
  切到 cutover 的路径**不能**因读取健康状态本身而炸。
- **不输出**正文/token/连接密码——只是聚合值。
- 字段名是最终契约，命名要稳定。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

__all__ = ["collect_snapshot", "UNAVAILABLE_DETAIL"]


# 占位值。阶段 7 时由 ``memory_maintenance_runs`` 与 ``runtime_events`` 填实。
UNAVAILABLE_DETAIL = "Worker 模块尚未就绪（阶段 0–6 过渡态）"


def collect_snapshot() -> Mapping[str, Any]:
    """返回当前 Worker 的健康快照。

    阶段 0：返回 ``UNAVAILABLE_DETAIL`` 标注的占位快照，``healthy`` 字段
    取 **True**——这是过渡态语义，与 ``memory_cutover.evaluate_worker_health``
    里"模块不存在 → 视为通过"是一致的（前者是模块存在但未配置，后者是模块
    还没写）。两者都不阻断 cutover。

    阶段 7：返回真实数字，``healthy`` 在 ``consecutive_failures < 3`` 且
    ``oldest_pending_age_seconds < 900`` 时为 True。
    """

    now = datetime.now(timezone.utc).isoformat()
    snapshot: dict[str, Any] = {
        # ---- 状态判定 ----
        "healthy": True,           # 过渡态默认 True
        "reason": UNAVAILABLE_DETAIL,
        "captured_at": now,
        # ---- 与 §6.6 健康指标字段一一对应（不重命名） ----
        "memory_maintenance_last_success_timestamp": {"embedding": None, "expiration": None, "purge": None, "consolidation": None, "reembedding": None, "health_report": None},
        "memory_maintenance_run_duration_seconds": {"embedding": 0, "expiration": 0, "purge": 0, "consolidation": 0, "reembedding": 0, "health_report": 0},
        "memory_maintenance_consecutive_failures": {"embedding": 0, "expiration": 0, "purge": 0, "consolidation": 0, "reembedding": 0, "health_report": 0},
        "memory_embedding_pending_total": {"default": 0},
        "memory_embedding_abandoned_total": {"default": 0},
        "memory_embedding_coverage_ratio": {"default": 1.0},
        "memory_expired_active_total": 0,
        "memory_deleted_waiting_purge_total": 0,
        "memory_proposals_pending_total": {"exact_duplicate": 0, "near_duplicate": 0, "conflict": 0, "merge": 0},
        "memory_reembedding_progress_ratio": {},
    }
    return snapshot