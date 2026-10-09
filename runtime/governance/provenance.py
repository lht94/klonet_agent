"""证据与 provenance（计划 §4.3）。

核心区分（W3C PROV-O 的简化落地）：

- **Entity（证据）**：某次观察——何时、从哪里、观察到什么、内容哈希；
- **Claim（主张）**：系统或用户提出的结论，与观察严格分离；
- **claim_evidence**：主张与证据的多对多关系（支持/反驳/不确定）。

计划 §4.3 的验收行为在这里落地：

- 修改源文件后，旧哈希证据能被识别为 stale（:func:`refresh_freshness`）；
- 冲突证据不静默覆盖（:class:`EvidenceConflict`，由服务层转成
  ``claims.status='contradicted'``）；
- 记忆候选的最小证据门槛（:func:`claim_evidence_gate`）。

Ops 现有 ``ops.privileged.workflow.contracts.EvidenceRecord`` 是领域专用
结构；计划 §10 要求不反向覆盖，只提供 :func:`ops_evidence_to_governance`
兼容 adapter 逐步统一协议。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from klonet_agent.runtime.governance.models import (
    GovernanceDomainError,
    enum_text,
    new_id,
    utcnow,
)


class SourceType(str, Enum):
    """证据来源类型（计划阶段 4 清单：文件、命令、HTTP、数据库、用户陈述、记忆召回）。"""

    TOOL = "tool"
    FILE = "file"
    COMMAND = "command"
    HTTP = "http"
    DATABASE = "database"
    USER_STATEMENT = "user_statement"
    MEMORY_RECALL = "memory_recall"
    OPS_PROBE = "ops_probe"


class ClaimStatus(str, Enum):

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNCERTAIN = "uncertain"


class ClaimEvidenceRelation(str, Enum):

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    UNCERTAIN = "uncertain"


class Scope(str, Enum):
    """证据作用域：决定谁能复用这条观察。"""

    USER = "user"
    PROJECT = "project"
    SESSION = "session"


# 每种来源类型的必填字段（计划：来源 adapter 对不同来源有不同最低要求）。
_SOURCE_REQUIRED: dict[SourceType, tuple[str, ...]] = {
    SourceType.TOOL: ("subject",),
    SourceType.FILE: ("source_uri", "artifact_hash"),
    SourceType.COMMAND: ("source_uri",),
    SourceType.HTTP: ("source_uri",),
    SourceType.DATABASE: ("source_uri",),
    SourceType.USER_STATEMENT: ("subject",),
    SourceType.MEMORY_RECALL: ("subject",),
    SourceType.OPS_PROBE: ("source_uri",),
}

# 证据优先级默认排序（计划 §4.3）：当前实时环境与工具结果 > 当前持久状态 >
# 本轮用户明确陈述 > 经验证记忆/RAG > 历史助手文本。数值越小优先级越高，
# 具体领域可覆盖但必须记录采用原因（由调用方把 priority 落进 claim/confidence）。
EVIDENCE_PRIORITY: dict[str, int] = {
    SourceType.TOOL.value: 0,
    SourceType.COMMAND.value: 0,
    SourceType.DATABASE.value: 0,
    SourceType.FILE.value: 1,
    SourceType.USER_STATEMENT.value: 2,
    SourceType.MEMORY_RECALL.value: 3,
    SourceType.HTTP.value: 3,
    SourceType.OPS_PROBE.value: 0,
}


class EvidenceDomainError(GovernanceDomainError):
    """证据领域规则违反。"""


@dataclass
class EvidenceRecord:
    """一次观察（Entity）。只存观察预览与哈希，不存大 payload。"""

    evidence_id: str
    run_id: str
    user_id: str
    project_id: str | None
    source_type: SourceType | str
    source_uri: str
    subject: str
    observation: str = ""
    source_revision: str | None = None
    observed_at: datetime = field(default_factory=utcnow)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    scope: Scope | str = Scope.PROJECT
    confidence: float = 0.5
    freshness: str = "fresh"
    artifact_hash: str | None = None
    producer: str | None = None
    parent_evidence_ids: list[str] = field(default_factory=list)
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        stype = SourceType(enum_text(self.source_type))
        for name in _SOURCE_REQUIRED.get(stype, ()):
            if not str(getattr(self, name) or "").strip():
                raise EvidenceDomainError(
                    f"来源类型 {stype.value} 的证据缺少必填字段: {name}"
                )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise EvidenceDomainError("confidence 必须在 [0, 1]")


@dataclass
class ClaimRecord:
    """一条主张（Activity/Agent 的结论），与证据分离。"""

    claim_id: str
    run_id: str
    user_id: str
    project_id: str | None
    subject: str
    statement: str
    status: ClaimStatus | str = ClaimStatus.UNCERTAIN
    confidence: float = 0.5
    task_id: str | None = None
    turn_id: str | None = None
    idempotency_key: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class ClaimEvidenceLink:
    """主张-证据关联（多对多）。"""

    claim_id: str
    evidence_id: str
    relation: ClaimEvidenceRelation | str = ClaimEvidenceRelation.SUPPORTS
    weight: float = 1.0


class EvidenceConflict(Exception):
    """同主体、同时效内的两条证据观察互相矛盾。

    这不是错误路径：服务层捕获后生成 ``claims.status='contradicted'``
    并把两条证据都挂上——冲突必须显式可见（计划 §4.3 验收第三条）。
    """

    def __init__(self, subject: str, existing_id: str, incoming_id: str):
        self.subject = subject
        self.existing_id = existing_id
        self.incoming_id = incoming_id
        super().__init__(
            f"证据冲突: subject={subject!r} 已有 {existing_id} 与新到 {incoming_id} 观察不一致"
        )


# --------------------------------------------------------------------------- #
# 内容寻址与 freshness
# --------------------------------------------------------------------------- #


def content_hash(data: Any) -> str:
    """稳定内容哈希（sha256）。

    文件/命令输出/HTTP 响应都先算这个哈希再入库；数据库只存哈希与预览，
    靠哈希判断"来源内容是否变过"。
    """

    if isinstance(data, bytes):
        return hashlib.sha256(data).hexdigest()
    if isinstance(data, str):
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    # 结构化对象：JSON 规范化后哈希（key 排序，保证同一内容同一哈希）。
    import json

    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def observation_signature(observation: str) -> str:
    """观察内容的规范化签名，用于冲突检测（忽略空白差异与大小写）。"""

    return re.sub(r"\s+", " ", str(observation or "")).strip().lower()


def is_stale(evidence: EvidenceRecord, now: datetime | None = None) -> bool:
    """判断证据是否过期（计划验收：修改源后旧哈希证据被识别为 stale）。"""

    moment = (now or utcnow()).astimezone(timezone.utc)
    if evidence.freshness == "stale":
        return True
    valid_until = evidence.valid_until
    if valid_until is not None:
        valid_until = valid_until.astimezone(timezone.utc)
        return moment >= valid_until
    return False


def refresh_freshness(
    evidence: EvidenceRecord,
    *,
    current_source_hash: str | None = None,
    now: datetime | None = None,
) -> str:
    """按当前来源哈希与有效期重算 freshness，返回新状态。

    - 来源哈希与登记时不一致 → stale（源已变，观察不再代表当前事实）；
    - 超过 valid_until → stale；
    - 其余保持 fresh。
    """

    if current_source_hash is not None and evidence.artifact_hash is not None:
        if current_source_hash != evidence.artifact_hash:
            evidence.freshness = "stale"
            return evidence.freshness
    evidence.freshness = "stale" if is_stale(evidence, now) else "fresh"
    return evidence.freshness


def detect_conflict(
    incoming: EvidenceRecord,
    existing: list[EvidenceRecord],
) -> EvidenceConflict | None:
    """检测同主体冲突：同 subject、双方 fresh、观察签名不同。

    冲突的判定刻意保守——只对"同一主体、同优先级来源、观察内容确实不同"
    报冲突；来源类型不同（如用户陈述 vs 工具结果）按 EVIDENCE_PRIORITY
    处理而不是冲突（高优先级观察胜出，由 claim 聚合时体现）。
    """

    incoming_type = SourceType(enum_text(incoming.source_type))
    for item in existing:
        if item.subject != incoming.subject:
            continue
        if item.evidence_id == incoming.evidence_id:
            continue
        if SourceType(enum_text(item.source_type)) != incoming_type:
            continue
        if is_stale(item) or is_stale(incoming):
            continue
        if observation_signature(item.observation) != observation_signature(
            incoming.observation
        ):
            return EvidenceConflict(
                incoming.subject, item.evidence_id, incoming.evidence_id
            )
    return None


def claim_evidence_gate(
    links: list[ClaimEvidenceLink],
    *,
    min_supports: int = 1,
) -> bool:
    """记忆候选的最小证据门槛（计划阶段 4：memory candidate 增加最小证据门槛）。

    主张至少要有 ``min_supports`` 条 ``supports`` 关联才允许被提炼成
    记忆候选；不满足时返回 False（调用方拒绝，fail closed）。
    """

    supports = sum(
        1
        for link in links
        if ClaimEvidenceRelation(enum_text(link.relation)) is ClaimEvidenceRelation.SUPPORTS
    )
    return supports >= min_supports


# --------------------------------------------------------------------------- #
# 来源 adapter
# --------------------------------------------------------------------------- #


def make_evidence(
    source_type: SourceType | str,
    *,
    run_id: str,
    user_id: str,
    project_id: str | None,
    subject: str,
    observation: str = "",
    source_uri: str | None = None,
    raw_content: Any = None,
    confidence: float = 0.5,
    scope: Scope | str = Scope.PROJECT,
    ttl_seconds: float | None = None,
    parent_evidence_ids: list[str] | None = None,
    idempotency_key: str | None = None,
    observed_at: datetime | None = None,
    producer: str | None = None,
) -> EvidenceRecord:
    """统一的证据构造入口（来源 adapter 的注册面）。

    ``raw_content`` 非空时自动计算内容哈希；``ttl_seconds`` 给出时自动推导
    ``valid_until``。各来源类型的必填字段缺失在 ``EvidenceRecord`` 构造时
    直接抛错（fail fast）。
    """

    stype = SourceType(enum_text(source_type))
    moment = observed_at or utcnow()
    if source_uri is None:
        source_uri = f"{stype.value}:{subject}"
    artifact_hash = content_hash(raw_content) if raw_content is not None else None
    valid_until = None
    if ttl_seconds is not None:
        from datetime import timedelta

        valid_until = moment + timedelta(seconds=max(0.0, float(ttl_seconds)))
    return EvidenceRecord(
        evidence_id=new_id(),
        run_id=run_id,
        user_id=user_id,
        project_id=project_id,
        source_type=stype,
        source_uri=source_uri,
        subject=subject,
        observation=observation,
        observed_at=moment,
        valid_from=moment,
        valid_until=valid_until,
        scope=scope,
        confidence=confidence,
        artifact_hash=artifact_hash,
        producer=producer,
        parent_evidence_ids=list(parent_evidence_ids or []),
        idempotency_key=idempotency_key,
    )


def ops_evidence_to_governance(
    ops_evidence: Any,
    *,
    run_id: str,
    user_id: str,
    project_id: str | None,
) -> EvidenceRecord:
    """Ops ``EvidenceRecord`` → 通用治理证据的兼容 adapter（计划 §10）。

    Ops 记录的字段是领域专用的（ProbeRequest / FactObservation）；
    这里做防御式映射，取不到的字段留空而不是猜。Ops 自身的权威语义
    不受影响——它继续是 Ops 域的事实源。
    """

    request = getattr(ops_evidence, "request", None)
    uri = str(
        getattr(request, "command", None)
        or getattr(request, "cache_key", None)
        or getattr(request, "label", None)
        or "ops-probe"
    )
    output = str(getattr(ops_evidence, "output", "") or "")
    status = str(getattr(ops_evidence, "status", "available") or "available")
    collected_at = getattr(ops_evidence, "collected_at", None)
    moment = utcnow()
    if collected_at:
        try:
            moment = datetime.fromisoformat(str(collected_at))
        except ValueError:
            moment = utcnow()
    return make_evidence(
        SourceType.OPS_PROBE,
        run_id=run_id,
        user_id=user_id,
        project_id=project_id,
        subject=uri[:200],
        observation=output[:2000],
        source_uri=uri[:500],
        raw_content=output,
        confidence=0.9 if status == "available" else 0.4,
        observed_at=moment,
        producer="ops",
    )
