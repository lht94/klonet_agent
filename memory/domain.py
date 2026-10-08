"""记忆系统领域模型。

这一层是纯领域对象：不 import 任何数据库驱动、不感知 SQL、不做 I/O。
它的作用是把"一条记忆是什么"固定下来，供 repository 契约、write pipeline、
retriever 和 MemoryPack 共同引用，避免各处再各自拼字典。

设计约束（来自 `通用功能升级计划/02-记忆系统升级计划.md` §5.2 / §7.1）：

- 一条逻辑记忆（``MemoryRecord``）与它的不可变版本（``MemoryVersion``）分开。
  正文只存在于版本里：改一条记忆等于新增版本，不覆盖历史。
- 有效时间（``valid_from`` / ``valid_to``）属于版本而非逻辑记录，
  这样"当时有效的事实是什么"可以被单独回答。
- 每条结论必须能追溯到来源（``MemorySource``）；只有 assistant 自述不算已验证。
- ``importance`` 只表示未来复用价值，不表示事实正确性；
  ``confidence`` 来自来源等级。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# 与 migrations/0001_init.sql 里 memory_sources.source_excerpt 的 CHECK 保持一致。
# 摘录进库前必须脱敏并限长，完整证据留在原事件系统里。
SOURCE_EXCERPT_MAX_CHARS = 1000

# subject_key 的分隔符。组件内不允许出现该字符，否则解析会产生歧义。
_SUBJECT_SEPARATOR = ":"

# episode 的 subject_key 前缀固定，后接事件 UUID。
_EPISODE_PREFIX = "episode"

_COMPONENT_PATTERN = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


class Scope(str, Enum):
    """记忆的作用域。决定谁能读到它。"""

    USER = "user"
    PROJECT = "project"
    SHARED_OPS = "shared_ops"


class MemoryType(str, Enum):
    """记忆类型。三类生命周期和冲突规则都不同。"""

    EPISODE = "episode"
    FACT = "fact"
    PREFERENCE = "preference"


class MemoryStatus(str, Enum):
    """逻辑记忆的状态。旧事实失效而不是删除。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DELETED = "deleted"


# 状态机。ACTIVE 是唯一的非终态：被替代、过期、删除都是终态。
#
# 这样定义的依据是计划 §6.5：旧记忆不是"错误"而是"已经过期"，因此它的历史版本
# 必须保留；一条记忆失效之后就永远读得到它的历史，但不允许被"复活"——
# 同一 subject 再次成立时会产生**新的**逻辑记录（唯一索引此时已经放行），
# 旧记录仍然是当初那条事实的完整历史。这条约束同时保证了
# `valid_to` 只会被写一次，不会被后来的操作覆盖。
_ALLOWED_STATUS_TRANSITIONS: Mapping[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset(
        {MemoryStatus.SUPERSEDED, MemoryStatus.EXPIRED, MemoryStatus.DELETED}
    ),
    MemoryStatus.SUPERSEDED: frozenset(),
    MemoryStatus.EXPIRED: frozenset(),
    MemoryStatus.DELETED: frozenset(),
}


def allowed_status_transitions(status: MemoryStatus | str) -> frozenset[MemoryStatus]:
    """返回某状态允许迁移到的目标状态集合。"""

    try:
        resolved = MemoryStatus(status)
    except ValueError as exc:
        raise MemoryDomainError(f"非法记忆状态: {status!r}") from exc
    return _ALLOWED_STATUS_TRANSITIONS[resolved]


