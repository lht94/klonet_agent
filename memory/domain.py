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


class MemoryDomainError(ValueError):
    """领域模型校验失败。调用方必须修正输入，不允许静默兜底。"""


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
    """一条不可变版本。正文、有效时间、embedding 都挂在这里。"""

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

    def __post_init__(self) -> None:
        if not normalize_content(self.content):
            raise MemoryDomainError("候选正文为空")
        _check_ratio("importance", self.importance)
        _check_ratio("confidence", self.confidence)
        parse_subject_key(self.subject_key)


@dataclass(frozen=True)
class WriteDecisionResult:
    """consolidation 的结果。五种决策之一，必须带原因。"""

    decision: WriteDecision
    reason: str
    candidate_id: str | None = None
    record_id: str | None = None
    version_id: str | None = None


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
