"""阶段 5（MemoryPack 与 ContextCompiler 接入）的测试。

分两部分：

* **纯离线**：渲染格式、条数上限、token 硬约束与淘汰顺序、冲突提示、
  ops 运行态约束、消息标记与发送前剥离。这部分在任何机器上都能跑。
* **真库端到端**（没有 DSN 时整块 skip）：写入记忆 → 混合召回 → 构包 →
  编译，证明"关闭 MEMORY.md/USER.md 常驻注入后，相关记忆仍能按问题被召回，
  无关记忆不会进入 compiled context"这条完成标准。

跑法：

    ./scripts/pg_local.sh up
    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_pack.py -q
"""

from __future__ import annotations

import hashlib
import math
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from klonet_agent.context.compiler import ContextCompiler, to_provider_messages
from klonet_agent.memory.domain import (
    MemoryHit,
    MemoryRecord,
    MemorySource,
    MemoryType,
    MemoryVersion,
    Scope,
    SourceType,
    Tenant,
    content_hash,
)
from klonet_agent.memory.pack import (
    DEFAULT_TYPE_LIMITS,
    MemoryPackBuilder,
)
from klonet_agent.memory.repository import EMBEDDING_DIMENSIONS
from klonet_agent.memory.retriever import MemoryRetrievalReport

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"

ALICE = Tenant(user_id="alice", project_id="demo")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #


def _subject_for(memory_type: MemoryType, memory_id: str) -> str:
    if memory_type is MemoryType.EPISODE:
        return f"episode:evt-{memory_id[:8]}"
    return f"{memory_type.value}:user:alice:subject"


def _hit(
    memory_type: MemoryType = MemoryType.FACT,
    content: str = "本项目运行时要求 Python 3.11",
    *,
    score: float = 1.0,
    confidence: float = 0.9,
    sources: tuple[tuple[SourceType, str, str], ...] = (
        (SourceType.USER_STATEMENT, "rows-12", "用户在对话里说明"),
    ),
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    reasons: tuple[str, ...] = ("lexical",),
) -> MemoryHit:
    memory_id = str(uuid4())
    record = MemoryRecord(
        id=memory_id,
        user_id=ALICE.user_id,
        scope=Scope.USER,
        project_id=None,
        memory_type=memory_type,
        subject_key=_subject_for(memory_type, memory_id),
        importance=0.6,
        confidence=confidence,
    )
    version = MemoryVersion(
        id=str(uuid4()),
        memory_id=memory_id,
        version=1,
        content=content,
        observed_at=_now(),
        valid_from=valid_from or _now(),
        valid_to=valid_to,
        content_hash=content_hash(content),
        sources=tuple(
            MemorySource(
                source_type=kind,
                source_id=sid,
                observed_at=_now(),
                source_excerpt=excerpt,
            )
            for kind, sid, excerpt in sources
        ),
    )
    return MemoryHit(record=record, version=version, score=score, reasons=reasons)


def _report(
    *,
    preference: str = "回答一律使用简体中文",
    fact: str = "本项目运行时要求 Python 3.11",
    episode: str = "上次部署失败是因为目标端口被占用",
    conflict_ids: tuple[str, ...] = (),
    degraded: tuple[str, ...] = (),
    extra_hits: tuple[MemoryHit, ...] = (),
) -> MemoryRetrievalReport:
    hits: list[MemoryHit] = []
    if preference:
        hits.append(
            _hit(MemoryType.PREFERENCE, preference, score=3.0, confidence=0.95)
        )
    if fact:
        hits.append(_hit(MemoryType.FACT, fact, score=2.0, confidence=0.9))
    if episode:
        hits.append(
            _hit(MemoryType.EPISODE, episode, score=1.0, confidence=0.7)
        )
    hits.extend(extra_hits)
    return MemoryRetrievalReport(
        query="运行时要求",
        hits=tuple(hits),
        conflict_ids=conflict_ids,
        degraded=degraded,
    )


def _builder(**overrides) -> MemoryPackBuilder:
    kwargs = {"token_budget": 900}
    kwargs.update(overrides)
    return MemoryPackBuilder(**kwargs)


# --------------------------------------------------------------------------- #
# 渲染格式：固定结构 + 五项元数据
# --------------------------------------------------------------------------- #


def test_pack_renders_fixed_sections_in_order() -> None:
    pack = _builder().build(_report())

    assert not pack.empty
    preference_at = pack.text.index("### 相关用户偏好")
    fact_at = pack.text.index("### 相关项目事实")
    episode_at = pack.text.index("### 相似历史经历")
    assert preference_at < fact_at < episode_at


