"""阶段 4（embedding outbox 与混合检索）的测试。

分三部分：

* **降级与错误分类**（纯离线）：查询向量拿不到时是否明确降级、数据库异常的
  SQLSTATE 分类、rerank 失败回退、冲突标注。这几条是"不伪造结果"的具体落点。
* **worker 行为**（纯离线）：退避纯函数、失败分类（重试 vs 终态）、幂等与
  统计。用一个只实现 outbox 方法的假仓库，不依赖数据库。
* **真库测试**（没有 DSN 时整块 skip）：正文与 outbox 同事务、worker 端到端与
  幂等、三通道在真 SQL 上的实际命中、作用域与有效期硬过滤、冲突关系抬高到
  召回结果里，以及一次 exact 向量扫描的规模测量（计划 §12 要求"先测量再决定
  是否建 HNSW"）。

跑法：

    ./scripts/pg_local.sh up
    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_retriever.py -q
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from time import perf_counter
from uuid import UUID, uuid4

import pytest

from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    MemoryHit,
    MemoryQuery,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
    MemoryType,
    MemoryVersion,
    RelationType,
    Scope,
    SourceType,
    Tenant,
    content_hash,
)
from klonet_agent.memory.embedding_worker import (
    EmbeddingContentError,
    EmbeddingWorker,
    backoff_delay,
    is_permanent_failure,
)
from klonet_agent.memory.postgres import PostgresMemoryRepository
from klonet_agent.memory.repository import (
    EMBEDDING_DIMENSIONS,
    NewRecordCommand,
    PendingEmbedding,
)
from klonet_agent.memory.retriever import (
    MemoryRetrievalError,
    MemoryRetriever,
    default_rerank_document,
    sqlstate_of,
)

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"

ALICE = Tenant(user_id="alice", project_id="demo")
BOB = Tenant(user_id="bob", project_id="demo")

FACT_SUBJECT = "fact:project:demo:runtime_version"
PY311 = "本项目运行时要求 Python 3.11"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #


def _src(kind: SourceType = SourceType.USER_STATEMENT, sid: str | None = None) -> MemorySource:
    return MemorySource(
        source_type=kind,
        source_id=sid or f"rows-{uuid4().hex[:8]}",
        observed_at=_now(),
        source_excerpt="用户在对话里明确说明",
    )


def _record(
    *,
    content: str = PY311,
    subject_key: str = FACT_SUBJECT,
    memory_type: MemoryType = MemoryType.FACT,
    scope: Scope = Scope.PROJECT,
    user_id: str = ALICE.user_id,
    project_id: str | None = "demo",
    memory_id: str | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        id=memory_id or str(uuid4()),
        user_id=user_id,
        scope=scope,
        project_id=project_id if scope is Scope.PROJECT else None,
        memory_type=memory_type,
        subject_key=subject_key,
        importance=0.6,
        confidence=0.9,
    )


def _hit(
    *,
    content: str = PY311,
    subject_key: str = FACT_SUBJECT,
    memory_id: str | None = None,
    reasons: tuple[str, ...] = ("lexical",),
) -> MemoryHit:
    record = _record(
        content=content, subject_key=subject_key, memory_id=memory_id
    )
    version = MemoryVersion(
        id=str(uuid4()),
        memory_id=record.id,
        version=1,
        content=content,
        observed_at=_now(),
        valid_from=_now(),
        content_hash=content_hash(content),
        lexical_text="本项目 运行时 要求",
    )
    return MemoryHit(record=record, version=version, score=1.0, reasons=reasons)


def _new_record(
    *,
    subject_key: str = FACT_SUBJECT,
    content: str = PY311,
    memory_type: MemoryType = MemoryType.FACT,
    scope: Scope = Scope.PROJECT,
    project_id: str | None = "demo",
    user_id: str = ALICE.user_id,
    confidence: float = 0.9,
    valid_from: datetime | None = None,
) -> NewRecordCommand:
    return NewRecordCommand(
        user_id=user_id,
        scope=scope,
        memory_type=memory_type,
        subject_key=subject_key,
        content=content,
        project_id=project_id,
        sources=(_src(),),
        confidence=confidence,
        valid_from=valid_from,
    )


def _deterministic_embedder(dimensions: int = EMBEDDING_DIMENSIONS):
    """把文本散列成确定性的单位向量。

    不调真实 embedding 服务：这些测试要证明的是"向量通道接得上、能命中"，
    而不是某家供应商的语义质量。确定性意味着同一段文本必然得到同一个向量，
    于是"用原文查询能不能命中"成为一个可以断言的事实。
    """

    def embed(text: str) -> tuple[float, ...]:
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        values = [digest[index % len(digest)] / 255.0 for index in range(dimensions)]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return tuple(value / norm for value in values)

    return embed


# --------------------------------------------------------------------------- #
# 降级矩阵：查询向量拿不到时必须是"可见的降级"，不是静默少一路
# --------------------------------------------------------------------------- #


class _FakeRepository:
    """只实现召回路径需要的方法，并记录调用。

    ``search`` 可以被要求抛指定异常，用来验证 SQLSTATE 分类。
    """

    def __init__(
        self,
        hits: list[MemoryHit] | None = None,
        relations: dict[str, list[MemoryRelation]] | None = None,
        error_factory=None,
    ) -> None:
        self._hits = list(hits or [])
        self._relations = dict(relations or {})
        self._error_factory = error_factory
        self.search_vectors: list[object] = []
        self.relation_calls: list[tuple[str, object]] = []

    def search(self, query: MemoryQuery, *, query_embedding=None):
        if self._error_factory is not None:
            raise self._error_factory()
        self.search_vectors.append(query_embedding)
        return list(self._hits)

    def list_relations(self, memory_id: str, *, relation_type=None):
        self.relation_calls.append((memory_id, relation_type))
        return list(self._relations.get(memory_id, ()))


def test_without_embedder_semantic_channel_is_reported_as_skipped() -> None:
    repository = _FakeRepository([_hit()])
    report = MemoryRetriever(repository).retrieve(MemoryQuery(text="运行时版本"))

    assert [hit.version.content for hit in report.hits] == [PY311]
    assert report.degraded == ("no_embedder:semantic_channel_skipped",)
    assert report.semantic_used is False
    assert repository.search_vectors == [None]


def test_embedder_failure_degrades_with_reason_and_keeps_lexical_results() -> None:
    def broken(_text: str):
        raise TimeoutError("上游超时")

    repository = _FakeRepository([_hit()])
    report = MemoryRetriever(repository, embedder=broken).retrieve(
        MemoryQuery(text="运行时版本")
    )

    assert report.degraded == ("embed_failed:TimeoutError",)
    # 关键：结果还在（关键词通道的），只是标注了语义没生效。
    assert len(report.hits) == 1
    assert repository.search_vectors == [None]


@pytest.mark.parametrize(
    "embedder,expected",
    [
        (lambda _text: (), "embed_empty"),
        (lambda _text: None, "embed_empty"),
        (lambda _text: ("a", "b"), "embed_not_numeric"),
        (lambda _text: (0.1, 0.2, 0.3), "embed_dimension_mismatch:3!=1024"),
    ],
)
def test_malformed_query_vectors_degrade_instead_of_crashing(embedder, expected) -> None:
    repository = _FakeRepository([_hit()])
    report = MemoryRetriever(repository, embedder=embedder).retrieve(
        MemoryQuery(text="运行时版本")
    )
    assert report.degraded == (expected,)
    assert repository.search_vectors == [None]


def test_valid_query_vector_is_passed_through() -> None:
    embedder = _deterministic_embedder()
    repository = _FakeRepository([_hit()])
    report = MemoryRetriever(repository, embedder=embedder).retrieve(
        MemoryQuery(text="运行时版本")
    )

    assert report.degraded == ()
    assert report.semantic_used is True
    assert repository.search_vectors == [embedder("运行时版本")]


# --------------------------------------------------------------------------- #
# 数据库错误分类
# --------------------------------------------------------------------------- #


class _FakeDatabaseError(Exception):
    def __init__(self, message: str, state: str | None) -> None:
        super().__init__(message)
        self.sqlstate = state


def test_schema_level_error_is_raised_not_degraded() -> None:
    """pgvector 缺失是部署错误：降级成全文会让每次召回都少一路而没人察觉。"""

    repository = _FakeRepository(
        error_factory=lambda: _FakeDatabaseError(
            'type "vector" does not exist', "42704"
        )
    )
    with pytest.raises(MemoryRetrievalError) as excinfo:
        MemoryRetriever(repository).retrieve(MemoryQuery(text="运行时版本"))
    assert "42704" in str(excinfo.value)
    assert "迁移" in str(excinfo.value)


def test_connection_error_returns_empty_report_with_observable_error() -> None:
    """数据库整体不可用时，"没有相关记忆"是诚实的回答，但必须留痕。"""

    repository = _FakeRepository(
        error_factory=lambda: _FakeDatabaseError("connection refused", "08006")
    )
    report = MemoryRetriever(repository).retrieve(MemoryQuery(text="运行时版本"))

    assert report.hits == ()
    assert report.errors == ("database_unavailable:_FakeDatabaseError",)
    assert report.ok is False


def test_unknown_exception_propagates_so_bugs_are_not_hidden() -> None:
    repository = _FakeRepository(error_factory=lambda: ValueError("编程错误"))
    with pytest.raises(ValueError):
        MemoryRetriever(repository).retrieve(MemoryQuery(text="运行时版本"))


def test_sqlstate_of_reads_both_driver_attribute_names() -> None:
    class Legacy(Exception):
        pgcode = "42P01"

    assert sqlstate_of(_FakeDatabaseError("x", "42704")) == "42704"
    assert sqlstate_of(Legacy("x")) == "42P01"
    assert sqlstate_of(RuntimeError("x")) is None


# --------------------------------------------------------------------------- #
# rerank：可选增强，失败必须稳定回退
# --------------------------------------------------------------------------- #


class _FakeReranker:
    def __init__(self, items=None, *, error=None, available: bool = True) -> None:
        self._items = items if items is not None else []
        self._error = error
        self.available = available
        self.documents: list[list[str]] = []

    def rerank_documents(self, query, documents, *, top_n):
        self.documents.append(list(documents))
        if self._error is not None:
            raise self._error
        return list(self._items)


def _rerank_item(index: int, score: float):
    from klonet_agent.llm.reranker import RerankItem

    return RerankItem(index, score)


def test_rerank_reorders_by_score_when_applied() -> None:
    hits = [_hit(content=f"内容 {index}") for index in range(3)]
    reranker = _FakeReranker([_rerank_item(0, 0.1), _rerank_item(1, 0.9), _rerank_item(2, 0.5)])
    report = MemoryRetriever(
        _FakeRepository(hits), reranker=reranker
    ).retrieve(MemoryQuery(text="运行时版本"))

    assert report.rerank == "applied"
    assert [hit.version.content for hit in report.hits] == ["内容 1", "内容 2", "内容 0"]


def test_rerank_failure_falls_back_to_recall_order() -> None:
    hits = [_hit(content=f"内容 {index}") for index in range(3)]
    reranker = _FakeReranker(error=RuntimeError("rerank 服务 500"))
    report = MemoryRetriever(
        _FakeRepository(hits), reranker=reranker
    ).retrieve(MemoryQuery(text="运行时版本"))

    assert report.rerank == "fallback:RuntimeError"
    assert [hit.version.content for hit in report.hits] == ["内容 0", "内容 1", "内容 2"]


def test_partial_rerank_response_keeps_unscored_candidates() -> None:
    """rerank 只给部分候选打分时，没分的不丢，只是排在后面。"""

    hits = [_hit(content=f"内容 {index}") for index in range(3)]
    reranker = _FakeReranker([_rerank_item(2, 0.9)])
    report = MemoryRetriever(
        _FakeRepository(hits), reranker=reranker
    ).retrieve(MemoryQuery(text="运行时版本"))

    assert report.rerank == "applied"
    assert [hit.version.content for hit in report.hits] == ["内容 2", "内容 0", "内容 1"]


def test_rerank_is_skipped_without_reranker_or_credentials() -> None:
    hits = [_hit()]

    no_reranker = MemoryRetriever(_FakeRepository(hits)).retrieve(MemoryQuery(text="x"))
    assert no_reranker.rerank == "skipped:no_reranker"

    missing = _FakeReranker(available=False)
    no_credentials = MemoryRetriever(
        _FakeRepository(hits), reranker=missing
    ).retrieve(MemoryQuery(text="x"))
    assert no_credentials.rerank == "fallback:missing_credentials"
    assert missing.documents == []

    empty = MemoryRetriever(_FakeRepository([]), reranker=_FakeReranker()).retrieve(
        MemoryQuery(text="x")
    )
    assert empty.rerank == "skipped:no_candidates"


def test_rerank_documents_are_built_from_memory_fields() -> None:
    """记忆渲染成 reranker 输入时要带上主体键——它是向量不敏感的精确证据。"""

    document = default_rerank_document(_hit())
    assert "subject: fact:project:demo:runtime_version" in document
    assert "type: fact" in document
    assert "scope: project" in document
    assert PY311 in document


# --------------------------------------------------------------------------- #
# 冲突标注：互斥事实必须说出来
# --------------------------------------------------------------------------- #


def test_contradicting_hit_is_annotated_without_changing_order() -> None:
    first, second = _hit(content="端口是 8080"), _hit(content="端口是 9090")
    relation = MemoryRelation(
        from_memory_id=first.record.id,
        relation_type=RelationType.CONTRADICTS,
        to_memory_id=second.record.id,
    )
    repository = _FakeRepository(
        [first, second], relations={first.record.id: [relation]}
    )
    report = MemoryRetriever(repository).retrieve(MemoryQuery(text="端口"))

    assert "conflict:contradicts" in report.hits[0].reasons
    assert "conflict:contradicts" not in report.hits[1].reasons
    assert report.conflict_ids == (first.record.id,)
    # 排序没有被冲突改变：顺序仍是召回顺序。
    assert [hit.record.id for hit in report.hits] == [first.record.id, second.record.id]


def test_relation_lookup_failure_does_not_lose_results() -> None:
    class ExplodingRelations(_FakeRepository):
        def list_relations(self, memory_id: str, *, relation_type=None):
            raise RuntimeError("关系表查询失败")

    report = MemoryRetriever(ExplodingRelations([_hit()])).retrieve(
        MemoryQuery(text="运行时版本")
    )
    assert len(report.hits) == 1
    assert report.conflict_ids == ()


# --------------------------------------------------------------------------- #
# worker：退避与失败分类（纯函数）
# --------------------------------------------------------------------------- #


def test_backoff_delay_grows_exponentially_and_caps() -> None:
    delays = [
        backoff_delay(attempt, base_seconds=30.0, max_seconds=600.0)
        for attempt in range(1, 7)
    ]
    assert delays == [30.0, 60.0, 120.0, 240.0, 480.0, 600.0]


def test_backoff_delay_normalizes_attempts_below_one() -> None:
    assert backoff_delay(0, base_seconds=5.0, max_seconds=100.0) == 5.0
    assert backoff_delay(-3, base_seconds=5.0, max_seconds=100.0) == 5.0


def test_is_permanent_failure_reads_only_the_marker() -> None:
    from klonet_agent.llm.embeddings import EmbeddingDimensionMismatch

    assert is_permanent_failure(EmbeddingContentError("空向量")) is True
    assert is_permanent_failure(EmbeddingDimensionMismatch("维度不对")) is True
    assert is_permanent_failure(RuntimeError("网络抖动")) is False


# --------------------------------------------------------------------------- #
# worker：用假仓库验证行为
# --------------------------------------------------------------------------- #


class _FakeOutboxRepository:
    """只实现 worker 用到的四个方法。

    ``claim`` 刻意镜像真实现的语义：返回的 ``attempt_count`` 是"本次是第几次
    尝试"（真实现是在领取时就 +1），否则"连续失败 N 次后放弃"这条规则在测试里
    永远触发不到。
    """

    def __init__(self, items=()) -> None:
        self.queue = list(items)
        self.claim_calls: list[dict] = []
        self.writes: list[tuple] = []
        self.failures: list[tuple] = []
        self.set_embedding_error: BaseException | None = None
        self.mark_error: BaseException | None = None

    def claim_pending_embeddings(
        self, *, limit=20, lease_seconds=300.0, profile_id="default", now=None
    ):
        self.claim_calls.append(
            {"limit": limit, "lease_seconds": lease_seconds, "profile_id": profile_id}
        )
        batch = self.queue[:limit]
        self.queue = self.queue[limit:]
        return [replace(item, attempt_count=item.attempt_count + 1) for item in batch]

    def set_embedding(
        self, version_id, embedding, *, embedding_model, embedding_version
    ) -> None:
        if self.set_embedding_error is not None:
            raise self.set_embedding_error
        self.writes.append(
            (version_id, tuple(embedding), embedding_model, embedding_version)
        )

    def mark_embedding_failed(
        self, version_id, error, *, retry_at, profile_id="default"
    ) -> None:
        if self.mark_error is not None:
            raise self.mark_error
        self.failures.append((version_id, error, retry_at))

    def embedding_outbox_stats(self, *, profile_id="default"):
        from klonet_agent.memory.repository import EmbeddingOutboxStats

        return EmbeddingOutboxStats(completed=len(self.writes), pending=len(self.queue))


def _pending(version_id: str = "v-1", *, attempt: int = 0, content: str = PY311):
    return PendingEmbedding(
        version_id=version_id,
        memory_id="m-1",
        content=content,
        attempt_count=attempt,
    )


def _worker(repository, embedder, **overrides):
    kwargs = {
        "batch_size": 10,
        "max_attempts": 3,
        "backoff_base_seconds": 30.0,
        "backoff_max_seconds": 600.0,
    }
    kwargs.update(overrides)
    return EmbeddingWorker(repository, embedder, **kwargs)


def test_worker_writes_vector_and_records_identity() -> None:
    embedder = _deterministic_embedder()
    repository = _FakeOutboxRepository([_pending("v-1"), _pending("v-2")])
    worker = _worker(repository, embedder, model="text-embedding-v4", model_version="2026-01")

    stats = worker.run()

    assert (stats.claimed, stats.embedded, stats.failed) == (2, 2, 0)
    assert [write[0] for write in repository.writes] == ["v-1", "v-2"]
    assert repository.writes[0][1] == embedder(PY311)
    assert repository.writes[0][2:] == ("text-embedding-v4", "2026-01")
    assert repository.failures == []


def test_worker_identity_falls_back_to_embedder_then_class_name() -> None:
    class WithIdentity:
        identity = ("dashscope-v4", "rev-7")

        def __call__(self, _text: str):
            return (0.0,) * EMBEDDING_DIMENSIONS

    repository = _FakeOutboxRepository([_pending("v-1")])
    worker = _worker(repository, WithIdentity())
    assert worker.identity == ("dashscope-v4", "rev-7")

    class Anonymous:
        def __call__(self, _text: str):
            return (0.0,) * EMBEDDING_DIMENSIONS

    anonymous = _worker(_FakeOutboxRepository([_pending("v-1")]), Anonymous())
    # schema 上的 memory_versions_embedding_identified 会拒掉空身份，
    # 所以这里必须兜住一个可辨识的名字，而不是留空。
    assert anonymous.identity == ("Anonymous", "Anonymous")


def test_worker_abandons_empty_vector_without_writing() -> None:
    repository = _FakeOutboxRepository([_pending("v-1")])
    stats = _worker(repository, lambda _text: ()).run()

    assert stats.embedded == 0
    assert stats.abandoned == 1
    # 空向量绝不当成功：写进去等于让语义通道静默失效。
    assert repository.writes == []
    assert "空向量" in repository.failures[0][1]
    assert repository.failures[0][2] is None
    assert "EmbeddingContentError" in repository.failures[0][1]


def test_worker_abandons_wrong_dimension_without_writing() -> None:
    repository = _FakeOutboxRepository([_pending("v-1")])
    stats = _worker(repository, lambda _text: (0.1, 0.2)).run()

    assert stats.abandoned == 1
    assert repository.writes == []
    assert "期望 1024 维" in repository.failures[0][1]


def test_worker_retries_transient_failure_with_backoff() -> None:
    def flaky(_text: str):
        raise TimeoutError("上游超时")

    repository = _FakeOutboxRepository([_pending("v-1")])
    stats = _worker(repository, flaky).run()

    assert (stats.retried, stats.abandoned, stats.embedded) == (1, 0, 0)
    version_id, message, retry_at = repository.failures[0]
    assert version_id == "v-1"
    assert "TimeoutError" in message
    # 第 1 次尝试的退避 = base
    assert retry_at is not None
    delta = retry_at - _now()
    assert timedelta(seconds=25) < delta < timedelta(seconds=31)


def _always_failing(_text: str):
    raise RuntimeError("上游一直不可用")


def test_worker_abandons_after_max_attempts() -> None:
    # max_attempts=3，而领取时 attempt_count 已经是 3 → 本次就是最后一次。
    repository = _FakeOutboxRepository([_pending("v-1", attempt=3)])
    stats = _worker(repository, _always_failing).run()

    assert (stats.retried, stats.abandoned) == (0, 1)
    assert repository.failures[0][2] is None


def test_worker_survives_set_embedding_failure_and_keeps_going() -> None:
    repository = _FakeOutboxRepository([_pending("v-1"), _pending("v-2")])
    repository.set_embedding_error = RuntimeError("写库失败")
    stats = _worker(repository, _deterministic_embedder()).run()

    # 两条都失败，但 run 本身不抛异常：后台任务失败不该影响任何用户路径。
    assert stats.claimed == 2
    assert stats.embedded == 0
    assert stats.failed == 2
    assert repository.writes == []
    assert len(repository.failures) == 2


def test_worker_survives_failure_recording_failure() -> None:
    repository = _FakeOutboxRepository([_pending("v-1")])
    repository.mark_error = RuntimeError("数据库挂了")
    stats = _worker(repository, lambda _text: ()).run()

    # 连"记失败"都写不进去时也要安静返回，任务留在 processing，
    # 由租约过期后重新领取。
    assert stats.abandoned == 1


def test_worker_respects_limit_and_stops_when_queue_drains() -> None:
    repository = _FakeOutboxRepository([_pending(f"v-{i}") for i in range(5)])
    stats = _worker(repository, _deterministic_embedder(), batch_size=2).run(limit=3)

    assert stats.claimed == 3
    assert len(repository.writes) == 3
    # 队列里还剩 2 条没有被领走。
    assert len(repository.queue) == 2


def test_worker_stats_exposes_coverage() -> None:
    repository = _FakeOutboxRepository([_pending("v-1")])
    stats = _worker(repository, _deterministic_embedder()).run()
    snapshot = _worker(repository, _deterministic_embedder()).stats()

    assert stats.embedded == 1
    assert snapshot.completed == 1


# --------------------------------------------------------------------------- #
# 真库部分
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin_dsn() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过 embedding outbox 与混合检索集成测试"
            "（「同事务写 outbox」「三通道在真 SQL 上命中」「作用域硬过滤」"
            "必须在真库上证明）"
        )
    try:
        import psycopg
    except ImportError:  # pragma: no cover - 取决于环境
        pytest.skip("未安装 psycopg，跳过真库集成测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:  # pragma: no cover - 取决于环境
        pytest.skip(f"无法连接 {TEST_DSN_ENV}：{exc}")
    return dsn


@pytest.fixture
def db(admin_dsn: str):
    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, min_size=1, max_size=4)
        database.open()
        try:
            database.run_migrations()
            yield database
        finally:
            database.close()


def _repo(database: MemoryDatabase, tenant: Tenant = ALICE) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def test_record_creation_enqueues_outbox_in_the_same_transaction(db) -> None:
    """正文与 outbox 必须同事务：否则先提交正文、后入队失败，就永远没有向量。"""

    record = _repo(db).add_record(_new_record())
    with db.tenant_session(ALICE) as conn:
        rows = conn.execute(
            """
            SELECT status, attempt_count FROM memory_embedding_outbox
             WHERE memory_version_id = %s
            """,
            (UUID(record.active_version_id),),
        ).fetchall()

    assert [(row["status"], row["attempt_count"]) for row in rows] == [("pending", 0)]


def test_add_version_also_enqueues_the_new_version(db) -> None:
    from klonet_agent.memory.repository import NewVersionCommand

    repo = _repo(db)
    record = repo.add_record(_new_record())
    version = repo.add_version(
        NewVersionCommand(memory_id=record.id, content=PY311 + "，并要求 3.8 兼容")
    )
    with db.tenant_session(ALICE) as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM memory_embedding_outbox "
            "WHERE memory_version_id = %s",
            (UUID(version.id),),
        ).fetchone()["n"]

    assert count == 1


def test_worker_end_to_end_fills_embeddings_and_is_idempotent(db) -> None:
    repo = _repo(db)
    embedder = _deterministic_embedder()
    records = [
        repo.add_record(_new_record(subject_key=f"fact:project:demo:item_{index}",
                                    content=f"第 {index} 条事实：运行时要求 Python 3.11"))
        for index in range(3)
    ]

    worker = EmbeddingWorker(
        repo, embedder, model="text-embedding-v4", model_version="2026-01"
    )
    first = worker.run()
    assert (first.claimed, first.embedded, first.failed) == (3, 3, 0)

    for record in records:
        version = repo.get_version(record.active_version_id)
        assert version is not None
        assert version.embedding is not None
        assert len(version.embedding) == EMBEDDING_DIMENSIONS
        assert version.embedding_model == "text-embedding-v4"
        assert version.embedding_version == "2026-01"

    # 幂等：完成任务不会被重复领取，也不会被重复计数。
    second = worker.run()
    assert second.claimed == 0
    snapshot = worker.stats()
    assert snapshot.completed == 3
    assert snapshot.outstanding == 0
    assert snapshot.coverage == 1.0


def test_outbox_state_is_per_tenant(db) -> None:
    """worker 按租户运行：另一个租户看不见、也领不走这些任务。"""

    alice = _repo(db, ALICE)
    alice.add_record(_new_record())
    bob = _repo(db, BOB)

    assert bob.embedding_outbox_stats().total == 0
    assert bob.claim_pending_embeddings(limit=10) == []
    assert alice.embedding_outbox_stats().total == 1


def test_failed_embedding_defers_the_next_claim(db) -> None:
    """退避在真库上生效：刚失败的任务下一次 claim 不会立刻被领走。"""

    repo = _repo(db)
    repo.add_record(_new_record())

    class Boom:
        def __call__(self, _text: str):
            raise RuntimeError("上游超时")

    worker = EmbeddingWorker(repo, Boom(), max_attempts=5, backoff_base_seconds=60.0)
    stats = worker.run()
    assert (stats.retried, stats.abandoned) == (1, 0)

    immediate = worker.run()
    assert immediate.claimed == 0

    with db.tenant_session(ALICE) as conn:
        row = conn.execute(
            "SELECT status, attempt_count, last_error, next_attempt_at "
            "FROM memory_embedding_outbox"
        ).fetchone()
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert "RuntimeError" in row["last_error"]
    assert row["next_attempt_at"] > _now()


def test_expired_outbox_lease_can_be_reclaimed(db) -> None:
    """进程崩在 processing 上时，租约过期后任务要能被重新领取。"""

    repo = _repo(db)
    repo.add_record(_new_record())

    # 零租约 = 立刻过期，模拟"上一次领取的 worker 已经死了"。
    lease_start = _now()
    claimed = repo.claim_pending_embeddings(
        limit=10, lease_seconds=1.0, now=lease_start
    )
    assert len(claimed) == 1
    assert claimed[0].attempt_count == 1

    # 租约未到期时领不走。
    assert repo.claim_pending_embeddings(
        limit=10, lease_seconds=1.0, now=lease_start
    ) == []

    # 租约过期后可以被重新领取，attempt_count 继续累加。
    reclaimed = repo.claim_pending_embeddings(
        limit=10, lease_seconds=1.0, now=lease_start + timedelta(seconds=30)
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt_count == 2


def test_lexical_and_exact_channels_hit_precise_tokens(db) -> None:
    """没有向量时，路径/版本号这类精确 token 必须还能召回。"""

    repo = _repo(db)
    repo.add_record(
        _new_record(
            subject_key="fact:project:demo:vemu_topology",
            content="VEMU2 前端的 nginx 别名是 /VEMU2/，部署脚本是 startup.md",
        )
    )
    repo.add_record(
        _new_record(
            subject_key="fact:project:demo:other",
            content="与本问题无关的另一条记忆",
        )
    )

    report = MemoryRetriever(repo).retrieve(
        MemoryQuery(text="/VEMU2/ 的 nginx 别名怎么配")
    )

    assert report.degraded == ("no_embedder:semantic_channel_skipped",)
    assert report.hits, "精确通道应当命中包含 /VEMU2/ 的记忆"
    assert any("exact" in hit.reasons for hit in report.hits)
    assert "VEMU2" in report.hits[0].version.content


def test_semantic_channel_hits_on_vector_similarity(db) -> None:
    repo = _repo(db)
    embedder = _deterministic_embedder()
    target = "本项目运行时要求 Python 3.11"
    record = repo.add_record(
        _new_record(subject_key="fact:project:demo:runtime", content=target)
    )
    repo.add_record(
        _new_record(
            subject_key="fact:project:demo:unrelated",
            content="完全不相干的另一条记忆，关于网络拓扑",
        )
    )

    worker = EmbeddingWorker(repo, embedder)
    assert worker.run().embedded == 2

    report = MemoryRetriever(repo, embedder=embedder).retrieve(
        MemoryQuery(text=target, limit=2)
    )

    assert report.semantic_used is True
    assert report.hits[0].record.id == record.id
    assert "semantic" in report.hits[0].reasons


def test_scope_filter_prevents_cross_tenant_recall(db) -> None:
    alice = _repo(db, ALICE)
    alice.add_record(_new_record(content="alice 的项目要求 Python 3.11"))

    bob = _repo(db, BOB)
    bob.add_record(
        _new_record(
            subject_key="fact:project:demo:runtime_version",
            content="bob 的项目要求 Python 3.8",
            user_id=BOB.user_id,
        )
    )

    alice_hits = MemoryRetriever(alice).retrieve(MemoryQuery(text="Python 要求"))
    bob_hits = MemoryRetriever(bob).retrieve(MemoryQuery(text="Python 要求"))

    assert {hit.record.user_id for hit in alice_hits.hits} == {"alice"}
    assert {hit.record.user_id for hit in bob_hits.hits} == {"bob"}


def test_expired_memory_is_not_recalled_but_as_of_still_finds_it(db) -> None:
    repo = _repo(db)
    # 有效期从两天前开始，一天前结束：这样 valid_from < valid_to，窗口合法。
    record = repo.add_record(
        _new_record(
            content="旧的有效期结论：要求 Python 3.8",
            valid_from=_now() - timedelta(days=2),
        )
    )
    cutoff = _now() - timedelta(days=1)
    repo.mark_expired(record.id, cutoff)

    current = MemoryRetriever(repo).retrieve(MemoryQuery(text="Python"))
    assert current.hits == ()

    historical = MemoryRetriever(repo).retrieve(
        MemoryQuery(text="Python", as_of=cutoff - timedelta(hours=1))
    )
    assert [hit.record.id for hit in historical.hits] == [record.id]


def test_contradicts_relation_is_surfaced_by_retriever(db) -> None:
    repo = _repo(db)
    first = repo.add_record(
        _new_record(subject_key="fact:project:demo:port", content="服务端口是 8080")
    )
    second = repo.add_record(
        _new_record(subject_key="fact:project:demo:alt_port", content="服务端口是 9090")
    )
    repo.add_relation(
        first.id, RelationType.CONTRADICTS.value, second.id, confidence=0.8
    )

    report = MemoryRetriever(repo).retrieve(MemoryQuery(text="端口"))

    # 关系的两端都该被标出来：模型需要知道"这两条互相矛盾"，而不是只知道其中一条
    # 参与了冲突——只标一端会让另一端看起来像可信结论。
    assert set(report.conflict_ids) == {first.id, second.id}
    for hit in report.hits:
        assert "conflict:contradicts" in hit.reasons


def test_list_relations_returns_both_directions_and_filters_by_type(db) -> None:
    repo = _repo(db)
    first = repo.add_record(
        _new_record(subject_key="fact:project:demo:a", content="结论 A：端口 8080")
    )
    second = repo.add_record(
        _new_record(subject_key="fact:project:demo:b", content="结论 B：端口 9090")
    )
    repo.add_relation(first.id, RelationType.CONTRADICTS.value, second.id)

    # 从被指向的一端查，同样要能看到这条关系。
    from_target = repo.list_relations(second.id)
    assert [(item.from_memory_id, item.to_memory_id) for item in from_target] == [
        (first.id, second.id)
    ]
    assert repo.list_relations(second.id, relation_type=RelationType.SUPPORTS) == []


def test_vector_scan_measurement_justifies_no_hnsw_yet(db) -> None:
    """按计划 §12 测量 exact 向量扫描的耗时。

    这个测试**不建 HNSW**，它把"当前规模下还不需要近似索引"这个结论钉住：
    建 HNSW 会带来召回损失，只有在测量表明 exact 扫描撑不住时才值得。
    """

    repo = _repo(db)
    embedder = _deterministic_embedder()
    count = 80
    for index in range(count):
        repo.add_record(
            _new_record(
                subject_key=f"fact:project:demo:bulk_{index}",
                content=f"批量记忆第 {index} 条：用于向量扫描规模测量",
            )
        )

    worker = EmbeddingWorker(repo, embedder, batch_size=40)
    assert worker.run(limit=count).embedded == count

    retriever = MemoryRetriever(repo, embedder=embedder)
    started = perf_counter()
    report = retriever.retrieve(MemoryQuery(text="批量记忆第 7 条", limit=10))
    elapsed = perf_counter() - started

    assert len(report.hits) == 10
    # 阈值刻意宽松：这里要证明的是"毫秒级、远没到需要 ANN 的程度"，
    # 而不是给一台机器的性能打榜。
    assert elapsed < 2.0, f"{count} 条记忆的 exact 扫描耗时 {elapsed:.3f}s"
