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
from datetime import datetime, timezone
from typing import Any, Protocol

from klonet_agent.memory.domain import (
    MemoryCandidate,
    MemoryHit,
    MemoryQuery,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
    MemoryType,
    MemoryVersion,
    RelationType,
    Scope,
    WriteDecision,
)

# 阶段 1 只允许一个 active embedding profile，名字固定（见
# ``migrations/0001_init.sql`` 里 memory_embedding_outbox 的复合主键）。
# 换维度要新建 profile 并在后台回填，不原地改已有 vector 的维度（计划 §7.3）。
DEFAULT_EMBEDDING_PROFILE_ID = "default"

# 与 ``migrations/0001_init.sql`` 的 ``embedding vector(1024)`` 一致。
# 放在契约侧而不是实现侧，因为"这个维度"是 schema 事实，worker 与召回器
# 都要用它做校验，不该各自去 import 具体数据库实现。
EMBEDDING_DIMENSIONS = 1024


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
    verified: bool = False
    # None 表示"不改动逻辑记录上的置信度"；给值时只允许抬高不允许降低——
    # 置信度是随证据积累单调上升的量，一次描述改写不应该让它倒退。
    confidence: float | None = None


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


@dataclass(frozen=True)
class PendingEmbedding:
    """一条待生成向量的任务，连同正文一起取出。

    正文随任务一起返回，避免 worker 拿到 id 后再查一次版本——那会让"读取要
    嵌入的内容"和"内容在返回后被改写"之间出现窗口（虽然版本不可变，但多一次
    查询就多一次可能落在不同事务里的机会）。
    """

    version_id: str
    memory_id: str
    content: str
    attempt_count: int = 0
    embedding_profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID


@dataclass(frozen=True)
class EmbeddingOutboxStats:
    """embedding 队列的可观测快照。

    ``coverage`` 是"已算出向量的版本占比"，也是判断能否依赖向量通道、以及
    要不要建 HNSW 的依据（计划 §12：先测量再决定）。

    ``oldest_pending_at`` 是**尚未闭环的最老任务**的创建时间（04 计划 §6.1
    "最老 pending" 指标）。只看 ``outstanding`` 数量无法区分"队列在动但积压
    很久"和"队列刚有活"——前者才是要告警的。
    """

    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0
    oldest_pending_at: datetime | None = None

    @property
    def outstanding(self) -> int:
        """尚未闭环的任务数：待处理 + 正在处理（含崩溃后待重领的）。"""

        return self.pending + self.processing

    @property
    def total(self) -> int:
        return self.pending + self.processing + self.completed + self.failed

    @property
    def coverage(self) -> float:
        """向量覆盖率；队列为空时定义为 1.0（没有欠账）。"""

        if self.total == 0:
            return 1.0
        return self.completed / self.total

    def oldest_pending_age_seconds(self, *, now: datetime | None = None) -> float | None:
        """最老未闭环任务的年龄（秒）；队列为空时返回 ``None``。

        ``None`` 与 ``0.0`` 语义不同：``None`` 表示"没有积压"，``0.0`` 表示
        "刚进来一条"。健康判定必须区分这两者，否则空队列会被算成"存在 0 秒
        的积压"。
        """

        if self.oldest_pending_at is None:
            return None
        moment = now or datetime.now(timezone.utc)
        delta = moment - self.oldest_pending_at
        return max(0.0, float(delta.total_seconds()))


@dataclass(frozen=True)
class ExpiredCandidate:
    """一条"``valid_to`` 已过、但状态仍是 active"的记忆。

    正常情况下 ``mark_expired`` 会同时改状态与 ``valid_to``；出现这种分歧意味着
    有历史遗留（直接改过 ``valid_to`` 的写入路径），它们会一直占着 "active" 的
    名字，让审计数字对不上。
    """

    memory_id: str
    user_id: str
    project_id: str | None
    scope: Scope
    valid_to: datetime


@dataclass(frozen=True)
class ExpiredCandidatePage:
    """keyset 分页的一页。

    ``next_cursor`` 为 ``None`` 表示已到末页。cursor 由
    :func:`encode_expired_cursor` 生成，形态是稳定的 ``(valid_to, memory_id)``。
    """

    items: tuple[ExpiredCandidate, ...] = ()
    next_cursor: str | None = None