def test_each_entry_carries_id_type_scope_confidence_window_and_sources() -> None:
    pack = _builder().build(_report(episode=""))
    fact_entry = next(e for e in pack.entries if e.memory_type == "fact")

    assert f"id={fact_entry.memory_id[:8]}" in pack.text
    assert "类型=fact" in pack.text
    assert "作用域=user" in pack.text
    assert "置信度=0.90" in pack.text
    assert "有效期=" in pack.text
    assert "来源=user_statement:rows-12" in pack.text


def test_pack_never_renders_source_excerpts() -> None:
    """来源只渲染标识：摘录是唯一可能带敏感内容的字段，少渲染一段就少一条旁路。"""

    hit = _hit(
        sources=(
            (SourceType.TOOL_RESULT, "call_7", "Authorization: Bearer sk-secret-xyz"),
        )
    )
    pack = _builder().build(MemoryRetrievalReport(query="q", hits=(hit,)))

    assert "call_7" in pack.text
    assert "sk-secret-xyz" not in pack.text
    assert "Bearer" not in pack.text


def test_overlong_content_is_truncated_and_explicitly_marked() -> None:
    long_text = "这是一条很长的记忆。" * 100
    pack = _builder(max_content_chars=60).build(
        MemoryRetrievalReport(query="q", hits=(_hit(content=long_text),))
    )

    entry = pack.entries[0]
    assert entry.content_truncated is True
    assert len(entry.content) <= 61
    # 结构不能被截断：id/类型/来源必须完整保留。
    assert "正文已截断" in pack.text
    assert "来源=user_statement:rows-12" in pack.text


def test_validity_window_renders_both_open_and_closed_forms() -> None:
    open_hit = _hit(content="长期有效的事实", valid_from=datetime(2026, 1, 2, tzinfo=timezone.utc))
    closed_hit = _hit(
        content="有明确结束时间的事实",
        valid_from=datetime(2026, 1, 2, tzinfo=timezone.utc),
        valid_to=datetime(2026, 3, 4, tzinfo=timezone.utc),
    )
    pack = _builder().build(
        MemoryRetrievalReport(query="q", hits=(open_hit, closed_hit))
    )

    assert "2026-01-02 起有效" in pack.text
    assert "2026-01-02 ~ 2026-03-04" in pack.text


# --------------------------------------------------------------------------- #
# 条数上限
# --------------------------------------------------------------------------- #


def test_type_limits_cap_each_section() -> None:
    assert DEFAULT_TYPE_LIMITS["preference"] == 2
    assert DEFAULT_TYPE_LIMITS["fact"] == 4
    assert DEFAULT_TYPE_LIMITS["episode"] == 3

    hits = tuple(
        _hit(MemoryType.FACT, f"第 {index} 条项目事实", score=float(20 - index))
        for index in range(7)
    )
    pack = _builder(token_budget=4000).build(
        MemoryRetrievalReport(query="q", hits=hits)
    )

    assert len(pack.entries) == 4
    assert [entry.content for entry in pack.entries] == [
        "第 0 条项目事实",
        "第 1 条项目事实",
        "第 2 条项目事实",
        "第 3 条项目事实",
    ]
    assert sum(1 for item in pack.dropped if item.reason == "over_type_limit") == 3


def test_preference_and_episode_have_their_own_caps() -> None:
    hits = tuple(
        _hit(MemoryType.PREFERENCE, f"偏好 {index}", score=float(10 - index))
        for index in range(4)
    ) + tuple(
        _hit(MemoryType.EPISODE, f"经历 {index}", score=float(9 - index))
        for index in range(5)
    )
    pack = _builder(token_budget=4000).build(
        MemoryRetrievalReport(query="q", hits=hits)
    )

    types = [entry.memory_type for entry in pack.entries]
    assert types.count("preference") == 2
    assert types.count("episode") == 3


# --------------------------------------------------------------------------- #
# token 硬约束与淘汰顺序
# --------------------------------------------------------------------------- #


def _first_drop_budget(report: MemoryRetrievalReport, memory_type: str) -> int | None:
    """从宽到紧扫描预算，找出该类型第一次被挤出去的预算。

    下界要足够低：用户偏好通常很短，预算压到几十 token 才会被挤出去。
    """

    for budget in range(1400, 0, -5):
        pack = _builder(token_budget=budget).build(report)
        if memory_type not in {entry.memory_type for entry in pack.entries}:
            return budget
    return None


def test_token_budget_evicts_episode_then_fact_then_preference() -> None:
    """计划 §6.7：超预算时优先删除低分 episode，再删除低分 fact。"""

    report = _report()
    episode_budget = _first_drop_budget(report, "episode")
    fact_budget = _first_drop_budget(report, "fact")
    preference_budget = _first_drop_budget(report, "preference")

    assert episode_budget is not None, "始终没挤出 episode，说明预算扫描区间不对"
    assert fact_budget is not None
    assert preference_budget is not None
    # 预算越紧越先丢：episode 在最大的预算下就出局，preference 撑到最后。
    assert episode_budget > fact_budget > preference_budget


