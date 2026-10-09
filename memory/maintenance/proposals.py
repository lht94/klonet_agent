"""整理提案的领域模型与仓库（04 计划 §6.4 / 阶段 5）。

ConsolidationJob 只**产生提案**，永远不直接改写正式记忆。这个模块负责把
"只产生提案"变成可强制的东西：

1. **结构化建议**——模型/启发式只能提交 ``suggested_action`` 这个 JSON 载荷，
   拿不到 repository。它想改记忆的唯一途径是让运维 ``approve``，而 approve 走
   ``memory/versioning.py:apply_plan``（唯一写入出口）。
2. **指纹去重**——``proposal_fingerprint(type, ids)`` 只取决于**类型 + 排序后的
   候选集合**：同一批记忆重复扫描只会命中 partial unique index，不会攒出一堆
   一模一样的待办。
3. **乐观锁**——创建时记录 ``source_fingerprints``（每条来源的
   ``status / active_version_id / content_hash``）；apply 前重新加载比对，
   不一致就让提案 ``expired`` 并让下一轮重新生成。**不做这一步的后果**是：
   提案生成之后用户又改了那条记忆，批准时按老计划改，等于把新内容覆盖掉。
4. **状态机**——数据库触发器 + 这里的 ``_ALLOWED_TRANSITIONS`` 双重把关。
   ``applied`` 只能从 ``approved`` 来，这就是"未批准提案导致的正式版本变化
   数为 0"的可执行形式。

**租户过滤**：提案表在 ``memory_maintenance`` schema 里、没有 RLS（与 0006 的
调度表同一取舍）。所以 :class:`ProposalStore` **必须**绑定 ``Tenant``，且每条
SQL 都带 ``user_id`` / ``project_id`` 谓词——"提案表没有 RLS"不等于"可以跨租户
读"。跨 user/project/shared 的组合在创建阶段就被拒（见 ``ensure_same_tenant``）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence
from uuid import uuid4

from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.domain import Scope, Tenant

__all__ = [
    "ApplyOutcome",
    "Proposal",
    "ProposalCreateResult",
    "ProposalInvalidError",
    "ProposalStateError",
    "ProposalStatus",
    "ProposalStaleError",
    "ProposalStore",
    "ProposalType",
    "apply_proposal",
    "build_merge_candidate",
    "capture_source_fingerprints",
    "ensure_same_tenant",
    "is_transition_allowed",
    "proposal_fingerprint",
    "verify_sources_unchanged",
]

#: shared_ops 记忆的归属 user_id（与 ``memory/migration.py`` 的 ``_SHARED_USER_ID``
#: 以及 ``maintenance/tenants.SHARED_OPS_TENANT`` 保持一致）。
_SHARED_USER_ID = "shared"


class ProposalType(str, Enum):
    """提案类型。

    ``NOOP`` 是"确认重复但不需要动作"——exact duplicate 的确定性分类结果。
    它仍然要留一条记录，因为"扫描过、判断为无操作"本身就是审计信息。
    """

    NOOP = "noop"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    CONFLICT = "conflict"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    EXPIRED = "expired"


#: 终态：到了这里就不能再改状态（改了等于擦掉审计依据）。
TERMINAL_STATUSES = frozenset(
    {ProposalStatus.REJECTED, ProposalStatus.APPLIED, ProposalStatus.EXPIRED}
)

_ALLOWED_TRANSITIONS: Mapping[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.PENDING: frozenset(
        {ProposalStatus.APPROVED, ProposalStatus.REJECTED, ProposalStatus.EXPIRED}
    ),
    ProposalStatus.APPROVED: frozenset(
        {ProposalStatus.APPLIED, ProposalStatus.EXPIRED}
    ),
    ProposalStatus.REJECTED: frozenset(),
    ProposalStatus.APPLIED: frozenset(),
    ProposalStatus.EXPIRED: frozenset(),
}


class ProposalStateError(RuntimeError):
    """非法状态转换。数据库触发器会再拦一次，这里是可读的应用层报错。"""


def is_transition_allowed(
    current: ProposalStatus, target: ProposalStatus
) -> bool:
    """状态机查询（应用层与测试共用的唯一口径）。"""

    return target in _ALLOWED_TRANSITIONS[current]


class ProposalInvalidError(ValueError):
    """提案载荷不合法（跨租户组合、缺字段、类型不认识的建议）。"""


class ProposalStaleError(RuntimeError):
    """来源记忆在提案生成之后变了：提案已标 expired，需要重新生成。"""


def proposal_fingerprint(
    proposal_type: ProposalType | str, source_memory_ids: Sequence[str]
) -> str:
    """候选集合的稳定指纹。

    **只取决于类型与排序后的 id 集合**——顺序无关、进程无关、时间无关。
    刻意用 ``blake2b`` 而不是内置 ``hash()``：后者按进程加盐，同一个候选集合在
    两个 worker 里会得到不同指纹，去重直接失效（这是 02 阶段 7 写记忆评测时
    踩过的同一个坑）。
    """

    kind = proposal_type.value if isinstance(proposal_type, ProposalType) else str(proposal_type)
    normalized = sorted({str(item).strip().lower() for item in source_memory_ids if str(item).strip()})
    if len(normalized) < 2:
        raise ProposalInvalidError("提案至少需要两条来源记忆")
    payload = kind + "|" + "|".join(normalized)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def ensure_same_tenant(tenants: Sequence[tuple[str, str | None]]) -> None:
    """拒绝跨 user / project 的候选组合。

    这是"跨租户提案数为 0"的应用层一半；数据库一半是候选查询永远带租户谓词。
    两者都要有：只靠查询谓词，一次手写 SQL 就能绕过；只靠这里，读路径漏了谓词
    也不会被发现。
    """

    unique = {(str(user_id), project) for user_id, project in tenants}
    if len(unique) > 1:
        raise ProposalInvalidError(
            f"候选跨租户：{sorted(unique)}。整理提案绝不能跨越 user/project 边界"
        )


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    job_name: str
    user_id: str
    project_id: str | None
    scope: Scope
    proposal_type: ProposalType
    source_memory_ids: tuple[str, ...]
    suggested_action: Mapping[str, Any]
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[Any, ...]
    fingerprint: str
    policy_version: str
    model_version: str | None
    status: ProposalStatus
    created_at: datetime | None
    reviewed_at: datetime | None
    applied_version_ids: tuple[str, ...]
    source_fingerprints: Mapping[str, Any]

    @property
    def tenant(self) -> Tenant:
        return Tenant(user_id=self.user_id, project_id=self.project_id)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def as_dict(self) -> dict[str, Any]:
        """CLI / 报告用的可序列化视图（**不含正文**，本来也不存正文）。"""

        return {
            "proposal_id": self.proposal_id,
            "job_name": self.job_name,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "scope": self.scope.value,
            "proposal_type": self.proposal_type.value,
            "source_memory_ids": list(self.source_memory_ids),
            "suggested_action": dict(self.suggested_action),
            "reason_codes": list(self.reason_codes),
            "evidence_refs": list(self.evidence_refs),
            "fingerprint": self.fingerprint,
            "policy_version": self.policy_version,
            "model_version": self.model_version,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "applied_version_ids": list(self.applied_version_ids),
        }


@dataclass(frozen=True)
class ProposalCreateResult:
    """``create`` 的结果：``created=False`` 表示命中指纹去重（不是错误）。"""

    proposal: Proposal
    created: bool


def _row_to_proposal(row: Mapping[str, Any]) -> Proposal:
    raw_sources = row["source_memory_ids"] or []
    return Proposal(
        proposal_id=str(row["proposal_id"]),
        job_name=str(row["job_name"]),
        user_id=str(row["user_id"]),
        project_id=(str(row["project_id"]) if row["project_id"] else None),
        scope=Scope(str(row["scope"])),
        proposal_type=ProposalType(str(row["proposal_type"])),
        source_memory_ids=tuple(str(item) for item in raw_sources),
        suggested_action=dict(row["suggested_action"] or {}),
        reason_codes=tuple(str(item) for item in (row["reason_codes"] or ())),
        evidence_refs=tuple(row["evidence_refs"] or ()),
        fingerprint=str(row["fingerprint"]),
        policy_version=str(row["policy_version"]),
        model_version=(str(row["model_version"]) if row["model_version"] else None),
        status=ProposalStatus(str(row["status"])),
        created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
        applied_version_ids=tuple(str(item) for item in (row["applied_version_ids"] or ())),
        source_fingerprints=dict(row["source_fingerprints"] or {}),
    )


_SELECT_COLUMNS = """
    proposal_id, job_name, user_id, project_id, scope, proposal_type,
    source_memory_ids, suggested_action, reason_codes, evidence_refs,
    fingerprint, policy_version, model_version, status, created_at,
    reviewed_at, applied_version_ids, source_fingerprints