@dataclass(frozen=True)
class ArchivedExpiredBatch:
    """一次 :meth:`MemoryRepository.archive_expired_batch` 的结果。"""

    archived: int = 0
    memory_ids: tuple[str, ...] = ()
    outbox_deferred: int = 0


@dataclass(frozen=True)
class DeletedBacklog:
    """待物理清理的规模（purge dry-run）。"""

    count: int = 0
    oldest_deleted_at: datetime | None = None
    estimated_bytes: int = 0
    # 这一批具体是哪些（上限 = 查询时给的 ``limit``）。purge 用它做"删完抽查
    # 还能不能被召回"——只知道数量就无从抽查。
    memory_ids: tuple[str, ...] = ()


_EXPIRED_CURSOR_SEPARATOR = "|"


def encode_expired_cursor(valid_to: datetime, memory_id: str) -> str:
    """把 keyset 位置编码成 cursor。

    形态刻意做得**可读**（``<ISO8601>|<uuid>`` 而不是 base64）：分页出错时
    运维能一眼看出"停在哪条、哪个时间点"，而不是先写一段解码脚本。
    ``valid_to`` 统一转成 UTC ISO8601——不同时区表示同一个瞬间必须产生
    同一个 cursor，否则断点会错位。
    """

    if valid_to is None:
        raise ValueError("cursor 的 valid_to 不能为空")
    if not str(memory_id or "").strip():
        raise ValueError("cursor 的 memory_id 不能为空")
    moment = valid_to.astimezone(timezone.utc) if valid_to.tzinfo else valid_to.replace(
        tzinfo=timezone.utc
    )
    return f"{moment.isoformat()}{_EXPIRED_CURSOR_SEPARATOR}{memory_id}"