def test_tokens_never_exceed_the_budget() -> None:
    report = _report(fact="本项目运行时要求 Python 3.11。" * 8)
    for budget in range(120, 1200, 20):
        pack = _builder(token_budget=budget).build(report)
        assert pack.tokens <= budget


def test_pack_is_empty_rather_than_a_bare_header() -> None:
    """连一条都放不下时返回空包：只留标题会白占 token，还暗示"查过了但没有"。"""

    # 预算小到连标题都装不下，任何条目都不可能留下。
    pack = _builder(token_budget=10).build(_report())

    assert pack.empty
    assert pack.text == ""
    assert pack.tokens == 0
    assert pack.to_message() is None
    assert pack.dropped


def test_no_hits_produces_empty_pack() -> None:
    pack = _builder().build(MemoryRetrievalReport(query="q", hits=()))

    assert pack.empty
    assert pack.to_message() is None


# --------------------------------------------------------------------------- #
# 冲突提示与 ops 运行态约束
# --------------------------------------------------------------------------- #


def test_conflict_notice_only_covers_memories_in_the_pack() -> None:
    hit = _hit(content="服务端口是 8080")
    pack = _builder().build(
        MemoryRetrievalReport(
            query="端口",
            hits=(hit,),
            conflict_ids=(hit.record.id, "not-in-pack-id"),
        )
    )

    assert "### 需要确认的冲突" in pack.text
    assert "不得静默合并" in pack.text
    assert hit.record.id[:8] in pack.text
    # 没进包的记忆不该出现在冲突提示里——模型看不到它的内容，提它只会制造困惑。
    assert pack.conflict_ids == (hit.record.id,)
    assert "not-in-pack" not in pack.text


def test_ops_mode_adds_runtime_confirmation_constraint() -> None:
    pack = _builder().build(_report(), mode="ops")
    assert "### 运行态约束" in pack.text
    assert "必须先由本轮工具结果确认" in pack.text


def test_mentor_mode_has_no_runtime_constraint() -> None:
    pack = _builder().build(_report(), mode="mentor")
    assert "运行态约束" not in pack.text


def test_degraded_recall_is_reported_inside_the_pack() -> None:
    pack = _builder().build(
        _report(degraded=("no_embedder:semantic_channel_skipped",))
    )
    assert "### 召回状态" in pack.text
    assert "no_embedder" in pack.text


# --------------------------------------------------------------------------- #
# 消息包装与发送前剥离
# --------------------------------------------------------------------------- #


def test_to_message_uses_user_role_with_local_markers() -> None:
    pack = _builder().build(_report())
    message = pack.to_message()

    assert message is not None
    # 用 user 而不是 system：记忆是证据，不能伪装成系统规则。
    assert message["role"] == "user"
    assert message["_memory_pack"] is True
    assert message["_memory_pack_ids"] == list(pack.memory_ids)
    assert len(message["_memory_pack_ids"]) == len(pack.entries)


def test_provider_messages_strip_pack_markers() -> None:
    pack = _builder().build(_report())
    message = pack.to_message()
    cleaned = to_provider_messages([message])

    assert sorted(cleaned[0].keys()) == ["content", "role"]


def test_memory_ids_expose_full_identifiers() -> None:
    pack = _builder().build(_report())
    for entry in pack.entries:
        assert str(entry.memory_id) in pack.memory_ids
        assert len(entry.memory_id) == 36  # 完整 UUID 只在这里，不在渲染文本里


def test_invalid_token_budget_is_rejected() -> None:
    with pytest.raises(ValueError):
        MemoryPackBuilder(token_budget=0)


# --------------------------------------------------------------------------- #
# 真库端到端：关闭常驻注入后，记忆仍按问题被召回
# --------------------------------------------------------------------------- #


def _deterministic_embedder(dimensions: int = EMBEDDING_DIMENSIONS):
    """确定性假向量：这些测试要证明的是链路接通，不是某家供应商的语义质量。"""

    def embed(text: str) -> tuple[float, ...]:
        digest = hashlib.sha256(str(text).encode("utf-8")).digest()
        values = [digest[index % len(digest)] / 255.0 for index in range(dimensions)]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return tuple(value / norm for value in values)

    return embed


@pytest.fixture
def admin_dsn() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过 MemoryPack 端到端集成测试"
            "（「关闭常驻注入后仍能按问题召回」必须在真库上证明）"
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
    from klonet_agent.memory.database import MemoryDatabase, temporary_database

    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, min_size=1, max_size=4)
        database.open()
        try:
            database.run_migrations()
            yield database
        finally:
            database.close()