def ensure_status_transition(
    current: MemoryStatus | str, target: MemoryStatus | str, *, subject: str = ""
) -> None:
    """校验状态迁移是否被允许，不允许时抛 InvalidStatusTransitionError。"""

    try:
        resolved_current = MemoryStatus(current)
    except ValueError as exc:
        raise MemoryDomainError(f"非法记忆状态: {current!r}") from exc
    try:
        resolved_target = MemoryStatus(target)
    except ValueError as exc:
        raise MemoryDomainError(f"非法记忆状态: {target!r}") from exc

    if resolved_target is resolved_current:
        return
    if resolved_target not in _ALLOWED_STATUS_TRANSITIONS[resolved_current]:
        where = f"（{subject}）" if subject else ""
        raise InvalidStatusTransitionError(
            f"不允许把记忆{where}从 {resolved_current.value} 迁移到 "
            f"{resolved_target.value}：{resolved_current.value} 是终态，"
            "同一 subject 再次成立时应创建新的逻辑记录"
        )


class SourceType(str, Enum):
    """记忆结论的证据来源类型。"""

    HISTORY_EVENT = "history_event"
    TOOL_RESULT = "tool_result"
    JOURNAL = "journal"
    USER_STATEMENT = "user_statement"


class RelationType(str, Enum):
    """逻辑记忆之间的关系。首版不上图数据库，这四种足够。"""

    SUPERSEDES = "supersedes"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    RELATED_TO = "related_to"


class WriteDecision(str, Enum):
    """候选写入的五种受控决策。"""

    ADD = "add"
    UPDATE = "update"
    SUPERSEDE = "supersede"
    NOOP = "noop"
    REJECT = "reject"