"""


class ProposalStore:
    """提案的读写入口。**必须绑定 Tenant**。"""

    def __init__(self, database: MemoryDatabase, tenant: Tenant):
        if not str(tenant.user_id or "").strip():
            raise ProposalInvalidError("ProposalStore 必须绑定非空 user_id")
        self._database = database
        self._tenant = tenant

    @property
    def tenant(self) -> Tenant:
        return self._tenant

    # ------------------------------------------------------------- 读 --

    def _tenant_clause(self, params: list[Any]) -> str:
        """租户谓词。``project_id`` 为 NULL 时只看 user 作用域提案。"""

        if self._tenant.project_id:
            params.extend([self._tenant.user_id, self._tenant.project_id])
            return "user_id = %s AND project_id = %s"
        params.append(self._tenant.user_id)
        return "user_id = %s AND project_id IS NULL"

    def list(
        self,
        *,
        status: ProposalStatus | str | None = None,
        limit: int = 100,
    ) -> list[Proposal]:
        params: list[Any] = []
        where = [self._tenant_clause(params)]
        if status is not None:
            where.append("status = %s")
            params.append(
                status.value if isinstance(status, ProposalStatus) else str(status)
            )
        params.append(max(1, int(limit)))
        with self._database.diagnostic_session() as conn:
            rows = conn.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                  FROM memory_maintenance.memory_maintenance_proposals
                 WHERE {' AND '.join(where)}
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                params,
            ).fetchall()
        return [_row_to_proposal(row) for row in rows]

    def get(self, proposal_id: str) -> Proposal | None:
        params: list[Any] = [proposal_id]
        where = self._tenant_clause(params)
        with self._database.diagnostic_session() as conn:
            row = conn.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                  FROM memory_maintenance.memory_maintenance_proposals
                 WHERE proposal_id = %s AND {where}
                """,
                params,
            ).fetchone()
        return _row_to_proposal(row) if row is not None else None

    def pending_count(self) -> int:
        params: list[Any] = []
        where = self._tenant_clause(params)
        with self._database.diagnostic_session() as conn:
            row = conn.execute(
                f"""
                SELECT count(*) AS n
                  FROM memory_maintenance.memory_maintenance_proposals
                 WHERE status = 'pending' AND {where}
                """,
                params,
            ).fetchone()
        return int(row["n"] or 0)

    # ------------------------------------------------------------- 写 --

    def create(
        self,
        *,
        proposal_type: ProposalType | str,
        source_memory_ids: Sequence[str],
        scope: Scope | str,
        suggested_action: Mapping[str, Any] | None = None,
        reason_codes: Sequence[str] = (),
        evidence_refs: Sequence[Any] = (),
        policy_version: str = "v1",
        model_version: str | None = None,
        source_fingerprints: Mapping[str, Any] | None = None,
        job_name: str = "consolidation",
    ) -> ProposalCreateResult:
        """创建提案；命中指纹去重时返回已有那条（``created=False``）。

        并发安全靠 partial unique index：两个 worker 同时扫到同一批记忆时，
        一个 INSERT 成功、另一个被索引挡下（``ON CONFLICT DO NOTHING``），
        然后读回已经存在的那条。
        """

        kind = proposal_type if isinstance(proposal_type, ProposalType) else ProposalType(str(proposal_type))
        scope_enum = scope if isinstance(scope, Scope) else Scope(str(scope))
        ids = tuple(str(item) for item in source_memory_ids)
        fingerprint = proposal_fingerprint(kind, ids)

        # 作用域必须与绑定租户自洽，否则会写出一条"挂在 user 作用域下、内容是
        # project 的"提案，读的时候又要靠猜。
        if scope_enum is Scope.PROJECT and not self._tenant.project_id:
            raise ProposalInvalidError("project 作用域提案必须有 project_id")
        if scope_enum is Scope.USER and self._tenant.project_id:
            raise ProposalInvalidError(
                "user 作用域提案不能带 project_id（否则读路径要猜按哪个过滤）"
            )
        if scope_enum is Scope.SHARED_OPS and (
            self._tenant.project_id or self._tenant.user_id != _SHARED_USER_ID
        ):
            raise ProposalInvalidError(
                "shared_ops 提案必须绑在 shared 租户上"
                f"（user_id={_SHARED_USER_ID!r}, project_id=None）"
            )

        proposal_id = str(uuid4())
        with self._database.diagnostic_session() as conn:
            row = conn.execute(
                """
                INSERT INTO memory_maintenance.memory_maintenance_proposals
                    (proposal_id, job_name, user_id, project_id, scope,
                     proposal_type, source_memory_ids, suggested_action,
                     reason_codes, evidence_refs, fingerprint, policy_version,
                     model_version, source_fingerprints)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb,
                        %s, %s, %s, %s::jsonb)
                ON CONFLICT DO NOTHING
                RETURNING proposal_id
                """,
                (
                    proposal_id,
                    str(job_name),
                    self._tenant.user_id,
                    self._tenant.project_id,
                    scope_enum.value,
                    kind.value,
                    list(ids),
                    json.dumps(dict(suggested_action or {}), ensure_ascii=False),
                    list(reason_codes),
                    json.dumps(list(evidence_refs), ensure_ascii=False),
                    fingerprint,
                    str(policy_version),
                    model_version,
                    json.dumps(dict(source_fingerprints or {}), ensure_ascii=False),
                ),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    f"""
                    SELECT {_SELECT_COLUMNS}
                      FROM memory_maintenance.memory_maintenance_proposals
                     WHERE fingerprint = %s
                       AND user_id = %s
                       AND COALESCE(project_id, '') = COALESCE(%s, '')
                       AND status IN ('pending', 'approved')
                     ORDER BY created_at DESC
                     LIMIT 1
                    """,
                    (fingerprint, self._tenant.user_id, self._tenant.project_id),
                ).fetchone()
                if existing is None:  # pragma: no cover - 并发删除的极小窗口
                    raise ProposalStateError(
                        "指纹冲突但读不到已有提案；并发删除？请重试"
                    )
                return ProposalCreateResult(
                    proposal=_row_to_proposal(existing), created=False
                )

            created = conn.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                  FROM memory_maintenance.memory_maintenance_proposals
                 WHERE proposal_id = %s
                """,
                (proposal_id,),
            ).fetchone()
        return ProposalCreateResult(proposal=_row_to_proposal(created), created=True)

    def transition(
        self,
        proposal_id: str,
        to_status: ProposalStatus | str,
        *,
        applied_version_ids: Sequence[str] = (),
    ) -> Proposal:
        """状态转换；非法转换在这里先拦一次，数据库触发器再兜底。"""

        target = (
            to_status if isinstance(to_status, ProposalStatus) else ProposalStatus(str(to_status))
        )
        current = self.get(proposal_id)
        if current is None:
            raise ProposalStateError(f"提案 {proposal_id} 不存在或不属于当前租户")
        if target not in _ALLOWED_TRANSITIONS[current.status]:
            raise ProposalStateError(
                f"非法状态转换：{current.status.value} -> {target.value}"
                f"（proposal_id={proposal_id}）"
            )
        tenant_params: list[Any] = []
        where = self._tenant_clause(tenant_params)
        with self._database.diagnostic_session() as conn:
            conn.execute(
                f"""
                UPDATE memory_maintenance.memory_maintenance_proposals
                   SET status = %s,
                       applied_version_ids = %s,
                       reviewed_at = now()
                 WHERE proposal_id = %s AND {where}
                """,
                [
                    target.value,
                    list(applied_version_ids),
                    proposal_id,
                    *tenant_params,
                ],
            )
        updated = self.get(proposal_id)
        if updated is None:  # pragma: no cover - 并发删除
            raise ProposalStateError(f"提案 {proposal_id} 更新后读不到")
        return updated

    def expire(self, proposal_id: str) -> Proposal:
        return self.transition(proposal_id, ProposalStatus.EXPIRED)


# --------------------------------------------------------------------------- #
# 乐观锁与落地
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ApplyOutcome:
    """一次 approve 的结果。"""

    proposal: Proposal
    decision: str | None = None
    record_id: str | None = None
    applied_version_ids: tuple[str, ...] = ()


def capture_source_fingerprints(
    repository: Any, memory_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """记录每条来源当前的 ``(status, active_version_id, content_hash)``。

    ``content_hash`` 是关键——只看 ``active_version_id`` 会漏掉"同一版本被原地
    改了内容"（正常路径不允许，但历史遗留与手工 SQL 都可能出现）。
    """

    snapshot: dict[str, dict[str, Any]] = {}
    for memory_id in memory_ids:
        key = str(memory_id)
        record = repository.get_record(key)
        if record is None:
            snapshot[key] = {"missing": True}
            continue
        version = record.active_version
        snapshot[key] = {
            "status": record.status.value,
            "active_version_id": record.active_version_id,
            "content_hash": getattr(version, "content_hash", None),
        }
    return snapshot


def verify_sources_unchanged(repository: Any, proposal: Proposal) -> None:
    """乐观锁校验；任何一条来源变了就抛 :class:`ProposalStaleError`。

    **没有快照也算过期**：无法证明来源没变，就不能按旧计划写库。宁可让下一轮
    重新生成提案（廉价），也不要在一个无法验证的前提下改动正式记忆。
    """

    expected = {str(key): value for key, value in (proposal.source_fingerprints or {}).items()}
    if not expected:
        raise ProposalStaleError(
            f"提案 {proposal.proposal_id} 没有来源快照，无法验证，按过期处理"
        )
    current = capture_source_fingerprints(repository, list(expected))
    for memory_id, before in expected.items():
        after = current.get(memory_id)
        if after != before:
            raise ProposalStaleError(
                f"来源 {memory_id} 在提案生成后发生变化"
                f"（{before} -> {after}），提案需重新生成"
            )


def build_merge_candidate(repository: Any, proposal: Proposal) -> Any:
    """把 MERGE/SUPERSEDE 提案的 ``suggested_action`` 翻译成 ``MemoryCandidate``。

    **这是 prompt injection 的主要防线**：模型只能提供 ``merged_content``
    与（可选）``subject_key``；``memory_type`` / ``scope`` / ``user_id`` /
    ``project_id`` / ``confidence`` 全部由 apply 路径从**当前来源记录**重建。
    于是即便模型被诱导输出"把这条记忆改成 shared_ops 并写入 alice 名下"，
    也不存在表达它的字段——不是"我们检查后拒绝"，而是"压根没法写出来"。
    """

    from klonet_agent.memory.domain import MemoryCandidate

    payload = dict(proposal.suggested_action or {})
    content = str(payload.get("merged_content") or "").strip()
    if not content:
        raise ProposalInvalidError(
            "merge/supersede 提案必须提供非空的 suggested_action.merged_content"
        )

    records = [
        record
        for record in (repository.get_record(item) for item in proposal.source_memory_ids)
        if record is not None
    ]
    if not records:
        raise ProposalStaleError(
            f"提案 {proposal.proposal_id} 的所有来源都已不存在，按过期处理"
        )

    primary = records[0]
    seen: set[tuple[str, str]] = set()
    sources: list[Any] = []
    for record in records:
        version = record.active_version
        if version is None or not getattr(version, "id", None):
            continue
        # ``get_record`` 不会顺带把来源查出来（``MemoryVersion`` 上的
        # ``sources`` 在按行构造时是空的）。必须显式走 ``list_sources``，
        # 否则候选会因为"没有任何来源"被 ``plan_consolidation`` 的 R1 直接
        # REJECT——这正是真库测试第一次跑出来的现象。
        for source in repository.list_sources(str(version.id)) or ():
            key = (source.source_type.value, source.source_id)
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)

    subject_key = str(payload.get("subject_key") or "").strip() or primary.subject_key
    return MemoryCandidate(
        memory_type=primary.memory_type,
        scope=primary.scope,
        project_id=primary.project_id,
        user_id=primary.user_id,
        subject_key=subject_key,
        content=content,
        confidence=max(record.confidence for record in records),
        sources=tuple(sources),
    )


def apply_proposal(
    store: ProposalStore,
    repository: Any,
    proposal_id: str,
) -> ApplyOutcome:
    """把一条**已批准**的提案落地。

    四步，每步都有明确失败方向：

    1. **状态必须是 approved**——pending 直接拒。这就是"未批准提案导致的正式
       版本变化数为 0"的应用层一半（另一半是数据库触发器：``applied`` 只能从
       ``approved`` 来）。
    2. **重新加载来源并做乐观锁校验**——不一致 → 标 ``expired`` 并抛
       :class:`ProposalStaleError`。
    3. **NOOP 不动正式记忆**——exact duplicate 的确定性结论就是"什么都不做"，
       标 applied 即可。
    4. MERGE/SUPERSEDE 走 ``versioning.plan_consolidation`` +
       ``apply_plan``（**唯一写入出口**），绝不直接调 repository 的写入方法。

    顺序说明：正文先落地、再把提案标 ``applied``。反过来的话，写入失败会留下
    一条"已应用但什么都没变"的假审计。中间崩掉的窗口里提案停在 ``approved``，
    重跑是幂等的（``plan_consolidation`` 会看到内容已等价而给出 UPDATE/NOOP）。
    """

    from klonet_agent.memory.versioning import apply_plan, plan_consolidation

    proposal = store.get(proposal_id)
    if proposal is None:
        raise ProposalStateError(f"提案 {proposal_id} 不存在或不属于当前租户")
    if proposal.status is not ProposalStatus.APPROVED:
        raise ProposalStateError(
            f"提案 {proposal_id} 状态为 {proposal.status.value}，"
            "只有已批准（approved）的提案才能落地"
        )

    try:
        verify_sources_unchanged(repository, proposal)
    except ProposalStaleError:
        store.expire(proposal_id)
        raise

    if proposal.proposal_type is ProposalType.NOOP:
        applied = store.transition(proposal_id, ProposalStatus.APPLIED)
        return ApplyOutcome(proposal=applied, decision="noop")

    candidate = build_merge_candidate(repository, proposal)
    existing = repository.find_active_by_subject(candidate.subject_key)
    plan = plan_consolidation(candidate, existing)
    outcome = apply_plan(repository, plan, candidate)

    version_ids = tuple(
        str(item)
        for item in (
            [getattr(outcome.version, "id", None)] if outcome.version is not None else []
        )
        if item
    )
    applied = store.transition(
        proposal_id, ProposalStatus.APPLIED, applied_version_ids=version_ids
    )
    return ApplyOutcome(
        proposal=applied,
        decision=plan.decision.value,
        record_id=str(outcome.record.id) if outcome.record is not None else None,
        applied_version_ids=version_ids,
    )
