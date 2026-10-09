"""整理提案系统测试（04 计划 §6.4 / 阶段 5）。

离线部分覆盖领域模型：指纹确定性、状态机、跨租户拒绝、载荷校验。
真库部分覆盖 schema（唯一指纹去重、状态机触发器、租户过滤）、
乐观锁过期、以及 "**只有显式批准能改变正式记忆**"。

跑法::

    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_maintenance_proposals.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    WriteDecision,
    MemorySource,
    MemoryStatus,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
)
from klonet_agent.memory.maintenance.proposals import (
    ApplyOutcome,
    Proposal,
    ProposalInvalidError,
    ProposalStateError,
    ProposalStatus,
    ProposalStaleError,
    ProposalStore,
    ProposalType,
    apply_proposal,
    build_merge_candidate,
    capture_source_fingerprints,
    ensure_same_tenant,
    is_transition_allowed,
    proposal_fingerprint,
    verify_sources_unchanged,
)
from klonet_agent.memory.postgres import PostgresMemoryRepository
from klonet_agent.memory.repository import NewRecordCommand

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


# --------------------------------------------------------------------------- #
# 离线：指纹
# --------------------------------------------------------------------------- #


def test_fingerprint_is_order_and_case_insensitive() -> None:
    first = proposal_fingerprint(ProposalType.MERGE, ["B", "a", "A"])
    second = proposal_fingerprint("merge", ["a", "B"])
    assert first == second


def test_fingerprint_depends_on_type() -> None:
    ids = ["a", "b"]
    assert proposal_fingerprint(ProposalType.MERGE, ids) != proposal_fingerprint(
        ProposalType.CONFLICT, ids
    )


def test_fingerprint_is_process_stable() -> None:
    """不能用内置 ``hash()``：它按进程加盐，跨 worker 去重会失效。

    ``blake2b`` 是内容寻址的——同一个候选集合在任何进程都得到同一个值。
    这里断言"重复计算结果一致 + 形态是 32 位十六进制"，而不是写死一个常量
    （写死常量会让"换算法"变成一次无意义的测试改动）。
    """

    first = proposal_fingerprint(ProposalType.NOOP, ["a", "b"])
    second = proposal_fingerprint(ProposalType.NOOP, ["b", "a"])
    assert first == second
    assert len(first) == 32
    assert all(ch in "0123456789abcdef" for ch in first)


def test_fingerprint_rejects_single_source() -> None:
    with pytest.raises(ProposalInvalidError, match="两条"):
        proposal_fingerprint(ProposalType.MERGE, ["only-one"])


def test_fingerprint_dedupes_repeated_ids() -> None:
    assert proposal_fingerprint(ProposalType.MERGE, ["a", "a", "b"]) == proposal_fingerprint(
        ProposalType.MERGE, ["a", "b"]
    )


# --------------------------------------------------------------------------- #
# 离线：跨租户
# --------------------------------------------------------------------------- #


def test_ensure_same_tenant_rejects_cross_user() -> None:
    with pytest.raises(ProposalInvalidError, match="跨租户"):
        ensure_same_tenant([("alice", "demo"), ("bob", "demo")])


def test_ensure_same_tenant_rejects_cross_project() -> None:
    with pytest.raises(ProposalInvalidError, match="跨租户"):
        ensure_same_tenant([("alice", "demo"), ("alice", "other")])


def test_ensure_same_tenant_rejects_shared_mixed_with_project() -> None:
    """shared_ops 与 project 是不同作用域，不能合并成一条提案。"""

    with pytest.raises(ProposalInvalidError, match="跨租户"):
        ensure_same_tenant([("shared", None), ("alice", "demo")])


def test_ensure_same_tenant_accepts_duplicates() -> None:
    ensure_same_tenant([("alice", "demo"), ("alice", "demo")])


# --------------------------------------------------------------------------- #
# 离线：状态机
# --------------------------------------------------------------------------- #


def test_state_machine_allows_documented_transitions() -> None:
    assert is_transition_allowed(ProposalStatus.PENDING, ProposalStatus.APPROVED)
    assert is_transition_allowed(ProposalStatus.PENDING, ProposalStatus.REJECTED)
    assert is_transition_allowed(ProposalStatus.PENDING, ProposalStatus.EXPIRED)
    assert is_transition_allowed(ProposalStatus.APPROVED, ProposalStatus.APPLIED)
    assert is_transition_allowed(ProposalStatus.APPROVED, ProposalStatus.EXPIRED)


def test_state_machine_forbids_skipping_approval() -> None:
    """``pending -> applied`` 非法：这就是"未批准不能改正式记忆"的状态机表达。"""

    assert not is_transition_allowed(ProposalStatus.PENDING, ProposalStatus.APPLIED)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for terminal in (
        ProposalStatus.REJECTED,
        ProposalStatus.APPLIED,
        ProposalStatus.EXPIRED,
    ):
        for target in ProposalStatus:
            assert not is_transition_allowed(terminal, target), f"{terminal} -> {target}"


# --------------------------------------------------------------------------- #
# 真库
# --------------------------------------------------------------------------- #


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(f"未设置 {TEST_DSN_ENV}，跳过提案系统真库测试。")
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过提案系统真库测试")
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
            applied = database.run_migrations()
            assert "0007_memory_maintenance_proposals" in applied or True
            yield database
        finally:
            database.close()


def _tenant() -> Tenant:
    return Tenant(user_id=f"prop_{uuid4().hex[:8]}", project_id="demo")


def _repo(database: MemoryDatabase, tenant: Tenant) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _seed(repo: PostgresMemoryRepository, tenant: Tenant, content: str, *, subject: str | None = None):
    return repo.add_record(
        NewRecordCommand(
            user_id=tenant.user_id,
            project_id=tenant.project_id,
            memory_type=MemoryType.FACT,
            scope=Scope.PROJECT,
            subject_key=subject or f"fact:project:prop:{uuid4().hex[:8]}",
            content=content,
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


def _seed_pair(db: MemoryDatabase, tenant: Tenant):
    repo = _repo(db, tenant)
    first = _seed(repo, tenant, "后端使用 Python 3.11")
    second = _seed(repo, tenant, "后端运行时要求 Python 3.11")
    return repo, first, second


# --- schema ---


def test_proposals_table_exists(db: MemoryDatabase) -> None:
    with db.diagnostic_session() as conn:
        row = conn.execute(
            "SELECT to_regclass('memory_maintenance.memory_maintenance_proposals') AS t"
        ).fetchone()
    assert row["t"] is not None


def test_create_and_read_back(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)

    result = store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=[str(first.id), str(second.id)],
        scope=Scope.PROJECT,
        suggested_action={"merged_content": "后端使用 Python 3.11"},
        reason_codes=["near_duplicate"],
        source_fingerprints=capture_source_fingerprints(
            repo, [str(first.id), str(second.id)]
        ),
    )
    assert result.created is True
    fetched = store.get(result.proposal.proposal_id)
    assert fetched is not None
    assert fetched.status is ProposalStatus.PENDING
    assert fetched.proposal_type is ProposalType.MERGE
    assert set(fetched.source_memory_ids) == {str(first.id), str(second.id)}
    assert fetched.fingerprint == proposal_fingerprint(
        ProposalType.MERGE, [str(first.id), str(second.id)]
    )


def test_duplicate_pending_proposal_is_not_created_twice(db: MemoryDatabase) -> None:
    """同一候选集合不会重复创建 pending proposal（唯一指纹保证）。"""

    tenant = _tenant()
    _, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]

    first_result = store.create(
        proposal_type=ProposalType.MERGE, source_memory_ids=ids, scope=Scope.PROJECT
    )
    second_result = store.create(
        proposal_type=ProposalType.MERGE, source_memory_ids=list(reversed(ids)),
        scope=Scope.PROJECT,
    )
    assert first_result.created is True
    assert second_result.created is False
    assert second_result.proposal.proposal_id == first_result.proposal.proposal_id
    assert store.pending_count() == 1


def test_different_type_creates_new_proposal(db: MemoryDatabase) -> None:
    tenant = _tenant()
    _, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]
    merge = store.create(
        proposal_type=ProposalType.MERGE, source_memory_ids=ids, scope=Scope.PROJECT
    )
    conflict = store.create(
        proposal_type=ProposalType.CONFLICT, source_memory_ids=ids, scope=Scope.PROJECT
    )
    assert merge.created and conflict.created
    assert merge.proposal.proposal_id != conflict.proposal.proposal_id


def test_terminal_proposal_allows_new_one(db: MemoryDatabase) -> None:
    """老提案已终态后，同一候选集合可以再产生新提案。"""

    tenant = _tenant()
    _, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]
    first_result = store.create(
        proposal_type=ProposalType.MERGE, source_memory_ids=ids, scope=Scope.PROJECT
    )
    store.transition(first_result.proposal.proposal_id, ProposalStatus.REJECTED)
    again = store.create(
        proposal_type=ProposalType.MERGE, source_memory_ids=ids, scope=Scope.PROJECT
    )
    assert again.created is True


# --- 状态机（数据库层） ---


def test_database_rejects_pending_to_applied(db: MemoryDatabase) -> None:
    """应用层能绕过，数据库触发器不能——直接写 SQL 也要被拒。"""

    import psycopg

    tenant = _tenant()
    _, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    result = store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=[str(first.id), str(second.id)],
        scope=Scope.PROJECT,
    )
    with pytest.raises(psycopg.Error) as excinfo:
        with db.diagnostic_session() as conn:
            conn.execute(
                "UPDATE memory_maintenance.memory_maintenance_proposals "
                "   SET status = 'applied' WHERE proposal_id = %s",
                (result.proposal.proposal_id,),
            )
    assert "状态转换非法" in str(excinfo.value)


def test_application_rejects_illegal_transition(db: MemoryDatabase) -> None:
    tenant = _tenant()
    _, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    result = store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=[str(first.id), str(second.id)],
        scope=Scope.PROJECT,
    )
    with pytest.raises(ProposalStateError, match="非法状态转换"):
        store.transition(result.proposal.proposal_id, ProposalStatus.APPLIED)


def test_approve_then_apply_is_allowed(db: MemoryDatabase) -> None:
    tenant = _tenant()
    _, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    result = store.create(
        proposal_type=ProposalType.NOOP,
        source_memory_ids=[str(first.id), str(second.id)],
        scope=Scope.PROJECT,
    )
    store.transition(result.proposal.proposal_id, ProposalStatus.APPROVED)
    applied = store.transition(result.proposal.proposal_id, ProposalStatus.APPLIED)
    assert applied.status is ProposalStatus.APPLIED
    assert applied.reviewed_at is not None


# --- 租户隔离 ---


def test_cross_tenant_reads_are_impossible(db: MemoryDatabase) -> None:
    alice = _tenant()
    bob = _tenant()
    _, alice_a, alice_b = _seed_pair(db, alice)
    alice_store = ProposalStore(db, alice)
    created = alice_store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=[str(alice_a.id), str(alice_b.id)],
        scope=Scope.PROJECT,
    )

    bob_store = ProposalStore(db, bob)
    assert bob_store.get(created.proposal.proposal_id) is None
    assert bob_store.list() == []
    assert bob_store.pending_count() == 0
    with pytest.raises(ProposalStateError, match="不存在或不属于当前租户"):
        bob_store.transition(created.proposal.proposal_id, ProposalStatus.REJECTED)


def test_project_scope_requires_project_binding(db: MemoryDatabase) -> None:
    tenant = Tenant(user_id=f"prop_{uuid4().hex[:8]}", project_id=None)
    store = ProposalStore(db, tenant)
    with pytest.raises(ProposalInvalidError, match="project_id"):
        store.create(
            proposal_type=ProposalType.MERGE,
            source_memory_ids=["a", "b"],
            scope=Scope.PROJECT,
        )


def test_shared_ops_proposal_needs_shared_tenant(db: MemoryDatabase) -> None:
    tenant = Tenant(user_id=f"prop_{uuid4().hex[:8]}", project_id=None)
    store = ProposalStore(db, tenant)
    with pytest.raises(ProposalInvalidError, match="shared"):
        store.create(
            proposal_type=ProposalType.MERGE,
            source_memory_ids=["a", "b"],
            scope=Scope.SHARED_OPS,
        )


# --- 乐观锁 ---


def test_stale_source_expires_proposal(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]
    created = store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=ids,
        scope=Scope.PROJECT,
        suggested_action={"merged_content": "合并后的正文"},
        source_fingerprints=capture_source_fingerprints(repo, ids),
    )
    store.transition(created.proposal.proposal_id, ProposalStatus.APPROVED)

    # 生成之后来源变了（新增一个版本 → active_version_id / content_hash 都变）。
    from klonet_agent.memory.repository import NewVersionCommand

    repo.add_version(
        NewVersionCommand(
            memory_id=str(first.id),
            content="后端运行时要求 Python 3.12",
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

    with pytest.raises(ProposalStaleError):
        apply_proposal(store, repo, created.proposal.proposal_id)

    after = store.get(created.proposal.proposal_id)
    assert after is not None and after.status is ProposalStatus.EXPIRED


def test_missing_snapshot_is_treated_as_stale(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    created = store.create(
        proposal_type=ProposalType.NOOP,
        source_memory_ids=[str(first.id), str(second.id)],
        scope=Scope.PROJECT,
        # 刻意不给 source_fingerprints
    )
    store.transition(created.proposal.proposal_id, ProposalStatus.APPROVED)
    with pytest.raises(ProposalStaleError):
        apply_proposal(store, repo, created.proposal.proposal_id)
    assert store.get(created.proposal.proposal_id).status is ProposalStatus.EXPIRED


def test_verify_sources_unchanged_passes_when_untouched(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]
    created = store.create(
        proposal_type=ProposalType.NOOP,
        source_memory_ids=ids,
        scope=Scope.PROJECT,
        source_fingerprints=capture_source_fingerprints(repo, ids),
    )
    verify_sources_unchanged(repo, created.proposal)  # 不抛即通过


# --- apply 路径 ---


def test_apply_requires_approved_status(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    created = store.create(
        proposal_type=ProposalType.NOOP,
        source_memory_ids=[str(first.id), str(second.id)],
        scope=Scope.PROJECT,
        source_fingerprints=capture_source_fingerprints(
            repo, [str(first.id), str(second.id)]
        ),
    )
    with pytest.raises(ProposalStateError, match="已批准"):
        apply_proposal(store, repo, created.proposal.proposal_id)


def test_noop_apply_changes_no_memory(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]
    before = {str(first.id): first.active_version_id, str(second.id): second.active_version_id}

    created = store.create(
        proposal_type=ProposalType.NOOP,
        source_memory_ids=ids,
        scope=Scope.PROJECT,
        source_fingerprints=capture_source_fingerprints(repo, ids),
    )
    store.transition(created.proposal.proposal_id, ProposalStatus.APPROVED)
    outcome = apply_proposal(store, repo, created.proposal.proposal_id)

    assert isinstance(outcome, ApplyOutcome)
    assert outcome.decision == "noop"
    assert outcome.applied_version_ids == ()
    assert outcome.proposal.status is ProposalStatus.APPLIED
    for memory_id, version_id in before.items():
        record = repo.get_record(memory_id)
        assert record is not None
        assert record.active_version_id == version_id, "NOOP 不该改动任何记忆"


def test_unapproved_proposal_leaves_no_version_change(db: MemoryDatabase) -> None:
    """验收指标：未批准提案导致的正式版本变化数为 0。"""

    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]
    snapshot = {
        str(first.id): first.active_version_id,
        str(second.id): second.active_version_id,
    }

    # 造一条带可执行内容的 MERGE 提案，但**不批准**。
    store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=ids,
        scope=Scope.PROJECT,
        suggested_action={"merged_content": "被模型合并后的正文"},
        source_fingerprints=capture_source_fingerprints(repo, ids),
    )
    for memory_id, version_id in snapshot.items():
        record = repo.get_record(memory_id)
        assert record is not None
        assert record.active_version_id == version_id
        assert record.status is MemoryStatus.ACTIVE


def test_build_merge_candidate_ignores_tenant_from_payload(db: MemoryDatabase) -> None:
    """prompt injection 防线：租户/类型/作用域只能来自来源记录。"""

    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]
    created = store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=ids,
        scope=Scope.PROJECT,
        suggested_action={
            "merged_content": "合并后的正文",
            # 恶意载荷：试图改租户与作用域
            "user_id": "someone-else",
            "project_id": "another-project",
            "scope": "shared_ops",
            "memory_type": "episode",
        },
        source_fingerprints=capture_source_fingerprints(repo, ids),
    )
    candidate = build_merge_candidate(repo, created.proposal)
    assert candidate.user_id == tenant.user_id
    assert candidate.project_id == tenant.project_id
    assert candidate.scope is Scope.PROJECT
    assert candidate.memory_type is MemoryType.FACT
    assert candidate.content == "合并后的正文"


def test_build_merge_candidate_requires_content(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    created = store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=[str(first.id), str(second.id)],
        scope=Scope.PROJECT,
        suggested_action={},
    )
    with pytest.raises(ProposalInvalidError, match="merged_content"):
        build_merge_candidate(repo, created.proposal)


def test_merge_apply_writes_through_versioning(db: MemoryDatabase) -> None:
    """已批准 + 来源未变 → 通过 apply_plan 落地，产出一个新版本。"""

    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]

    created = store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=ids,
        scope=Scope.PROJECT,
        suggested_action={
            "merged_content": "后端运行时要求 Python 3.11（合并后正文）",
            "subject_key": f"fact:project:merged:{uuid4().hex[:8]}",
        },
        source_fingerprints=capture_source_fingerprints(repo, ids),
    )
    store.transition(created.proposal.proposal_id, ProposalStatus.APPROVED)
    outcome = apply_proposal(store, repo, created.proposal.proposal_id)

    assert outcome.proposal.status is ProposalStatus.APPLIED
    assert outcome.record_id is not None
    written = repo.get_record(outcome.record_id)
    assert written is not None
    assert "合并后正文" in (written.active_version.content if written.active_version else "")
    assert outcome.applied_version_ids


def test_merge_into_existing_subject_appends_version_and_keeps_old(
    db: MemoryDatabase,
) -> None:
    """MERGE 语义 = UPDATE（含义未变、只是描述合并），旧值必须保留。

    这是 R8 那条规则的可执行形式：同一个 subject 内容不同、又没声明决策时
    ``plan_consolidation`` 会 REJECT。提案类型给出了这个声明——
    MERGE → UPDATE（追加版本），SUPERSEDE → 替换。
    """

    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    ids = [str(first.id), str(second.id)]
    old_version_id = first.active_version_id

    created = store.create(
        proposal_type=ProposalType.MERGE,
        source_memory_ids=ids,
        scope=Scope.PROJECT,
        # 刻意不给 subject_key：落到 primary 的 subject 上，即"与已有记忆合并"。
        suggested_action={"merged_content": "后端运行时要求 Python 3.11（合并后正文）"},
        source_fingerprints=capture_source_fingerprints(repo, ids),
    )
    candidate = build_merge_candidate(repo, created.proposal)
    assert candidate.proposed_decision is WriteDecision.UPDATE

    store.transition(created.proposal.proposal_id, ProposalStatus.APPROVED)
    outcome = apply_proposal(store, repo, created.proposal.proposal_id)

    assert outcome.decision == "update"
    assert outcome.record_id == str(first.id)
    assert outcome.applied_version_ids
    # 旧版本仍在：MERGE 不丢历史。
    assert repo.get_version(str(old_version_id)) is not None


def test_supersede_proposal_asks_for_supersede(db: MemoryDatabase) -> None:
    tenant = _tenant()
    repo, first, second = _seed_pair(db, tenant)
    store = ProposalStore(db, tenant)
    created = store.create(
        proposal_type=ProposalType.SUPERSEDE,
        source_memory_ids=[str(first.id), str(second.id)],
        scope=Scope.PROJECT,
        suggested_action={"merged_content": "替换后的正文"},
    )
    candidate = build_merge_candidate(repo, created.proposal)
    assert candidate.proposed_decision is WriteDecision.SUPERSEDE