class ImportanceLevel(str, Enum):
    """复用价值等级。

    §7.1 要求 importance "由枚举规则映射为数值"，而不是让模型直接给一个
    浮点数——浮点数既不可复现，也没法在评测里被断言。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


IMPORTANCE_BY_LEVEL: Mapping[ImportanceLevel, float] = {
    ImportanceLevel.LOW: 0.3,
    ImportanceLevel.MEDIUM: 0.6,
    ImportanceLevel.HIGH: 0.9,
}


def importance_for_level(level: ImportanceLevel | str) -> float:
    """把复用价值等级映射成数值。"""

    try:
        resolved = ImportanceLevel(level)
    except ValueError as exc:
        raise MemoryDomainError(f"非法复用价值等级: {level!r}") from exc
    return IMPORTANCE_BY_LEVEL[resolved]


class MemoryDomainError(ValueError):
    """领域模型校验失败。调用方必须修正输入，不允许静默兜底。"""


class InvalidStatusTransitionError(MemoryDomainError):
    """试图做状态机不允许的迁移（例如把已失效的记忆再失效一次）。"""


# --------------------------------------------------------------------------- #
# 规范化与内容哈希
# --------------------------------------------------------------------------- #


def normalize_content(content: str) -> str:
    """规范化正文，用于计算内容哈希。

    只做不影响语义的归一：统一换行、压缩空白、去掉首尾空白。
    刻意不做大小写折叠——正文是给人读的，哈希只用于判重。
    """

    text = unicodedata.normalize("NFKC", str(content))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def content_hash(content: str) -> str:
    """计算规范化正文的 sha256。

    用于两条幂等路径：同一记忆不能插入重复版本；同一候选不能重复处理。
    """

    normalized = normalize_content(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_component(value: str) -> str:
    """把 subject_key 的单个组件规范化成稳定 token。"""

    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    text = _COMPONENT_PATTERN.sub("_", text).strip("_")
    if not text:
        raise MemoryDomainError(f"subject_key 组件规范化后为空: {value!r}")
    return text


def build_subject_key(
    *,
    memory_type: MemoryType,
    scope: Scope | None = None,
    entity: str | None = None,
    attribute: str | None = None,
    episode_id: str | None = None,
) -> str:
    """构造规范化 subject_key。

    - fact/preference：``<type>:<scope>:<entity>:<attribute>``，
      例如 ``fact:project:demo:python_version``（见计划 §7.1）。
    - episode：``episode:<event_uuid>``。情景记忆按事件身份去重，
      不按主题覆盖——两次不同的经历可以拥有同一个话题。
    """

    if memory_type is MemoryType.EPISODE:
        if not episode_id:
            raise MemoryDomainError("episode 必须提供 episode_id 作为 subject_key")
        return f"{_EPISODE_PREFIX}{_SUBJECT_SEPARATOR}{normalize_component(episode_id)}"

    if scope is None or entity is None or attribute is None:
        raise MemoryDomainError(
            f"{memory_type.value} 的 subject_key 需要 scope/entity/attribute"
        )
    return _SUBJECT_SEPARATOR.join(
        [
            memory_type.value,
            scope.value,
            normalize_component(entity),
            normalize_component(attribute),
        ]
    )


@dataclass(frozen=True)
class SubjectKey:
    """解析后的 subject_key。"""

    memory_type: MemoryType
    scope: Scope | None
    entity: str | None
    attribute: str | None
    episode_id: str | None
    raw: str


def parse_subject_key(raw: str) -> SubjectKey:
    """解析 subject_key，并在格式非法时直接报错。"""

    parts = str(raw).split(_SUBJECT_SEPARATOR)
    if not parts or not parts[0]:
        raise MemoryDomainError(f"subject_key 为空: {raw!r}")
    try:
        memory_type = MemoryType(parts[0])
    except ValueError as exc:
        raise MemoryDomainError(f"subject_key 前缀不是合法记忆类型: {raw!r}") from exc

    if memory_type is MemoryType.EPISODE:
        if len(parts) != 2 or not parts[1]:
            raise MemoryDomainError(f"episode subject_key 必须是 episode:<id>: {raw!r}")
        return SubjectKey(memory_type, None, None, None, parts[1], raw)

    if len(parts) != 4:
        raise MemoryDomainError(
            f"{memory_type.value} subject_key 必须是 "
            f"<type>:<scope>:<entity>:<attribute>: {raw!r}"
        )
    try:
        scope = Scope(parts[1])
    except ValueError as exc:
        raise MemoryDomainError(f"subject_key 作用域非法: {raw!r}") from exc
    if not parts[2] or not parts[3]:
        raise MemoryDomainError(f"subject_key 实体或属性为空: {raw!r}")
    return SubjectKey(memory_type, scope, parts[2], parts[3], None, raw)


# --------------------------------------------------------------------------- #
# 三类记忆各自的领域规则
# --------------------------------------------------------------------------- #
#
# 这里集中放"哪类记忆允许哪些作用域/能否被替代/需要什么才能算已验证"。
# 放在领域层而不是仓库层，是因为这些约束对候选人（还没落库）和正式记忆
# （已落库）同样成立：候选在写入前就应该被同一套规则拦下，而不是先进库再报错。

ALLOWED_SCOPES_BY_TYPE: Mapping[MemoryType, frozenset[Scope]] = {
    # 经历可以属于某个人、某个项目，也可以作为运维共享经历。
    MemoryType.EPISODE: frozenset({Scope.USER, Scope.PROJECT, Scope.SHARED_OPS}),
    # 事实同理：项目事实、用户事实、平台共享事实。
    MemoryType.FACT: frozenset({Scope.USER, Scope.PROJECT, Scope.SHARED_OPS}),
    # 偏好一定有归属人。shared_ops 里放"某人的偏好"没有意义——
    # 计划 §6.3 把偏好限定为"全局稳定偏好 / 仅某个项目有效的偏好"。
    MemoryType.PREFERENCE: frozenset({Scope.USER, Scope.PROJECT}),
}

# 情景记忆按事件身份去重（subject_key = episode:<uuid>），两次不同的经历
# 可以拥有同一个话题。计划 §6.5 第 3 条明确："情景记忆：保留各自事件，
# 不因结果不同而互相覆盖"——因此 episode 不参与 supersede。
SUPERSEDABLE_TYPES: frozenset[MemoryType] = frozenset(
    {MemoryType.FACT, MemoryType.PREFERENCE}
)

# 能把一条记忆标为"已验证"的来源：用户明确陈述，或工具成功执行的证据。
# history_event 只是对话原文，assistant 的自述也在里面，所以不算证据——
# §7.1："仅 assistant 自述不能标为已验证"。
VERIFYING_SOURCE_TYPES: frozenset[SourceType] = frozenset(
    {SourceType.USER_STATEMENT, SourceType.TOOL_RESULT}
)

# 有资格"替用户说话"的来源。偏好是主观的，工具证据不能替用户决定，
# 见 §6.5 冲突规则第 2 条。
USER_AUTHORITATIVE_SOURCE_TYPES: frozenset[SourceType] = frozenset(
    {SourceType.USER_STATEMENT}
)

# 来源等级决定的 confidence 上限：用户明确陈述最高，工具验证次之，
# 项目日志再次，原始对话事件最低（§7.1："confidence 来自来源等级"）。
SOURCE_CONFIDENCE_CEILING: Mapping[SourceType, float] = {
    SourceType.USER_STATEMENT: 1.0,
    SourceType.TOOL_RESULT: 0.95,
    SourceType.JOURNAL: 0.8,
    SourceType.HISTORY_EVENT: 0.6,
}

# 完全没有来源时的 confidence。候选缺来源本身会被决策层 REJECT，
# 这个常量只用于"来源齐全但都没定级"的兜底。
DEFAULT_CONFIDENCE = 0.5


def validate_type_scope(memory_type: MemoryType | str, scope: Scope | str) -> None:
    """校验记忆类型与作用域的组合是否合法。"""

    try:
        resolved_type = MemoryType(memory_type)
    except ValueError as exc:
        raise MemoryDomainError(f"非法记忆类型: {memory_type!r}") from exc
    try:
        resolved_scope = Scope(scope)
    except ValueError as exc:
        raise MemoryDomainError(f"非法作用域: {scope!r}") from exc

    allowed = ALLOWED_SCOPES_BY_TYPE[resolved_type]
    if resolved_scope not in allowed:
        raise MemoryDomainError(
            f"{resolved_type.value} 不允许 {resolved_scope.value} 作用域，"
            f"允许取值为 {sorted(item.value for item in allowed)}"
        )


def type_allows_supersede(memory_type: MemoryType | str) -> bool:
    """该类型是否参与替代（supersede）。"""

    try:
        return MemoryType(memory_type) in SUPERSEDABLE_TYPES
    except ValueError as exc:
        raise MemoryDomainError(f"非法记忆类型: {memory_type!r}") from exc


def verifying_sources(
    sources: Sequence[MemorySource] | None,
) -> tuple[MemorySource, ...]:
    """从来源里挑出有验证资格的（用户陈述 / 工具结果）。"""

    return tuple(
        source for source in (sources or ()) if source.source_type in VERIFYING_SOURCE_TYPES
    )


def qualifies_as_verified(
    memory_type: MemoryType | str, sources: Sequence[MemorySource] | None
) -> bool:
    """按类型判断这组来源能不能支撑 verified=True。

    - 事实 / 经历：用户明确陈述或工具成功证据任一即可。
    - 偏好：只认用户明确陈述——工具证据不得替用户决定主观偏好（§6.5 规则 2）。
    """

    candidates = verifying_sources(sources)
    if not candidates:
        return False
    if MemoryType(memory_type) is MemoryType.PREFERENCE:
        return any(
            source.source_type in USER_AUTHORITATIVE_SOURCE_TYPES for source in candidates
        )
    return True


def ensure_verification_supported(
    memory_type: MemoryType | str,
    verified: bool,
    sources: Sequence[MemorySource] | None,
) -> None:
    """`verified=True` 必须有合格来源；否则直接报错，不允许"自封已验证"。"""

    if not verified:
        return
    if qualifies_as_verified(memory_type, sources):
        return
    if MemoryType(memory_type) is MemoryType.PREFERENCE:
        raise MemoryDomainError(
            "偏好只有在用户明确陈述时才能标为已验证；工具证据不能替用户决定主观偏好"
        )
    raise MemoryDomainError(
        "标为已验证的记忆必须带用户明确陈述或工具成功证据（history_event 不算）"
    )


def confidence_ceiling(sources: Sequence[MemorySource] | None) -> float:
    """一组来源允许的置信度上限。无来源时返回默认值。"""

    ceilings = [
        SOURCE_CONFIDENCE_CEILING[source.source_type]
        for source in (sources or ())
        if source.source_type in SOURCE_CONFIDENCE_CEILING
    ]
    return max(ceilings) if ceilings else DEFAULT_CONFIDENCE


def same_content(left: str, right: str) -> bool:
    """两条正文在"判重"意义下是否等价（按规范化后的 sha256 比较）。"""

    return content_hash(left) == content_hash(right)


def source_keys(
    sources: Sequence[MemorySource] | None,
) -> frozenset[tuple[SourceType, str]]:
    """来源的身份集合，用于判断"新来源是否带来新信息"。"""

    return frozenset((source.source_type, source.source_id) for source in (sources or ()))


def additional_source_keys(
    existing: Sequence[MemorySource] | None,
    incoming: Sequence[MemorySource] | None,
) -> frozenset[tuple[SourceType, str]]:
    """incoming 里 existing 没有的来源身份。为空表示没有新增信息。"""

    return source_keys(incoming) - source_keys(existing)


def validate_validity_window(
    valid_from: datetime, valid_to: datetime | None
) -> None:
    """有效期必须是半开区间 ``[valid_from, valid_to)``。

    ``valid_from`` 允许早于 ``observed_at``：事实在被观察到之前就可能已经成立
    （计划 §6.5 的 Python 3.8→3.11 迁移例子就是这个形状）。
    """

    if valid_from is None:
        raise MemoryDomainError("valid_from 不能为空")
    if valid_to is not None and valid_to <= valid_from:
        raise MemoryDomainError(
            f"valid_to({valid_to.isoformat()}) 必须晚于 valid_from({valid_from.isoformat()})"
        )


def is_valid_at(version: "MemoryVersion", moment: datetime) -> bool:
    """``as_of`` 语义：version 在给定时刻是否有效。

    默认查询用 ``version.is_current``（只认未失效版本），只有回看历史状态
    才用这个函数。
    """

    if moment < version.valid_from:
        return False
    return version.valid_to is None or moment < version.valid_to


# --------------------------------------------------------------------------- #
# 领域对象
# --------------------------------------------------------------------------- #


def _check_ratio(name: str, value: float) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise MemoryDomainError(f"{name} 必须落在 [0, 1]: {value!r}")
    return number


@dataclass(frozen=True)
class Tenant:
    """一次数据库会话绑定的租户上下文。

    所有查询都必须先绑定 ``user_id``；项目记忆还必须绑定 ``project_id``。
    ``allow_shared_ops`` 只应由 Ops 服务角色打开。
    """

    user_id: str
    project_id: str | None = None
    allow_shared_ops: bool = False

    def __post_init__(self) -> None:
        if not str(self.user_id).strip():
            raise MemoryDomainError("Tenant 必须绑定非空 user_id")


@dataclass(frozen=True)
class MemorySource:
    """把记忆结论关联回原始证据。"""

    source_type: SourceType
    source_id: str
    observed_at: datetime
    memory_version_id: str | None = None
    source_excerpt: str = ""

    def __post_init__(self) -> None:
        if not str(self.source_id).strip():
            raise MemoryDomainError("来源必须带 source_id，无来源的记忆不写库")
        if len(self.source_excerpt) > SOURCE_EXCERPT_MAX_CHARS:
            raise MemoryDomainError(
                f"source_excerpt 超过 {SOURCE_EXCERPT_MAX_CHARS} 字符上限，"
                "摘录必须先脱敏并限长"
            )
        if self.observed_at is None:
            raise MemoryDomainError("来源必须带 observed_at")


@dataclass(frozen=True)
class MemoryVersion:
    """一条不可变版本。正文、有效时间、embedding 都挂在这里。

    "不可变"是硬约束：正文、哈希、时间戳、分词结果都不允许事后修改，
    数据库侧还有触发器兜底（见 ``migrations/0003``）。全列里只有三类可以变——
    ``valid_to``（失效/替代时结束有效期）、``embedding`` 三列（worker 异步补算）、
    以及 ``verified`` 从 false 翻到 true（后续来源补齐了证据）。
    """

    id: str
    memory_id: str
    version: int
    content: str
    observed_at: datetime
    valid_from: datetime
    content_hash: str
    summary: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    lexical_text: str | None = None
    embedding: Sequence[float] | None = None
    embedding_model: str | None = None
    embedding_version: str | None = None
    valid_to: datetime | None = None
    created_at: datetime | None = None
    verified: bool = False
    sources: tuple[MemorySource, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.content).strip():
            raise MemoryDomainError("版本正文不能为空")
        if not str(self.content_hash).strip():
            raise MemoryDomainError("版本必须带 content_hash")
        validate_validity_window(self.valid_from, self.valid_to)
        if self.embedding is not None and not (
            self.embedding_model and self.embedding_version
        ):
            raise MemoryDomainError(
                "写入向量必须同时给出 embedding_model 与 embedding_version"
            )

    @property
    def is_current(self) -> bool:
        """是否仍处于有效期（valid_to 为空即视为未失效）。"""

        return self.valid_to is None


@dataclass(frozen=True)
class MemoryRecord:
    """一条逻辑记忆的当前身份。不直接保存可变正文。"""

    id: str
    user_id: str
    scope: Scope
    memory_type: MemoryType
    subject_key: str
    status: MemoryStatus = MemoryStatus.ACTIVE
    project_id: str | None = None
    active_version_id: str | None = None
    importance: float = 0.5
    confidence: float = 0.5
    created_at: datetime | None = None
    updated_at: datetime | None = None
    active_version: MemoryVersion | None = None

    def __post_init__(self) -> None:
        if not str(self.user_id).strip():
            raise MemoryDomainError("记忆必须绑定 user_id")
        validate_type_scope(self.memory_type, self.scope)
        if self.scope is Scope.PROJECT and not self.project_id:
            raise MemoryDomainError("项目作用域记忆必须带 project_id")
        if self.scope is not Scope.PROJECT and self.project_id:
            raise MemoryDomainError(
                f"{self.scope.value} 作用域记忆不应带 project_id"
            )
        _check_ratio("importance", self.importance)
        _check_ratio("confidence", self.confidence)
        # subject_key 必须自洽：前缀类型与作用域要和记录字段一致，
        # 否则同一事实会被写成两条不同身份。
        parsed = parse_subject_key(self.subject_key)
        if parsed.memory_type is not self.memory_type:
            raise MemoryDomainError(
                f"subject_key 类型 {parsed.memory_type.value} 与记录类型 "
                f"{self.memory_type.value} 不一致"
            )
        if parsed.scope is not None and parsed.scope is not self.scope:
            raise MemoryDomainError(
                f"subject_key 作用域 {parsed.scope.value} 与记录作用域 "
                f"{self.scope.value} 不一致"
            )


@dataclass(frozen=True)
class MemoryRelation:
    """两条逻辑记忆之间的关系。"""

    from_memory_id: str
    relation_type: RelationType
    to_memory_id: str
    confidence: float = 0.5
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.from_memory_id == self.to_memory_id:
            raise MemoryDomainError("记忆关系不允许指向自身")
        _check_ratio("confidence", self.confidence)


@dataclass(frozen=True)
class MemoryCandidate:
    """待审计的写入候选。

    主模型只能产出这个结构；它能不能变成正式记忆由 write pipeline 决定。

    ``proposed_decision`` 是模型的**提议**，不是最终决定：consolidation 会拿它
    和数据库现状核对（见 ``memory/versioning.py``）。模型可以说"这替代了旧值"，
    但同一 subject 是否真的存在 active 记忆、旧值是不是已验证的，
    只能由 Python 和数据库判定。
    """

    memory_type: MemoryType
    scope: Scope
    subject_key: str
    content: str
    user_id: str
    summary: str | None = None
    project_id: str | None = None
    importance: float = 0.5
    confidence: float = 0.5
    verified: bool = False
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[MemorySource, ...] = ()
    proposed_decision: WriteDecision | None = None

    def __post_init__(self) -> None:
        if not normalize_content(self.content):
            raise MemoryDomainError("候选正文为空")
        validate_type_scope(self.memory_type, self.scope)
        if self.scope is Scope.PROJECT and not self.project_id:
            raise MemoryDomainError("项目作用域候选必须带 project_id")
        if self.scope is not Scope.PROJECT and self.project_id:
            raise MemoryDomainError(
                f"{self.scope.value} 作用域候选不应带 project_id"
            )
        _check_ratio("importance", self.importance)
        _check_ratio("confidence", self.confidence)
        parsed = parse_subject_key(self.subject_key)
        if parsed.memory_type is not self.memory_type:
            raise MemoryDomainError(
                f"subject_key 类型 {parsed.memory_type.value} 与候选类型 "
                f"{self.memory_type.value} 不一致"
            )
        if parsed.scope is not None and parsed.scope is not self.scope:
            raise MemoryDomainError(
                f"subject_key 作用域 {parsed.scope.value} 与候选作用域 "
                f"{self.scope.value} 不一致"
            )
        ensure_verification_supported(self.memory_type, self.verified, self.sources)
        if self.valid_from is not None:
            validate_validity_window(self.valid_from, None)
        if self.proposed_decision is not None:
            try:
                WriteDecision(self.proposed_decision)
            except ValueError as exc:
                raise MemoryDomainError(
                    f"非法写入提议: {self.proposed_decision!r}"
                ) from exc


@dataclass(frozen=True)
class ConsolidationPlan:
    """consolidation 的结论：要做什么、为什么、以谁为目标。

    这是**计划**而不是结果——真正的写入由 ``memory/versioning.py`` 执行。
    决策必须带 reason：候选表里的 ``decision_reason`` 是唯一的事后审计依据。
    """

    decision: WriteDecision
    reason: str
    subject_key: str
    # UPDATE / SUPERSEDE 的目标逻辑记录。
    target_memory_id: str | None = None
    # REJECT 时建议建立的关系（例如"旧值已验证，新值只能先记为矛盾"）。
    suggested_relation: RelationType | None = None

    @property
    def writes(self) -> bool:
        """这条计划是否会产生正式记忆写入。"""

        return self.decision in (
            WriteDecision.ADD,
            WriteDecision.UPDATE,
            WriteDecision.SUPERSEDE,
        )


@dataclass(frozen=True)
class MemoryHit:
    """一次召回结果。分数与命中的通道都保留，便于 trace。"""

    record: MemoryRecord
    version: MemoryVersion
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    exact_match: bool = False


@dataclass(frozen=True)
class MemoryQuery:
    """一次召回请求。作用域过滤是第一等参数，不是可选优化。"""

    text: str
    limit: int = 10
    scopes: tuple[Scope, ...] = ()
    memory_types: tuple[MemoryType, ...] = ()
    as_of: datetime | None = None
    min_confidence: float = 0.0
    include_shared_ops: bool = False

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise MemoryDomainError("limit 必须为正整数")
        _check_ratio("min_confidence", self.min_confidence)