def decode_expired_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    """解析 cursor；无法解析时返回 ``None``（从头开始，绝不"猜一个位置"）。

    与阶段 3 的租户 cursor 同一取舍：宁可多扫一页，也不要因为 cursor 不认识
    而跳过一批过期记忆——后者会让"过期记录 2 小时内归档"的 SLO 静默失守。
    """

    if not cursor:
        return None
    if _EXPIRED_CURSOR_SEPARATOR not in cursor:
        return None
    raw_moment, _, raw_id = cursor.partition(_EXPIRED_CURSOR_SEPARATOR)
    memory_id = raw_id.strip()
    if not memory_id:
        return None
    try:
        moment = datetime.fromisoformat(raw_moment.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc), memory_id


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

    def add_candidate_payload(
        self,
        payload: Mapping[str, Any],
        *,
        user_id: str,
        project_id: str | None,
        idempotency_key: str,
        source_event_range: Mapping[str, Any],
    ) -> str:
        """用**原始 payload** 登记候选，不经过 ``MemoryCandidate`` 校验。

        给策略层用：被策略拒绝的候选往往构造不出合法的领域对象
        （作用域越权、来源不可核对、正文过短……），但计划 §7.1 要求
        "拒绝只记录在 candidate 表" —— 审计要留下"模型提过什么、我们为什么拒"。
        所以这里允许直接落 payload。

        ⚠️ 调用方**必须**保证 payload 里没有敏感原文（口令/密钥/连接串）。
        含敏感的候选只应进 trace，不应进任何数据库表。
        与 :meth:`add_candidate` 一致，同一 ``idempotency_key`` 幂等。
        """

        ...

    def processed_source_ranges(
        self, *, limit: int = 1000
    ) -> list[Mapping[str, Any]]:
        """返回已登记过候选的事件区间（含已决策的）。

        计划 §7.2 要求补扫时"不能重复处理已有 source range"，两份状态容易漂移，
        所以直接把候选表当台账用——它本来就是"每个区间处理过一次"的记录。
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
        """在同一事务内建立 supersedes 关系并失效旧记忆。

        要求新旧两条记忆**已经存在**。当新值会占用同一个 ``subject_key`` 时用不了
        这条路径（插入新记录会撞上"一个 subject 只能有一条 active"的唯一索引），
        要用 :meth:`replace_active`。
        """

        ...

    def replace_active(
        self,
        old_memory_id: str,
        command: NewRecordCommand,
        *,
        relation_confidence: float = 1.0,
        reason: str | None = None,
    ) -> MemoryRecord:
        """用一条新记忆原子替代当前的 active 记忆（同一 subject_key）。

        这是 SUPERSEDE 决策的落地点。计划 §6.5 的例子（subject 不变、有效值变化）
        要求新旧两条记忆拥有**同一个** subject_key，因此不能拆成
        "先 add_record 再 supersede"：中间那一步会让库里同时存在两条 active。
        实现必须在同一事务里先让旧记录离开 active，再插入新记录。

        ``command.subject_key`` 必须与被替代的记忆一致；旧记忆的当前版本结束
        有效期，新记忆成为该 subject 唯一的 active 版本，两者之间建立
        ``supersedes`` 关系（``reason`` 落在关系的 reason 列，便于审计）。
        """

        ...

    def mark_expired(self, memory_id: str, valid_to: datetime) -> None:
        """把记忆标记为过期，保留全部历史版本。"""

        ...

    def add_source(self, version_id: str, source: MemorySource) -> None:
        """给版本追加一条来源。重复来源必须幂等。"""

        ...

    def attach_sources(
        self,
        memory_id: str,
        sources: Sequence[MemorySource],
        *,
        confidence: float | None = None,
        verified: bool = False,
    ) -> MemoryVersion:
        """把新来源挂到**当前 active 版本**上，不新增版本。

        这是 plan §7.1 里 UPDATE 的一种形态："同一事实含义未变，只补充来源或
        置信度"。内容等价时不能走 ``add_version``——版本表上有
        ``(memory_id, content_hash)`` 唯一约束，同一段正文在一个记忆里只允许
        存在一个版本，数据库会在那里挡住。

        ``confidence`` 给定时只抬高不降低；``verified=True`` 需要把这批来源与
        已有来源合起来看仍然成立（校验规则见 domain 层）。
        返回刷新后的 active 版本。
        """

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
    ) -> bool:
        """写入向量并闭环 outbox 状态。

        返回 ``True`` 表示已写入；``False`` 表示跳过——记录已被删除（04 计划
        §6.1"删除记忆不会复活"）。把删除当成"跳过"而不是"失败"，是为了不让
        正常的删除在 worker 日志里留下假失败。

        这是 embedding worker（阶段 4）的写入口；正文事务自己不调用它，
        以保持"正文先提交、向量异步补算"的解耦。模型与版本必须一起给，
        否则向量无法被追溯是哪个 profile 算出来的。
        """

        ...

    # --- embedding outbox（阶段 4）---

    def claim_pending_embeddings(
        self,
        *,
        limit: int = 20,
        lease_seconds: float = 300.0,
        profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID,
        now: datetime | None = None,
    ) -> list[PendingEmbedding]:
        """领取待生成向量的任务并标记为 ``processing``。

        领取即把 ``attempt_count`` 加一，所以进程在生成途中崩溃也会留下痕迹，
        不会无限重试。领取同时写入租约（``next_attempt_at``）：崩溃留下的
        ``processing`` 记录在租约到期后可以被重新领取，不需要人工介入。

        多个 worker 并发时用行锁跳过已被别人领走的记录（``SKIP LOCKED``），
        不互相阻塞。
        """

        ...

    def mark_embedding_failed(
        self,
        version_id: str,
        error: str,
        *,
        retry_at: datetime | None,
        profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID,
    ) -> None:
        """记录一次生成失败。

        ``retry_at`` 指出下次可重试时间（退避）；为 ``None`` 表示不再自动重试，
        任务进入 ``failed`` 终态。终态只表示"放弃自动重试"，不表示数据不可用——
        该版本仍然会被全文和精确通道召回，只是少了语义通道。
        """

        ...

    def embedding_outbox_stats(
        self, *, profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID
    ) -> EmbeddingOutboxStats:
        """按状态统计 outbox 队列（只统计当前租户可见的版本）。"""

        ...

    # --- 删除权与生命周期（阶段 7）---

    def delete_memory(
        self,
        memory_id: str,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """逻辑删除一条记忆，并**同步清除它的向量**。

        三件事必须一起做，缺一条就会出现"删了但还能召回"：

        1. 记录状态转 ``deleted``（当前视图立刻不再返回它）；
        2. 当前版本结束有效期（``as_of`` 视图也不再把它当成"当时有效"）；
        3. **清掉向量与 outbox 任务**——否则语义通道还会命中它，而接口上
           看起来已经删了。这是"删除权"最容易漏掉的一条。
        """

        ...

    def purge_deleted(
        self, *, older_than: datetime, limit: int = 1000
    ) -> int:
        """物理清理已逻辑删除且超过保留期的记录，返回删除条数。

        只删 ``status='deleted'`` 且 ``updated_at <= older_than`` 的记录；
        版本、来源、关系随外键级联删除。**不可逆**，所以门槛（保留期）由调用方
        显式给出，不在这里给默认值。
        """

        ...

    # --- 过期归档与物理清理（04 计划阶段 4）--------------------------------- #

    def list_expired_candidates(
        self,
        *,
        cutoff: datetime,
        cursor: str | None = None,
        batch_size: int = 500,
    ) -> "ExpiredCandidatePage":
        """列出"``valid_to`` 已过但仍为 active"的记忆，**keyset 分页**。

        只读，不改变任何状态——dry-run、运维巡检和 ExpirationJob 的"取一批"
        都走这里。

        * ``cutoff``：判定门槛（通常是 ``now``）。条件与
          :meth:`archive_expired_batch` 完全一致，避免"列出的是 A、改的是 B"。
        * ``cursor``：上一页返回的 ``next_cursor``。keyset 字段是稳定的
          ``(valid_to, memory_id)``，**不能用 offset**——归档会把行移出结果集，
          offset 分页必然漏行。
        * 跨租户硬过滤由实现层的 ``_record_scope()`` + RLS 保证；调用方拿到的
          永远是当前绑定租户的行。
        """

        ...

    def archive_expired_batch(
        self,
        *,
        memory_ids: Sequence[str],
        cutoff: datetime,
        outbox_retry_after: datetime | None = None,
    ) -> "ArchivedExpiredBatch":
        """把给定的一批记忆从 active 归档成 expired（**条件更新**）。

        SQL 里带上 ``status='active' AND valid_to <= cutoff``：即便传进来的 id
        在这一瞬间已经过期/被删/状态已变，也不会被"再归档一次"。所以这个方法是
        幂等且并发安全的——两个 worker 同时跑最多各改一半，不会互相覆盖。

        ``outbox_retry_after`` 非空时，**同一个事务**里把这些记录的
        ``embedding_outbox`` 未闭环条目推到该时刻：归档后它们的向量没有意义，
        但立刻删除 outbox 行会掩盖"曾经欠过一次"的事实。推后而不是删除，
        配合阶段 3 的 ``claim_pending_embeddings`` 的 ``r.status <> 'deleted'``
        谓词（expired 仍会被领到，所以推后是为了让它们排在 deleted 之前不被
        无谓计算）。
        """

        ...

    def inspect_deleted_backlog(
        self, *, older_than: datetime, limit: int = 1000
    ) -> "DeletedBacklog":
        """查"已过保留期、待物理清理"的规模（purge dry-run 用）。

        与 :meth:`purge_deleted` 用**同一个谓词**，所以 dry-run 报的数字就是
        真跑会删的量。``estimated_bytes`` 用正文长度估算（不落盘、不精确），
        只用来判断"这次要不要在低峰期跑"。
        """

        ...

    def list_records(
        self,
        *,
        limit: int = 100,
        memory_type: MemoryType | None = None,
        scope: Scope | None = None,
        status: str | None = None,
    ) -> list[MemoryRecord]:
        """管理视图：列出当前租户的记忆（含已失效/已删除的，供审计与删除用）。"""

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

    def list_relations(
        self,
        memory_id: str,
        *,
        relation_type: RelationType | None = None,
    ) -> list[MemoryRelation]:
        """读取与一条记忆相关的关系，两个方向都返回。

        召回需要它来标注冲突：一条命中的事实若被别的记忆
        ``contradicts``，必须在结果里显式说出来，而不是让模型自己猜
        （计划 §6.5 规则 5：模型不得静默合并互斥事实）。
        """

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
