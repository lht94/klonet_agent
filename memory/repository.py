"""记忆存储的数据库无关契约。

正式运行只提供 PostgreSQL 实现（``memory/postgres.py``）；单元测试用 in-memory
fake，不维护第二套 SQLite 后端——两套 SQL、全文检索和向量实现长期必然漂移
（见计划 §12）。

这里的 Protocol 与命令/查询对象是写入管线、召回器和 MemoryPack 之间唯一的
耦合面：它们只依赖 ``memory/domain.py``，不依赖驱动。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from klonet_agent.memory.domain import (
    MemoryCandidate,
    MemoryHit,
    MemoryQuery,
    MemoryRecord,
    MemorySource,
    MemoryType,
    MemoryVersion,
    Scope,
    WriteDecision,
)


class MemoryRepositoryError(RuntimeError):
    """存储层错误的基类。"""


class RecordNotFoundError(MemoryRepositoryError):
    """按 id 找不到逻辑记忆。"""


class DuplicateVersionError(MemoryRepositoryError):
    """同一记忆已存在内容等价的版本：这是 NOOP 而不是失败。"""

    def __init__(self, existing_version_id: str, existing_version: int):
        super().__init__(
            f"内容哈希重复，已有版本 v{existing_version}（{existing_version_id}）"
        )
        self.existing_version_id = existing_version_id
        self.existing_version = existing_version


class ActiveSubjectConflictError(MemoryRepositoryError):
    """同一 subject_key 已有 active 记忆。

    必须走 UPDATE/SUPERSEDE，不能直接再插一条 active——
    否则同一事实会有两个"当前有效"版本。
    """

    def __init__(self, subject_key: str, existing_memory_id: str):
        super().__init__(f"{subject_key} 已存在 active 记忆（{existing_memory_id}）")
        self.subject_key = subject_key
        self.existing_memory_id = existing_memory_id


class MemoryUnavailableError(MemoryRepositoryError):
    """数据库不可用或超时。

    召回路径必须把它转成"空 MemoryPack + 可观测错误"，不得伪造记忆结果。
    """


class CandidateNotFoundError(MemoryRepositoryError):
    """按 id 找不到写入候选，或该候选已经决策过。

    候选决策只允许发生一次：重复决策应当报错，而不是静默覆盖审计记录。
    """


class RecordNotActiveError(MemoryRepositoryError):
    """目标记忆存在但已不在 active 状态。

    已失效的记忆只能读，不能继续追加版本或再次被替代。
    """


class ScopeViolationError(MemoryRepositoryError):
    """写入内容的租户作用域与当前会话绑定不一致。

    正常情况下数据库的 RLS 会拒绝这种写入；应用层提前报错只是为了给出
    可读的原因，而不是依赖驱动抛出的权限错误。
    """


@dataclass(frozen=True)
class NewRecordCommand:
    """创建一条新的逻辑记忆，同时写入它的第一个版本。

    逻辑记录与首个版本必须同事务创建：只有记录没有版本的状态没有意义。
    """

    user_id: str
    scope: Scope
    memory_type: MemoryType
    subject_key: str
    content: str
    project_id: str | None = None
    summary: str | None = None
    importance: float = 0.5
    confidence: float = 0.5
    verified: bool = False
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[MemorySource, ...] = ()
    lexical_text: str | None = None
    # 迁移和重放场景允许调用方指定 id，用唯一约束保证幂等。
    record_id: str | None = None
    version_id: str | None = None


@dataclass(frozen=True)
class NewVersionCommand:
    """为已有逻辑记忆追加一个不可变版本。"""

    memory_id: str
    content: str
    summary: str | None = None
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[MemorySource, ...] = ()
    lexical_text: str | None = None
    version_id: str | None = None


@dataclass(frozen=True)
class CandidateRecord:
    """已落库的写入候选及其决策（审计视图）。"""

    id: str
    idempotency_key: str
    user_id: str
    project_id: str | None
    source_event_range: Mapping[str, Any]
    candidate_payload: Mapping[str, Any]
    decision: WriteDecision | None = None
    decision_reason: str | None = None
    processed_at: datetime | None = None
    created_at: datetime | None = None


class MemoryRepository(Protocol):
    """记忆存储契约。

    实现必须保证：所有查询都绑定已通过 ``Tenant`` 固定的作用域；
    ``supersede`` 在同一事务里建立关系并失效旧记录；正文事务与 embedding
    队列解耦（正文先提交，embedding 由 worker 幂等补算）。
    """

    # --- 写入 ---

    def add_candidate(
        self,
        candidate: MemoryCandidate,
        *,
        idempotency_key: str,
        source_event_range: Mapping[str, Any],
    ) -> str:
        """保存待审计的写入候选，返回候选 id。

        同一 ``idempotency_key`` 重复提交必须返回同一条候选，不新建。
        """

        ...

    def record_decision(
        self,
        candidate_id: str,
        decision: WriteDecision,
        *,
        reason: str,
        processed_at: datetime | None = None,
    ) -> None:
        """记录候选的最终决策与原因。"""

        ...

    def add_record(self, command: NewRecordCommand) -> MemoryRecord:
        """创建新的逻辑记忆及其首个版本。"""

        ...

    def add_version(self, command: NewVersionCommand) -> MemoryVersion:
        """为已有记忆创建不可变新版本，并把 active_version 指过去。"""

        ...

    def supersede(
        self,
        old_memory_id: str,
        new_memory_id: str,
        *,
        confidence: float = 1.0,
        reason: str | None = None,
    ) -> None:
        """在同一事务内建立 supersedes 关系并失效旧记忆。"""

        ...

    def mark_expired(self, memory_id: str, valid_to: datetime) -> None:
        """把记忆标记为过期，保留全部历史版本。"""

        ...

    def add_source(self, version_id: str, source: MemorySource) -> None:
        """给版本追加一条来源。重复来源必须幂等。"""

        ...

    def add_relation(
        self,
        from_memory_id: str,
        relation_type: str,
        to_memory_id: str,
        *,
        confidence: float = 0.5,
    ) -> None:
        """建立两条记忆之间的关系。"""

        ...

    def enqueue_embedding(
        self, version_id: str, embedding_profile_id: str
    ) -> None:
        """把版本登记进 embedding outbox（待 worker 生成向量）。"""

        ...

    def set_embedding(
        self,
        version_id: str,
        embedding: Sequence[float],
        *,
        embedding_model: str,
        embedding_version: str,
    ) -> None:
        """写入向量并闭环 outbox 状态。

        这是 embedding worker（阶段 4）的写入口；正文事务自己不调用它，
        以保持"正文先提交、向量异步补算"的解耦。模型与版本必须一起给，
        否则向量无法被追溯是哪个 profile 算出来的。
        """

        ...

    # --- 读取 ---

    def get_record(self, memory_id: str) -> MemoryRecord | None:
        """按 id 读取逻辑记忆（含 active 版本）。"""

        ...

    def get_active(self, memory_id: str) -> MemoryRecord | None:
        """读取当前有效版本；已失效或被替代的记忆返回 None。"""

        ...

    def get_version(self, version_id: str) -> MemoryVersion | None:
        """按 id 读取不可变版本。"""

        ...

    def find_active_by_subject(self, subject_key: str) -> MemoryRecord | None:
        """按 subject_key 找当前 active 记忆。"""

        ...

    def list_sources(self, version_id: str) -> list[MemorySource]:
        """读取版本的来源列表。"""

        ...

    def list_versions(self, memory_id: str) -> list[MemoryVersion]:
        """按版本号升序读取全部历史版本。"""

        ...

    def list_candidates(
        self, *, decision: WriteDecision | None = None, limit: int = 100
    ) -> list[CandidateRecord]:
        """列出候选；``decision=None`` 表示只看未处理的。"""

        ...

    def search(
        self,
        query: MemoryQuery,
        *,
        query_embedding: Sequence[float] | None = None,
    ) -> list[MemoryHit]:
        """执行已绑定作用域的混合检索。

        硬过滤（user/project/scope/status/valid time/confidence）在 SQL 里完成；
        ``query_embedding`` 为空时退化为全文与精确匹配，不伪造语义结果。
        """

        ...