def _repo(database, tenant: Tenant = ALICE):
    from klonet_agent.memory.postgres import PostgresMemoryRepository

    return PostgresMemoryRepository(database, tenant)


def _new_record(
    subject_key: str, content: str, *, valid_from: datetime | None = None
):
    from klonet_agent.memory.repository import NewRecordCommand

    return NewRecordCommand(
        user_id=ALICE.user_id,
        scope=Scope.USER,
        memory_type=MemoryType.FACT,
        subject_key=subject_key,
        content=content,
        project_id=None,
        sources=(
            MemorySource(
                source_type=SourceType.USER_STATEMENT,
                source_id="rows-3",
                observed_at=_now(),
                source_excerpt="用户在对话里说明",
            ),
        ),
        confidence=0.9,
        valid_from=valid_from,
    )


def test_real_db_pack_injects_only_the_relevant_memory(db) -> None:
    """完成标准：相关记忆按问题被召回，无关记忆不进入 compiled context。"""

    from klonet_agent.context.compiler import ContextCompiler
    from klonet_agent.memory.domain import MemoryQuery
    from klonet_agent.memory.embedding_worker import EmbeddingWorker
    from klonet_agent.memory.retriever import MemoryRetriever

    repo = _repo(db)
    embedder = _deterministic_embedder()
    target_text = "本项目运行时要求 Python 3.11"
    target = repo.add_record(_new_record("fact:user:alice:runtime_version", target_text))
    unrelated = repo.add_record(
        _new_record(
            "fact:user:alice:meeting_room",
            "Meetings are booked through a Flask service on port 8080 with Redis cache",
        )
    )
    assert EmbeddingWorker(repo, embedder, batch_size=10).run(limit=10).embedded == 2

    # limit=1：只取融合分数最高的那一条。相关记忆必须占据这个位置，
    # 无关记忆不该因为"检索通道把池子取大了"而被顺带注入上下文。
    report = MemoryRetriever(repo, embedder=embedder).retrieve(
        MemoryQuery(text=target_text, limit=1)
    )
    pack = _builder().build(report, mode="mentor")

    assert target.id in pack.memory_ids
    assert unrelated.id not in pack.memory_ids

    compiled = ContextCompiler().compile_history(
        [
            {"role": "system", "content": "你是 Klonet 教学协作 Agent。"},
            {"role": "user", "content": target_text},
        ],
        model="glm-5.2",
        memory_pack_message=pack.to_message(),
    )
    text = "".join(str(message.get("content")) for message in compiled.messages)
    assert compiled.memory_pack_ids == pack.memory_ids
    assert "Python 3.11" in text
    assert "Redis" not in text
    # 记忆走证据区，不能出现在系统规则里。
    system_text = "".join(
        str(message.get("content"))
        for message in compiled.messages
        if message.get("role") == "system"
    )
    assert "Python 3.11" not in system_text


def test_real_db_pack_survives_recall_without_embeddings(db) -> None:
    """向量通道不可用时，关键词召回仍然能产出可注入的包（可解释的降级）。"""

    from klonet_agent.memory.domain import MemoryQuery
    from klonet_agent.memory.retriever import MemoryRetriever

    repo = _repo(db)
    repo.add_record(
        _new_record(
            "fact:user:alice:vemu_alias",
            "VEMU2 前端的 nginx 别名是 /VEMU2/，部署脚本是 startup.md",
        )
    )

    report = MemoryRetriever(repo).retrieve(MemoryQuery(text="/VEMU2/ 别名", limit=10))
    pack = _builder().build(report, mode="mentor")

    assert report.semantic_used is False
    assert "召回状态" in pack.text
    assert not pack.empty
    assert "VEMU2" in pack.text


def test_real_db_expired_memory_never_reaches_the_pack(db) -> None:
    from klonet_agent.memory.domain import MemoryQuery
    from klonet_agent.memory.retriever import MemoryRetriever

    repo = _repo(db)
    target_text = "旧结论：本项目要求 Python 3.8"
    record = repo.add_record(
        _new_record(
            "fact:user:alice:old_runtime",
            target_text,
            # 有效期从两天前开始、一天前结束：valid_from < valid_to，窗口合法。
            valid_from=_now() - timedelta(days=2),
        )
    )
    repo.mark_expired(record.id, _now() - timedelta(days=1))

    report = MemoryRetriever(repo).retrieve(MemoryQuery(text=target_text, limit=10))
    pack = _builder().build(report)

    assert pack.empty
    assert record.id not in pack.memory_ids
