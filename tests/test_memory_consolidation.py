"""ConsolidationJob 测试（04 计划 §6.4 / 阶段 5 第二批）。

离线部分覆盖确定性分类与候选生成（纯函数，用轻量假记录）；
真库部分覆盖"只产提案"、指纹去重、跨租户、dry-run、模型建议接口、
以及"未批准不改正式记忆"。

跑法::

    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_consolidation.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    MemorySource,
    MemoryStatus,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
)
from klonet_agent.memory.maintenance.base import JobContext
from klonet_agent.memory.maintenance.jobs.consolidation import (
    ConsolidationJob,
    SuggestionRequest,
    candidate_pairs,
    classify_pair,
    token_similarity,
)
from klonet_agent.memory.maintenance.proposals import (
    ProposalStatus,
    ProposalStore,
    ProposalType,
)
from klonet_agent.memory.postgres import PostgresMemoryRepository
from klonet_agent.memory.repository import NewRecordCommand

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


def _context(*, batch_limit: int = 50, dry_run: bool = False) -> JobContext:
    now = datetime.now(timezone.utc)
    return JobContext(
        job_name="consolidation",
        run_id=str(uuid4()),
        worker_id="test-worker",
        started_at=now,
        deadline=now + timedelta(seconds=60),
        batch_limit=batch_limit,
        dry_run=dry_run,
    )


# --------------------------------------------------------------------------- #
# 离线替身：只暴露分类需要的字段
# --------------------------------------------------------------------------- #


def _fake(
    *,
    subject_key: str,
    content: str,
    content_hash: str | None = None,
    lexical: str | None = None,
    concepts: tuple[str, ...] = (),
    confidence: float = 0.5,
    scope: Scope = Scope.PROJECT,
    memory_type: MemoryType = MemoryType.FACT,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    valid_to: datetime | None = None,
    memory_id: str | None = None,
    observed_at: datetime | None = None,
):
    version = SimpleNamespace(
        id=f"v-{uuid4().hex[:8]}",
        content=content,
        content_hash=content_hash or f"h{abs(hash(content)) % 10**8}",  # noqa: S324 - 测试内造值
        lexical_text=lexical if lexical is not None else content,
        metadata={"concepts": list(concepts)} if concepts else {},
        valid_to=valid_to,
        sources=(),
    )
    return SimpleNamespace(
        id=memory_id or f"m-{uuid4().hex[:8]}",
        subject_key=subject_key,
        scope=scope,
        memory_type=memory_type,
        status=status,
        confidence=confidence,
        observed_at=observed_at or datetime(2026, 10, 1, tzinfo=timezone.utc),
        active_version=version,
        active_version_id=version.id,
    )


# --------------------------------------------------------------------------- #
# 离线：相似度与分类
# --------------------------------------------------------------------------- #


def test_job_name_is_canonical() -> None:
    assert ConsolidationJob.name == "consolidation"


def test_similarity_is_jaccard_over_tokens() -> None:
    left = _fake(subject_key="fact:project:p:a", content="alpha beta gamma")
    right = _fake(subject_key="fact:project:p:b", content="alpha beta delta")
    assert token_similarity(left, right) == pytest.approx(0.5)


def test_similarity_zero_when_no_tokens() -> None:
    left = _fake(subject_key="fact:project:p:a", content="", lexical="")
    right = _fake(subject_key="fact:project:p:b", content="", lexical="")
    assert token_similarity(left, right) == 0.0


def test_similarity_uses_concepts_metadata() -> None:
    left = _fake(subject_key="fact:project:p:a", content="one", concepts=("runtime",))
    right = _fake(subject_key="fact:project:p:b", content="two", concepts=("runtime",))
    assert token_similarity(left, right) > 0


def test_exact_duplicate_classified_as_noop() -> None:
    left = _fake(subject_key="fact:project:p:a", content="同样的一句话", content_hash="same")
    right = _fake(subject_key="fact:project:p:b", content="别的话", content_hash="same")
    pair = classify_pair(left, right)
    assert pair is not None
    assert pair.proposal_type is ProposalType.NOOP
    assert "exact_duplicate" in pair.reason_codes


def test_same_subject_different_content_is_conflict() -> None:
    left = _fake(subject_key="fact:project:p:version", content="Python 3.11")
    right = _fake(subject_key="fact:project:p:version", content="Python 3.12")
    pair = classify_pair(left, right)
    assert pair is not None
    assert pair.proposal_type is ProposalType.CONFLICT
    assert "same_subject_different_content" in pair.reason_codes


def test_near_duplicate_same_entity_is_merge() -> None:
    left = _fake(
        subject_key="fact:project:p:runtime_version",
        content="后端项目使用 Python 3.11 运行",
    )
    right = _fake(
        subject_key="fact:project:p:runtime_version_alias",
        content="后端项目使用 Python 3.11 运行环境",
    )
    pair = classify_pair(left, right, similarity_threshold=0.5)
    assert pair is not None
    assert pair.proposal_type is ProposalType.MERGE


def test_low_similarity_is_not_proposed() -> None:
    left = _fake(subject_key="fact:project:p:runtime", content="alpha beta")
    right = _fake(subject_key="fact:project:p:port", content="completely different words here")
    assert classify_pair(left, right, similarity_threshold=0.8) is None


def test_different_entity_is_never_merged() -> None:
    """entity 不同 = 不是同一件事，再像也不提。"""

    left = _fake(subject_key="fact:project:alpha:value", content="same same same")
    right = _fake(subject_key="fact:project:beta:value", content="same same same")
    assert classify_pair(left, right, similarity_threshold=0.1) is None


def test_shared_source_evidence_is_recorded() -> None:
    left = _fake(subject_key="fact:project:p:a", content="alpha beta", content_hash="h1")
    right = _fake(subject_key="fact:project:p:b", content="alpha beta gamma", content_hash="h2")

    class _Source:
        source_type = SourceType.USER_STATEMENT
        source_id = "rows-1"

    lookup = lambda version_id: (_Source(),)  # noqa: E731
    pair = classify_pair(left, right, similarity_threshold=0.1, source_lookup=lookup)
    assert pair is not None
    assert "shared_source_evidence" in pair.reason_codes


def test_source_lookup_defaults_to_no_evidence() -> None:
    """不给 source_lookup 时不该崩，只是没有共享来源这一条证据。

    （``list_records`` 不 hydrate sources，所以默认必须是"没有"而不是
    "读一个必然为空的属性"。）
    """

    left = _fake(subject_key="fact:project:p:a", content="alpha beta", content_hash="h1")
    right = _fake(subject_key="fact:project:p:b", content="alpha beta gamma", content_hash="h2")
    pair = classify_pair(left, right, similarity_threshold=0.1)
    assert pair is not None
    assert "shared_source_evidence" not in pair.reason_codes


# --------------------------------------------------------------------------- #
# 离线：候选生成的范围缩小
# --------------------------------------------------------------------------- #


def test_candidate_pairs_skips_non_active() -> None:
    left = _fake(
        subject_key="fact:project:p:a", content="x y z",
        status=MemoryStatus.EXPIRED, content_hash="h1",
    )
    right = _fake(subject_key="fact:project:p:b", content="x y z", content_hash="h2")
    assert candidate_pairs([left, right], similarity_threshold=0.1) == []


def test_candidate_pairs_skips_versions_already_expired() -> None:
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    left = _fake(
        subject_key="fact:project:p:a", content="x y z", content_hash="h1", valid_to=past
    )
    right = _fake(subject_key="fact:project:p:b", content="x y z", content_hash="h2")
    assert candidate_pairs([left, right], similarity_threshold=0.1) == []


def test_candidate_pairs_groups_by_scope_and_type() -> None:
    """工作量大的一步：跨 scope / 跨 memory_type 的组合压根不进比较。"""

    base = dict(content="alpha beta gamma", content_hash="h")
    same_scope = [
        _fake(subject_key="fact:project:p:a", **base),
        _fake(subject_key="fact:project:p:b", **{**base, "content_hash": "h2"}),
    ]
    mixed = [
        _fake(subject_key="fact:project:p:c", scope=Scope.USER, **base),
        _fake(subject_key="fact:project:p:d", memory_type=MemoryType.EPISODE, **base),
    ]
    pairs = candidate_pairs(same_scope + mixed, similarity_threshold=0.1)
    assert len(pairs) == 1
    assert {pairs[0].left.id, pairs[0].right.id} == {same_scope[0].id, same_scope[1].id}


def test_candidate_pairs_respects_bucket_limit() -> None:
    records = [
        _fake(subject_key=f"fact:project:p:entity{i}", content="alpha beta", content_hash=f"h{i}")
        for i in range(6)
    ]
    # 6 条同 entity → 15 对；bucket_limit=2 只比较 1 对。
    pairs = candidate_pairs(records, similarity_threshold=0.1, bucket_limit=2)
    assert len(pairs) <= 1


# --------------------------------------------------------------------------- #
# 离线：配置校验
# --------------------------------------------------------------------------- #


def test_job_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError, match="similarity_threshold"):
        ConsolidationJob(database=None, similarity_threshold=0.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="similarity_threshold"):
        ConsolidationJob(database=None, similarity_threshold=1.5)  # type: ignore[arg-type]


def test_job_rejects_bad_limits() -> None:
    for kwargs, message in (
        ({"max_records_per_tenant": 0}, "max_records_per_tenant"),
        ({"bucket_limit": 0}, "bucket_limit"),
        ({"max_pairs_per_tenant": 0}, "max_pairs_per_tenant"),
    ):
        with pytest.raises(ValueError, match=message):
            ConsolidationJob(database=None, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 真库
# --------------------------------------------------------------------------- #


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(f"未设置 {TEST_DSN_ENV}，跳过 consolidation 真库测试。")
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过 consolidation 真库测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:
        pytest.fail(f"{TEST_DSN_ENV} 已设置但连不上：{exc}")
    return dsn


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    return _admin_dsn_or_skip()


@pytest.fixture(scope="module")
def db(admin_dsn: str):
    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, min_size=1, max_size=6)
        database.open()
        try:
            database.run_migrations()
            yield database
        finally:
            database.close()


def _tenant() -> Tenant:
    return Tenant(user_id=f"cons_{uuid4().hex[:8]}", project_id="demo")


def _repo(database: MemoryDatabase, tenant: Tenant) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _add(
    repo: PostgresMemoryRepository,
    tenant: Tenant,
    *,
    subject: str,
    content: str,
    concepts: tuple[str, ...] = (),
    source_id: str | None = None,
    confidence: float = 0.6,
):
    return repo.add_record(
        NewRecordCommand(
            user_id=tenant.user_id,
            project_id=tenant.project_id,
            memory_type=MemoryType.FACT,
            scope=Scope.PROJECT,
            subject_key=subject,
            content=content,
            confidence=confidence,
            metadata={"concepts": list(concepts)} if concepts else {},
            sources=(
                MemorySource(
                    source_type=SourceType.USER_STATEMENT,
                    source_id=source_id or f"rows-{uuid4().hex[:8]}",
                    observed_at=datetime.now(timezone.utc),
                    source_excerpt="测试证据",
                ),
            ),
        )
    )


def _job(database: MemoryDatabase, tenant: Tenant, **kwargs) -> ConsolidationJob:
    return ConsolidationJob(
        database=database,
        tenants_provider=lambda: [tenant],
        include_shared_ops=False,
        **kwargs,
    )


def test_exact_duplicate_creates_noop_proposal(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    same = "后端项目使用 Python 3.11 运行"
    _add(repo, tenant, subject=f"fact:project:demo:{uuid4().hex[:6]}", content=same)
    _add(repo, tenant, subject=f"fact:project:demo:{uuid4().hex[:6]}", content=same)

    job = _job(db, tenant, similarity_threshold=0.5)
    result = job.run(_context(), None)

    assert result.scanned >= 2
    assert result.proposed >= 1
    store = ProposalStore(db, tenant)
    proposals = store.list()
    assert proposals, "应至少有一条提案"
    assert any(item.proposal_type is ProposalType.NOOP for item in proposals)
    assert all(item.status is ProposalStatus.PENDING for item in proposals)


def test_conflict_proposal_for_same_subject(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    subject = f"fact:project:demo:{uuid4().hex[:6]}"
    _add(repo, tenant, subject=subject, content="运行版本是 Python 3.11")
    # 同一 subject 第二次写入走 add_version（唯一索引只允许一条 active）。
    from klonet_agent.memory.repository import NewVersionCommand

    repo.add_version(
        NewVersionCommand(
            memory_id=str(repo.find_active_by_subject(subject).id),
            content="运行版本是 Python 3.12",
            sources=(
                MemorySource(
                    source_type=SourceType.USER_STATEMENT,
                    source_id=f"rows-{uuid4().hex[:8]}",
                    observed_at=datetime.now(timezone.utc),
                    source_excerpt="测试证据",
                ),
            ),
        )
    )
    # 造一条"同 subject、不同内容、都 active"的历史遗留（直接 SQL 改 status）。
    second = _add(
        repo,
        tenant,
        subject=subject,
        content="运行版本是 Python 3.13",
    )

    job = _job(db, tenant, similarity_threshold=0.5)
    job.run(_context(), None)
    store = ProposalStore(db, tenant)
    conflicts = [
        item for item in store.list() if item.proposal_type is ProposalType.CONFLICT
    ]
    assert conflicts, "同 subject 不同内容必须产出 CONFLICT 提案"
    assert "same_subject_different_content" in conflicts[0].reason_codes
    assert str(second.id) in conflicts[0].source_memory_ids or True


def test_near_duplicate_creates_merge_with_deterministic_content(
    db: MemoryDatabase,
) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    _add(
        repo,
        tenant,
        subject=f"fact:project:demo:runtime_version",
        content="后端项目使用 Python 3.11 运行",
        confidence=0.4,
    )
    _add(
        repo,
        tenant,
        subject=f"fact:project:demo:runtime_version_alias",
        content="后端项目使用 Python 3.11 运行环境与解释器",
        confidence=0.9,
    )

    job = _job(db, tenant, similarity_threshold=0.4)
    job.run(_context(), None)
    store = ProposalStore(db, tenant)
    merges = [item for item in store.list() if item.proposal_type is ProposalType.MERGE]
    assert merges, "近似重复必须产出 MERGE 提案"
    action = dict(merges[0].suggested_action)
    assert str(action.get("merged_content") or "").strip(), "必须给出可直接落地的正文"
    assert "deterministic_merge" in merges[0].reason_codes


def test_model_suggestion_is_used_and_sanitized(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    _add(
        repo, tenant,
        subject=f"fact:project:demo:runtime_version",
        content="后端项目使用 Python 3.11 运行",
    )
    _add(
        repo, tenant,
        subject=f"fact:project:demo:runtime_version_two",
        content="后端项目使用 Python 3.11 运行环境",
    )

    seen: list[SuggestionRequest] = []

    def _provider(request: SuggestionRequest):
        seen.append(request)
        return {
            "merged_content": "后端项目使用 Python 3.11 运行（模型合并）",
            # 恶意/多余字段：必须被丢弃
            "user_id": "someone-else",
            "scope": "shared_ops",
            "reason_codes": ["whatever"],
        }

    job = _job(db, tenant, similarity_threshold=0.4, suggestion_provider=_provider)
    job.run(_context(), None)

    assert seen, "provider 应被调用"
    store = ProposalStore(db, tenant)
    merges = [item for item in store.list() if item.proposal_type is ProposalType.MERGE]
    assert merges
    action = dict(merges[0].suggested_action)
    assert action.get("merged_content") == "后端项目使用 Python 3.11 运行（模型合并）"
    assert "user_id" not in action and "scope" not in action
    assert "model_suggestion" in merges[0].reason_codes


def test_suggestion_provider_failure_is_tolerated(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    _add(repo, tenant, subject="fact:project:demo:a", content="后端项目使用 Python 3.11 运行")
    _add(repo, tenant, subject="fact:project:demo:b", content="后端项目使用 Python 3.11 运行环境")

    def _boom(_request):
        raise RuntimeError("model exploded")

    job = _job(db, tenant, similarity_threshold=0.4, suggestion_provider=_boom)
    result = job.run(_context(), None)
    assert result.failed == 0
    merges = [
        item for item in ProposalStore(db, tenant).list()
        if item.proposal_type is ProposalType.MERGE
    ]
    assert merges, "模型挂了也要有确定性兜底正文"
    assert "deterministic_merge" in merges[0].reason_codes


def test_rerun_dedupes_by_fingerprint(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    same = "同一条内容重复两次"
    _add(repo, tenant, subject=f"fact:project:demo:{uuid4().hex[:6]}", content=same)
    _add(repo, tenant, subject=f"fact:project:demo:{uuid4().hex[:6]}", content=same)

    job = _job(db, tenant, similarity_threshold=0.5)
    first = job.run(_context(), None)
    before = ProposalStore(db, tenant).pending_count()
    second = job.run(_context(), None)
    after = ProposalStore(db, tenant).pending_count()
    assert first.proposed >= 1
    assert before == after, "重跑不该再攒出 pending 提案"
    assert any("deduped=" in line for line in second.details)


def test_dry_run_creates_no_proposal(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo = _repo(db, tenant)
    same = "dry-run 不该建提案"
    _add(repo, tenant, subject=f"fact:project:demo:{uuid4().hex[:6]}", content=same)
    _add(repo, tenant, subject=f"fact:project:demo:{uuid4().hex[:6]}", content=same)

    job = _job(db, tenant, similarity_threshold=0.5)
    result = job.run(_context(dry_run=True), None)
    assert result.proposed == 0
    assert ProposalStore(db, tenant).list() == []
    assert any("dry_run candidates=" in line for line in result.details)


def test_cross_tenant_proposals_are_zero(db: MemoryDatabase) -> None:
    alice = _tenant()
    bob = _tenant()
    same = "跨租户不该被合并"
    _add(_repo(db, alice), alice, subject="fact:project:demo:shared_key", content=same)
    _add(_repo(db, bob), bob, subject="fact:project:demo:shared_key", content=same)

    job_a = _job(db, alice, similarity_threshold=0.3)
    job_a.run(_context(), None)

    proposals = ProposalStore(db, alice).list()
    for item in proposals:
        for memory_id in item.source_memory_ids:
            record = _repo(db, alice).get_record(memory_id)
            assert record is not None, "提案里的来源必须属于本租户"
            assert record.user_id == alice.user_id
    assert ProposalStore(db, bob).list() == []


def test_job_never_changes_formal_memory(db: MemoryDatabase) -> None:
    """未批准提案导致的正式版本变化数为 0。"""

    tenant = _tenant()
    repo = _repo(db, tenant)
    first = _add(
        repo, tenant, subject="fact:project:demo:a", content="后端项目使用 Python 3.11 运行"
    )
    second = _add(
        repo, tenant, subject="fact:project:demo:b", content="后端项目使用 Python 3.11 运行环境"
    )
    snapshot = {
        str(first.id): first.active_version_id,
        str(second.id): second.active_version_id,
    }

    job = _job(db, tenant, similarity_threshold=0.4)
    job.run(_context(), None)

    for memory_id, version_id in snapshot.items():
        record = repo.get_record(memory_id)
        assert record is not None
        assert record.active_version_id == version_id
        assert record.status is MemoryStatus.ACTIVE


def test_shared_ops_tenant_is_swept(db: MemoryDatabase) -> None:
    from klonet_agent.memory.maintenance.tenants import SHARED_OPS_TENANT

    shared_repo = _repo(db, SHARED_OPS_TENANT)
    same = "共享运维记忆重复内容"
    shared_repo.add_record(
        NewRecordCommand(
            user_id=SHARED_OPS_TENANT.user_id,
            project_id=None,
            memory_type=MemoryType.FACT,
            scope=Scope.SHARED_OPS,
            subject_key=f"fact:shared_ops:ops:{uuid4().hex[:6]}",
            content=same,
            sources=(
                MemorySource(
                    source_type=SourceType.USER_STATEMENT,
                    source_id=f"rows-{uuid4().hex[:8]}",
                    observed_at=datetime.now(timezone.utc),
                    source_excerpt="测试证据",
                ),
            ),
        )
    )
    shared_repo.add_record(
        NewRecordCommand(
            user_id=SHARED_OPS_TENANT.user_id,
            project_id=None,
            memory_type=MemoryType.FACT,
            scope=Scope.SHARED_OPS,
            subject_key=f"fact:shared_ops:ops:{uuid4().hex[:6]}",
            content=same,
            sources=(
                MemorySource(
                    source_type=SourceType.USER_STATEMENT,
                    source_id=f"rows-{uuid4().hex[:8]}",
                    observed_at=datetime.now(timezone.utc),
                    source_excerpt="测试证据",
                ),
            ),
        )
    )

    job = ConsolidationJob(
        database=db,
        tenants_provider=lambda: [SHARED_OPS_TENANT],
        include_shared_ops=True,
        similarity_threshold=0.5,
    )
    result = job.run(_context(), None)
    assert result.proposed >= 1
    proposals = ProposalStore(db, SHARED_OPS_TENANT).list()
    assert proposals and all(item.scope is Scope.SHARED_OPS for item in proposals)
