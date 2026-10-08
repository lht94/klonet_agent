"""治理层的装配入口：从环境构造 RuntimeGovernance。

降级规则（计划 阶段 1）与记忆系统一致——治理层是**可选部署件**：

- 开关 ``KLONET_AGENT_RUNTIME_GOVERNANCE`` 未打开 → ``None``，主链路逐字不变；
- 开关打开但没有记忆库 DSN → ``None`` 并记录原因（状态无处可落）；
- 开关打开、DSN 在但数据库连不上 → 照常构造，首次使用时按 fail-closed
  语义报错（telemetry 进缓冲）。

为什么默认关闭：没有 DSN 的环境里默认打开只会让每轮对话多一次失败连接；
"启用"落在部署变量上，切换门槛由评测把守（与 ``MEMORY_AUTHORITY`` 同哲学）。
"""

from __future__ import annotations

from typing import Any

from klonet_agent.memory.database import MemoryDatabase, memory_dsn
from klonet_agent.memory.domain import Tenant
from klonet_agent.runtime.governance.exporters import JsonlEventExporter
from klonet_agent.runtime.governance.service import RuntimeGovernance


def governance_enabled(env: dict[str, str] | None = None) -> bool:
    import os

    source = os.environ if env is None else env
    return (source.get("KLONET_AGENT_RUNTIME_GOVERNANCE", "0").strip().lower() in {
        "1", "true", "yes", "on",
    })


def runtime_governance_from_env(
    tenant: Tenant,
    *,
    session_id: str | None = None,
    mode: str | None = None,
    trace_file: Any = None,
) -> RuntimeGovernance | None:
    """构造治理边界；开关关闭或 DSN 缺失时返回 ``None``。"""

    if not governance_enabled():
        return None
    dsn = memory_dsn()
    if not dsn:
        return None
    database = MemoryDatabase(dsn, min_size=1, max_size=4)
    database.open()
    exporter = JsonlEventExporter(trace_file) if trace_file is not None else None
    return RuntimeGovernance(
        PostgresGovernanceRepositoryProxy(database, tenant),
        tenant,
        session_id=session_id,
        mode=mode,
        exporters=[exporter] if exporter else (),
    )


class PostgresGovernanceRepositoryProxy:
    """惰性导入的 PostgreSQL 仓储代理。

    治理仓储模块依赖 psycopg；代理把 import 推迟到第一次真正读写，
    保证没有装驱动的环境在构造阶段就拿到清晰的错误而不是 import 炸弹。
    """

    def __init__(self, database: MemoryDatabase, tenant: Tenant) -> None:
        self._database = database
        self._tenant = tenant
        self._inner: Any | None = None

    def _repo(self):
        if self._inner is None:
            from klonet_agent.runtime.governance.postgres import (
                PostgresGovernanceRepository,
            )

            self._inner = PostgresGovernanceRepository(self._database, self._tenant)
        return self._inner

    def __getattr__(self, name: str):
        return getattr(self._repo(), name)
