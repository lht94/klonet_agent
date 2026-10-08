"""阶段 6（Markdown 数据迁移）的测试。

分两部分：

* **纯离线**：四态状态机的合法/非法转换、状态文件容错、路径分类、Markdown
  分条、subject 派生（幂等的基础）、以及"源文件只读、失败不留半成品"。
* **真库端到端**（没有 DSN 时整块 skip）：扫描 → 解析 → 经 write pipeline 幂等
  写入 → 重复执行不产生重复记忆 → 核对报告。

跑法：

    ./scripts/pg_local.sh up
    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_migration.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from klonet_agent.memory.domain import MemoryType, Scope, WriteDecision
from klonet_agent.memory.migration import (
    MarkdownMemoryScanner,
    MemoryMigrator,
    MigrationPhase,
    MigrationState,
    MigrationStateStore,
    TransitionError,
    allowed_next_phases,
    parse_document,
    render_report,
    split_markdown_items,
)

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


# --------------------------------------------------------------------------- #
# 四态状态机
# --------------------------------------------------------------------------- #


def test_phase_transitions_are_adjacent_only() -> None:
    assert allowed_next_phases("legacy") == ("shadow",)
    assert allowed_next_phases("shadow") == ("compare", "legacy")
    assert allowed_next_phases("compare") == ("cutover", "shadow")
    # cutover 只能退回 compare：回到读 Markdown 回答之前必须先确认库里数据完整。
    assert allowed_next_phases("cutover") == ("compare",)
    assert allowed_next_phases("nonsense") == ()


def test_state_store_roundtrip_and_missing_file(tmp_path: Path) -> None:
    store = MigrationStateStore(tmp_path / ".migration_state.json")
    assert store.load().phase == MigrationPhase.LEGACY.value

    store.save(
        MigrationState(
            phase=MigrationPhase.SHADOW.value,
            updated_at="2026-10-08T00:00:00+00:00",
            source_hashes={"sessions/a/b/MEMORY.md": "abc"},
        )
    )
    loaded = store.load()
    assert loaded.phase == "shadow"
    assert loaded.source_hashes == {"sessions/a/b/MEMORY.md": "abc"}
    # save 会补 updated_at
    assert loaded.updated_at


def test_corrupt_state_file_is_treated_as_fresh(tmp_path: Path) -> None:
    """状态文件坏了就当作没迁移过：重跑一遍是安全的（幂等），崩掉不是。"""

    path = tmp_path / ".migration_state.json"
    path.write_text("{not json", encoding="utf-8")
    assert MigrationStateStore(path).load().phase == MigrationPhase.LEGACY.value


def test_illegal_transition_is_rejected(tmp_path: Path) -> None:
    store = MigrationStateStore(tmp_path / "state.json")
    with pytest.raises(TransitionError):
        store.transition("cutover")


def test_cutover_requires_a_snapshot(tmp_path: Path) -> None:
    """cutover 的回滚只能回到只读快照，所以没有快照就不允许进入。"""

    store = MigrationStateStore(tmp_path / "state.json")
    store.transition("shadow")
    store.transition("compare")
    with pytest.raises(TransitionError):
        store.transition("cutover")

    state = store.transition("cutover", snapshot_export="exports/2026-10-08.json")
    assert state.phase == "cutover"
    assert state.snapshot_export == "exports/2026-10-08.json"


def test_rollback_from_cutover_keeps_the_snapshot(tmp_path: Path) -> None:
    store = MigrationStateStore(tmp_path / "state.json")
    store.transition("shadow")
    store.transition("compare")
    store.transition("cutover", snapshot_export="exports/s.json")
    rolled = store.transition("compare")
    assert rolled.phase == "compare"
    # 回滚后快照仍留着：再次进 cutover 不必重新导出。
    assert rolled.snapshot_export == "exports/s.json"


# --------------------------------------------------------------------------- #
# 扫描与解析
# --------------------------------------------------------------------------- #


def _write_tree(root: Path) -> None:
    (root / "sessions" / "alice" / "demo").mkdir(parents=True)
    (root / "sessions" / "alice" / "demo" / "MEMORY.md").write_text(
        "# 项目长期记忆\n"
        "\n"
        "## 运行时\n"
        "- 本项目运行时要求 Python 3.11\n"
        "- 依赖安装使用 `pip install -e .`\n"
        "\n"
        "## 部署\n"
        "部署前先跑一遍全量测试。\n",
        encoding="utf-8",
    )
    (root / "sessions" / "alice" / "demo" / "2026-10-02.md").write_text(
        "## 09:00 排查端口占用\n\n- 重启 nginx 后恢复\n",
        encoding="utf-8",
    )
    (root / "users" / "alice").mkdir(parents=True)
    (root / "users" / "alice" / "USER.md").write_text(
        "- 回答一律使用简体中文\n", encoding="utf-8"
    )
    (root / "shared" / "ops").mkdir(parents=True)
    (root / "shared" / "ops" / "2026-10-01.md").write_text(
        "## 10:00 部署失败\n\n- 目标端口 8080 被占用\n", encoding="utf-8"
    )
    (root / "shared" / "ops" / "BASELINE.md").write_text(
        "- 服务器是 Ubuntu 20.04，内核 5.4\n", encoding="utf-8"
    )
    # 非记忆文件必须被忽略。
    (root / "histories").mkdir()
    (root / "histories" / "other.md").write_text("- 不该被迁移\n", encoding="utf-8")


def test_scanner_classifies_every_known_layout(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    documents = {doc.rel_path: doc for doc in MarkdownMemoryScanner(tmp_path).scan()}

    assert set(documents) == {
        "sessions/alice/demo/MEMORY.md",
        "sessions/alice/demo/2026-10-02.md",
        "users/alice/USER.md",
        "shared/ops/2026-10-01.md",
        "shared/ops/BASELINE.md",
    }
    assert documents["sessions/alice/demo/MEMORY.md"].kind == "session_memory"
    assert documents["sessions/alice/demo/MEMORY.md"].project_id == "demo"
    assert documents["users/alice/USER.md"].kind == "user_profile"
    assert documents["shared/ops/2026-10-01.md"].user_id == "shared"
    assert documents["shared/ops/2026-10-01.md"].kind == "shared_ops_daily"


def test_scan_is_read_only(tmp_path: Path) -> None:
    """迁移不修改源文件——它们会一直是最新的导出源。"""

    _write_tree(tmp_path)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(tmp_path.rglob("*.md"))
    }
    MarkdownMemoryScanner(tmp_path).scan()
    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(tmp_path.rglob("*.md"))
    }
    assert before == after


def test_scanner_is_stable_for_missing_root(tmp_path: Path) -> None:
    assert MarkdownMemoryScanner(tmp_path / "nope").scan() == []


def test_split_markdown_items_handles_bullets_continuations_and_headings() -> None:
    text = (
        "# 标题不进条目\n"
        "\n"
        "- 第一条事实\n"
        "  续行并入上一条\n"
        "- 第二条事实也足够长\n"
        "\n"
        "独立段落也算一条。\n"
    )
    items = split_markdown_items(text)
    assert items == [
        "第一条事实\n续行并入上一条",
        "第二条事实也足够长",
        "独立段落也算一条。",
    ]
    assert all("标题不进条目" not in item for item in items)


def test_split_markdown_items_drops_too_short_lines() -> None:
    assert split_markdown_items("- ok\n- 这是一条足够长的事实\n") == [
        "这是一条足够长的事实"
    ]


def test_parse_document_maps_kinds_to_memory_types(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    documents = {doc.rel_path: doc for doc in MarkdownMemoryScanner(tmp_path).scan()}

    memory_items = parse_document(documents["sessions/alice/demo/MEMORY.md"])
    assert {item.proposal.memory_type for item in memory_items} == {MemoryType.FACT.value}
    assert {item.proposal.scope for item in memory_items} == {Scope.PROJECT.value}

    user_items = parse_document(documents["users/alice/USER.md"])
    assert user_items[0].proposal.memory_type == MemoryType.PREFERENCE.value
    assert user_items[0].proposal.scope == Scope.USER.value

    shared_items = parse_document(documents["shared/ops/2026-10-01.md"])
    assert shared_items[0].proposal.scope == Scope.SHARED_OPS.value
    assert shared_items[0].proposal.memory_type == MemoryType.EPISODE.value


def test_subject_key_is_derived_from_content_so_migration_is_idempotent(
    tmp_path: Path,
) -> None:
    """同一条内容必须落到同一个 subject 上，否则重复迁移会插出第二条记忆。"""

    _write_tree(tmp_path)
    document = next(
        doc
        for doc in MarkdownMemoryScanner(tmp_path).scan()
        if doc.rel_path.endswith("MEMORY.md")
    )
    first = parse_document(document)
    second = parse_document(document)

    assert [item.subject_key for item in first] == [item.subject_key for item in second]
    # 内容不同 → subject 不同。
    assert first[0].subject_key != first[1].subject_key
    # 情景记忆按事件身份去重，不走内容哈希那一条；
    # build_subject_key 会把组件里的 "-" 归一成 "_"。
    episode_document = next(
        doc for doc in MarkdownMemoryScanner(tmp_path).scan() if doc.kind == "session_episode"
    )
    episode_items = parse_document(episode_document)
    assert episode_items[0].subject_key.startswith("episode:md_demo_")
    assert episode_items[0].proposal.episode_id.startswith("md-demo-")


def test_source_whitelist_contains_only_this_document(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    document = next(
        doc
        for doc in MarkdownMemoryScanner(tmp_path).scan()
        if doc.rel_path.endswith("USER.md")
    )
    item = parse_document(document)[0]
    assert item.source_id.startswith("md:users/alice/USER.md#")
    assert item.proposal.sources[0].source_id == item.source_id


# --------------------------------------------------------------------------- #
# 编排（假管线，离线）
# --------------------------------------------------------------------------- #


class _FakePipeline:
    """记录调用的假管线，用来把编排逻辑与数据库分开测。"""

    def __init__(self, *, wrote: bool = True, decision: WriteDecision | None = None):
        self.calls: list[dict] = []
        self._wrote = wrote
        self._decision = decision or (
            WriteDecision.ADD if wrote else WriteDecision.REJECT
        )

    def process_proposal(self, proposal, context, *, source_event_range, idempotency_key):
        self.calls.append(
            {
                "proposal": proposal,
                "context": context,
                "range": source_event_range,
                "key": idempotency_key,
            }
        )
        return SimpleNamespace(
            wrote=self._wrote,
            decision=self._decision,
            reason="fake",
            verdict=None,
        )


def _migrator(tmp_path: Path, pipeline: _FakePipeline) -> MemoryMigrator:
    return MemoryMigrator(
        lambda _tenant: pipeline,
        root=tmp_path,
        state_store=MigrationStateStore(tmp_path / ".migration_state.json"),
    )


def test_preview_never_calls_the_pipeline(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    pipeline = _FakePipeline()
    report = _migrator(tmp_path, pipeline).preview()

    assert pipeline.calls == []
    assert report.dry_run is True
    assert report.documents == 5
    # 条目数：MEMORY.md 3 条 + 日记 1 + USER.md 1 + 共享日记 1 + BASELINE 1 = 7
    assert report.items == 7
    assert report.accepted == 0  # 预览不"接受"，只是列出会处理多少条
    assert report.by_kind["session_memory"] == 1


def test_apply_passes_tenant_and_idempotency_key(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    pipeline = _FakePipeline()
    report = _migrator(tmp_path, pipeline).run(apply=True)

    assert report.failures == ()
    assert report.accepted == report.items == 7
    keys = [call["key"] for call in pipeline.calls]
    assert len(set(keys)) == len(keys), "幂等键必须互不相同"
    assert all(key.startswith("migrate:") for key in keys)
    tenants = {call["context"].tenant.user_id for call in pipeline.calls}
    assert tenants == {"alice", "shared"}
    # 共享 Ops 记忆必须带 allow_shared_ops，否则策略层会拒。
    shared_call = next(
        call for call in pipeline.calls if call["context"].tenant.user_id == "shared"
    )
    assert shared_call["context"].allow_shared_ops is True


def test_second_apply_skips_unchanged_sources(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    pipeline = _FakePipeline()
    migrator = _migrator(tmp_path, pipeline)

    first = migrator.run(apply=True)
    assert first.items == 7 and first.skipped_unchanged == 0
    calls_after_first = len(pipeline.calls)

    second = migrator.run(apply=True)
    assert second.skipped_unchanged == 5
    assert second.items == 0
    assert len(pipeline.calls) == calls_after_first


def test_changed_source_is_reprocessed(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    pipeline = _FakePipeline()
    migrator = _migrator(tmp_path, pipeline)
    migrator.run(apply=True)

    target = tmp_path / "users" / "alice" / "USER.md"
    target.write_text("- 回答一律使用简体中文\n- 代码示例要带注释\n", encoding="utf-8")

    again = migrator.run(apply=True)
    assert again.skipped_unchanged == 4
    assert again.items == 2


def test_failures_do_not_advance_the_hash_ledger(tmp_path: Path) -> None:
    """有失败时不能把源文件标成"已迁移"，否则下次重跑会跳过它们。"""

    _write_tree(tmp_path)
    pipeline = _FakePipeline()
    migrator = _migrator(tmp_path, pipeline)

    original = pipeline.process_proposal

    def boom(proposal, context, *, source_event_range, idempotency_key):
        if "USER.md" in idempotency_key:
            raise RuntimeError("模拟写入失败")
        return original(
            proposal,
            context,
            source_event_range=source_event_range,
            idempotency_key=idempotency_key,
        )

    pipeline.process_proposal = boom  # type: ignore[method-assign]
    report = migrator.run(apply=True)

    assert report.failures and "USER.md" in report.failures[0]
    # 台账没有推进 → 下次重跑仍会处理全部文件。
    assert migrator.run(apply=False).items == 7


def test_rejections_are_counted_by_reason(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    pipeline = _FakePipeline(wrote=False, decision=WriteDecision.NOOP)
    report = _migrator(tmp_path, pipeline).run(apply=True)

    assert report.accepted == 0
    assert report.noop == 7
    assert report.rejected == 0


def test_render_report_contains_the_reconciliation_fields(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    report = _migrator(tmp_path, _FakePipeline()).run(apply=True)
    text = render_report(report, phase="shadow")

    assert "阶段 shadow" in text
    assert "解析条目" in text
    assert "按来源类型" in text
    assert "session_memory" in text


# --------------------------------------------------------------------------- #
# 真库端到端
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin_dsn() -> str:
    import os

    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过迁移端到端测试"
            "（「重复迁移不产生重复记忆」必须在真库上证明）"
        )
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        pytest.skip("未安装 psycopg，跳过真库集成测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:  # pragma: no cover
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


def _real_migrator(
    db, tmp_path: Path, *, state_name: str = ".migration_state.json"
) -> MemoryMigrator:
    from klonet_agent.memory.postgres import PostgresMemoryRepository
    from klonet_agent.memory.write_pipeline import MemoryWritePipeline, NullWriteTracer
    from klonet_agent.memory.write_policy import MemoryWritePolicy

    def build(tenant):
        return MemoryWritePipeline(
            PostgresMemoryRepository(db, tenant),
            policy=MemoryWritePolicy(),
            tracer=NullWriteTracer(),
        )

    return MemoryMigrator(
        build,
        root=tmp_path,
        state_store=MigrationStateStore(tmp_path / state_name),
    )


def _count_records(db, tenant) -> int:
    """该租户可见的记忆条数。

    走租户事务而不是 search：search 依赖 lexical 命中，用它计数会把
    "检索排序"和"到底写了几条"两件事混在一起。
    租户视图里读不到 shared_ops（RLS policy 明确排除），正是这里要的口径。
    """

    with db.tenant_session(tenant) as conn:
        row = conn.execute("SELECT count(*) AS n FROM memory_records").fetchone()
    return int(row["n"])


def test_real_db_migration_is_idempotent(db, tmp_path: Path) -> None:
    """完成标准：同一迁移命令重复执行不会产生重复记忆。"""

    from klonet_agent.memory.domain import Tenant

    _write_tree(tmp_path)
    migrator = _real_migrator(db, tmp_path)

    first = migrator.run(apply=True)
    assert first.failures == ()
    assert first.accepted == first.items == 7

    alice = Tenant(user_id="alice", project_id="demo")
    after_first = _count_records(db, alice)

    # 第二次：源文件没变 → 全部跳过，库里的记录数不变。
    second = migrator.run(apply=True)
    assert second.skipped_unchanged == 5
    assert _count_records(db, alice) == after_first

    # 就算强行重跑（换一个空台账，绕开"源文件没变就跳过"那层），
    # subject 派生与内容哈希也让 consolidation 判 NOOP。
    fresh = _real_migrator(db, tmp_path, state_name=".fresh_state.json")
    third = fresh.run(apply=True)
    assert third.accepted == 0
    assert third.noop == third.items > 0
    assert _count_records(db, alice) == after_first


def test_real_db_migration_does_not_touch_sources(db, tmp_path: Path) -> None:
    _write_tree(tmp_path)
    before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*.md"))}
    _real_migrator(db, tmp_path).run(apply=True)
    after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*.md"))}
    assert before == after


def test_real_db_migrated_memory_is_recallable(db, tmp_path: Path) -> None:
    """迁进来的记忆必须能被召回，否则迁移没有意义。"""

    from klonet_agent.memory.domain import MemoryQuery, Tenant
    from klonet_agent.memory.postgres import PostgresMemoryRepository
    from klonet_agent.memory.retriever import MemoryRetriever

    _write_tree(tmp_path)
    _real_migrator(db, tmp_path).run(apply=True)

    repo = PostgresMemoryRepository(db, Tenant(user_id="alice", project_id="demo"))
    report = MemoryRetriever(repo).retrieve(MemoryQuery(text="Python 运行 要求", limit=20))

    assert report.hits
    contents = " ".join(hit.version.content for hit in report.hits)
    assert "Python 3.11" in contents


def test_real_db_state_file_records_the_run(db, tmp_path: Path) -> None:
    _write_tree(tmp_path)
    migrator = _real_migrator(db, tmp_path)
    migrator.run(apply=True)

    payload = json.loads(
        (tmp_path / ".migration_state.json").read_text(encoding="utf-8")
    )
    assert payload["phase"] == MigrationPhase.LEGACY.value
    assert len(payload["source_hashes"]) == 5
    assert payload["last_report"]["accepted"] == 7
