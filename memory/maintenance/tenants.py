"""租户枚举与游标编解码（04 计划各 Job 共用）。

**为什么单独一个模块**：阶段 3 的 ``EmbeddingOutboxJob`` 与阶段 4 的
``ExpirationJob`` / ``PurgeJob`` 都需要同一件事——"稳定顺序地遍历租户，
并记录断点"。如果每个 Job 各写一份排序/去重/cursor 逻辑，它们迟早会在
"project_id 为 NULL 排哪边""cursor 解析失败怎么办"这类细节上分叉，而这类
分叉在测试里几乎看不出来，只会表现为"某个租户永远轮不到"。

三条不变量：

1. **顺序稳定**：按 ``(user_id, project_id)``（``None`` 当空串，排在同 user 最前）。
   cursor 的正确性完全建立在这上面。
2. **cursor 解析失败一律返回 ``None``**（从头重扫），绝不"猜一个位置"。
   宁可多扫一遍，也不要因为 cursor 形态变化而静默漏掉一批租户。
3. **shared_ops 要么显式包含，要么完全不碰**。它在数据库层的放行挂在
   **角色**（``TO klonet_ops``）上而不是租户字段上，所以普通租户上下文
   永远扫不到它；这里把 `Tenant(user_id="shared")` 当作普通租户枚举出来，
   但真要在生产上读到它，连接角色必须是 ``klonet_ops``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from klonet_agent.memory.domain import Tenant

__all__ = [
    "SHARED_OPS_TENANT",
    "TenantSweepOutcome",
    "decode_tenant_cursor",
    "default_tenants_provider",
    "encode_tenant_cursor",
    "normalise_tenants",
    "sweep_tenants",
    "tenant_key",
    "tenants_after_cursor",
]

#: shared_ops 记忆的归属租户。与 ``memory/migration.py`` 的 ``_SHARED_USER_ID``
#: 保持一致——那里的共享记忆就是用这个 user_id 落库的。
SHARED_OPS_TENANT = Tenant(user_id="shared", project_id=None)


def tenant_key(tenant: Tenant) -> tuple[str, str]:
    """稳定排序键。``project_id`` 为 None 的记忆排在同一 user 的最前面。"""

    return (str(tenant.user_id), str(tenant.project_id or ""))


def encode_tenant_cursor(tenant: Tenant) -> str:
    """把一个租户编码成 cursor 字符串（JSON 数组，便于人工阅读与排查）。"""

    return json.dumps([str(tenant.user_id), tenant.project_id], ensure_ascii=False)


def decode_tenant_cursor(cursor: str | None) -> tuple[str, str | None] | None:
    """解析 cursor；无法解析时返回 ``None``（从头上重扫）。"""

    if not cursor:
        return None
    try:
        payload = json.loads(cursor)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, (list, tuple)) or len(payload) != 2:
        return None
    user_id = str(payload[0] or "").strip()
    if not user_id:
        return None
    project_id = payload[1]
    return (user_id, str(project_id) if project_id else None)


def normalise_tenants(tenants: Iterable[Tenant]) -> list[Tenant]:
    """去重 + 稳定排序。cursor 的正确性依赖这一步。"""

    unique: dict[tuple[str, str], Tenant] = {}
    for tenant in tenants:
        unique.setdefault(tenant_key(tenant), tenant)
    return [unique[key] for key in sorted(unique)]


def tenants_after_cursor(
    tenants: Sequence[Tenant], cursor: str | None
) -> list[Tenant]:
    """只保留排在 cursor 之后的租户；cursor 解析不出来时保留全部。"""

    ordered = list(tenants)
    decoded = decode_tenant_cursor(cursor)
    if decoded is None:
        return ordered
    after = (decoded[0], str(decoded[1] or ""))
    return [tenant for tenant in ordered if tenant_key(tenant) > after]


def default_tenants_provider(
    database: Any,
    *,
    include_shared_ops: bool = False,
    limit: int = 200,
) -> Callable[[], Sequence[Tenant]]:
    """构造默认的租户枚举器（``EmbeddingOutboxJob`` 等直接注入它）。

    ``include_shared_ops=True`` 时把 ``SHARED_OPS_TENANT`` 并进结果——**这是
    过期归档必须开的一项**：shared_ops 的放行挂在角色上，普通租户上下文
    永远扫不到它，漏掉就意味着共享运维记忆的过期记录永远不归档。
    """

    def _provider() -> Sequence[Tenant]:
        from klonet_agent.memory.postgres import list_active_tenants

        tenants = list(list_active_tenants(
            database, limit=limit, include_shared_ops=include_shared_ops
        ))
        if include_shared_ops and not any(
            tenant_key(tenant) == tenant_key(SHARED_OPS_TENANT) for tenant in tenants
        ):
            # 库里暂时没有 shared_ops 行也要枚举它——否则"共享记忆的过期归档"
            # 会在第一条共享记忆写进来之前一直不被调度，而没人会发现。
            tenants.append(SHARED_OPS_TENANT)
        return tenants

    return _provider


@dataclass(frozen=True)
class TenantSweepOutcome:
    """一次"按租户轮转"的结果，可直接喂给 ``JobResult``。"""

    tenants_done: int = 0
    scanned: int = 0
    changed: int = 0
    failed: int = 0
    next_cursor: str | None = None
    notes: tuple[str, ...] = ()


def sweep_tenants(
    tenants: Sequence[Tenant],
    cursor: str | None,
    *,
    handler: Callable[[Tenant], "tuple[int, int, int, str | None] | None"],
    clock: Callable[[], Any],
    batch_deadline: Any,
    should_stop: Callable[[], bool] | None = None,
    max_tenants: int = 50,
) -> TenantSweepOutcome:
    """按稳定顺序遍历租户，逐个交给 ``handler``，并在预算用尽时留 cursor。

    ``handler(tenant)`` 返回 ``(scanned, changed, failed, note)``；返回 ``None``
    表示"这个租户本轮跳过"，仍然推进 cursor（否则一个坏租户会卡住整条轮转）。

    cursor 语义与 ``EmbeddingOutboxJob`` 一致：记录**最后一个完整跑完的租户**；
    全部跑完写 ``None``，下一轮从头重扫（没有待办时是廉价空转，但能保证不会
    因为"只往后走"而永久漏掉排在前面的租户）。
    """

    remaining = tenants_after_cursor(tenants, cursor)[: max(0, int(max_tenants))]
    if not remaining:
        return TenantSweepOutcome(next_cursor=None)

    scanned = changed = failed = 0
    notes: list[str] = []
    last_done: Tenant | None = None
    done = 0
    all_done = True

    for index, tenant in enumerate(remaining):
        outcome = handler(tenant)
        if outcome is not None:
            scanned += int(outcome[0])
            changed += int(outcome[1])
            failed += int(outcome[2])
            if outcome[3]:
                notes.append(outcome[3])
        last_done = tenant
        done += 1
        if index < len(remaining) - 1:
            if should_stop is not None and should_stop():
                all_done = False
                break
            if batch_deadline is not None and clock() >= batch_deadline:
                all_done = False
                break

    next_cursor = None if all_done else (
        encode_tenant_cursor(last_done) if last_done is not None else None
    )
    return TenantSweepOutcome(
        tenants_done=done,
        scanned=scanned,
        changed=changed,
        failed=failed,
        next_cursor=next_cursor,
        notes=tuple(notes),
    )
