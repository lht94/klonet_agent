"""阶段 2（原子记忆与版本管理）的测试。

分两部分：

* **纯决策测试**（不需要数据库）：``plan_consolidation`` 是纯函数，规则表里的
  十三条规则在这里逐条钉死。这部分在任何机器上都能跑。
* **真库测试**（没有 DSN 时整块 skip）：版本不可变触发器、同 subject 替代的
  原子性、有效期与状态机、以及 consolidation 的端到端效果。这几条离开真库无法证明。

跑法：

    ./scripts/pg_local.sh up
    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_versioning.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    ALLOWED_SCOPES_BY_TYPE,
    ConsolidationPlan,
    InvalidStatusTransitionError,
    MemoryCandidate,
    MemoryDomainError,
    MemoryQuery,
    MemoryRecord,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryVersion,
    RelationType,
    Scope,
    SourceType,
    Tenant,
    WriteDecision,
    additional_source_keys,
    allowed_status_transitions,
    confidence_ceiling,
    content_hash,
    ensure_status_transition,
    importance_for_level,
    is_valid_at,
    qualifies_as_verified,
    same_content,
    type_allows_supersede,
    validate_type_scope,
    validate_validity_window,
)
from klonet_agent.memory.postgres import PostgresMemoryRepository
from klonet_agent.memory.repository import (
    NewRecordCommand,
    NewVersionCommand,
    RecordNotActiveError,
)
from klonet_agent.memory.versioning import apply_plan, plan_consolidation

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"

ALICE = Tenant(user_id="alice", project_id="demo")

FACT_SUBJECT = "fact:project:demo:python_version"
PREF_SUBJECT = "preference:project:demo:answer_style"
EPISODE_SUBJECT = "episode:evt-0001"

PY38 = "本项目运行时要求 Python 3.8"
PY311 = "本项目运行时要求 Python 3.11"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #


def _src(
    kind: SourceType = SourceType.USER_STATEMENT,
    *,
    sid: str | None = None,
    excerpt: str = "用户在对话里明确说明",
) -> MemorySource:
    return MemorySource(
        source_type=kind,
        source_id=sid or f"rows-{uuid4().hex[:8]}",
        observed_at=_now(),
        source_excerpt=excerpt,
    )


def _candidate(
    *,
    content: str = PY311,
    subject_key: str = FACT_SUBJECT,
    memory_type: MemoryType = MemoryType.FACT,
    scope: Scope = Scope.PROJECT,
    project_id: str | None = "demo",
    sources: tuple[MemorySource, ...] | None = None,
    proposed: WriteDecision | None = None,
    verified: bool = False,
    confidence: float = 0.6,
) -> MemoryCandidate:
    if sources is None:
        sources = (_src(),)
    return MemoryCandidate(
        memory_type=memory_type,
        scope=scope,
        subject_key=subject_key,
        content=content,
        user_id=ALICE.user_id,
        project_id=project_id,
        sources=sources,
        proposed_decision=proposed,
        verified=verified,
        confidence=confidence,
    )


def _version(
    *,
    content: str = PY38,
    verified: bool = False,
    sources: tuple[MemorySource, ...] = (),
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    version: int = 1,
) -> MemoryVersion:
    return MemoryVersion(
        id=str(uuid4()),
        memory_id=str(uuid4()),
        version=version,
        content=content,
        observed_at=valid_from or _now(),
        valid_from=valid_from or _now(),
        valid_to=valid_to,
        content_hash=content_hash(content),
        verified=verified,
        sources=sources,
    )


def _record(
    *,
    subject_key: str = FACT_SUBJECT,
    memory_type: MemoryType = MemoryType.FACT,
    scope: Scope = Scope.PROJECT,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    confidence: float = 0.6,
    active_version: MemoryVersion | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        id=str(uuid4()),
        user_id=ALICE.user_id,
        scope=scope,
        project_id="demo" if scope is Scope.PROJECT else None,
        memory_type=memory_type,
        subject_key=subject_key,
        status=status,
        confidence=confidence,
        active_version=active_version,
    )


# --------------------------------------------------------------------------- #
# 领域规则：三类记忆的类型-作用域组合
# --------------------------------------------------------------------------- #


def test_type_scope_matrix_is_enforced() -> None:
    # 事实与经历允许三种作用域，偏好只允许 user/project。
    assert ALLOWED_SCOPES_BY_TYPE[MemoryType.PREFERENCE] == {
        Scope.USER,
        Scope.PROJECT,
    }
    validate_type_scope(MemoryType.FACT, Scope.SHARED_OPS)
    validate_type_scope(MemoryType.EPISODE, Scope.USER)

    with pytest.raises(MemoryDomainError):
        validate_type_scope(MemoryType.PREFERENCE, Scope.SHARED_OPS)
    with pytest.raises(MemoryDomainError):
        validate_type_scope("not-a-type", Scope.USER)


def test_preference_candidate_rejects_shared_ops_scope() -> None:
    # 共享运维记忆里放"某人的偏好"没有意义，构造候选时就要拦下来。
    with pytest.raises(MemoryDomainError):
        _candidate(
            memory_type=MemoryType.PREFERENCE,
            scope=Scope.SHARED_OPS,
            subject_key="preference:shared_ops:team:style",
            project_id=None,
        )


def test_only_fact_and_preference_participate_in_supersede() -> None:
    assert type_allows_supersede(MemoryType.FACT)
    assert type_allows_supersede(MemoryType.PREFERENCE)
    assert not type_allows_supersede(MemoryType.EPISODE)


def test_episode_candidate_needs_event_identity_and_project_scope_consistency() -> None:
    episode = _candidate(
        memory_type=MemoryType.EPISODE,
        scope=Scope.USER,
        project_id=None,
        subject_key=EPISODE_SUBJECT,
        content="这次部署失败是因为目标端口被占用",
    )
    assert episode.subject_key == EPISODE_SUBJECT

    # episode 的 subject_key 不能带 entity/attribute 三段式。
    with pytest.raises(MemoryDomainError):
        _candidate(
            memory_type=MemoryType.EPISODE,
            scope=Scope.USER,
            project_id=None,
            subject_key="episode:demo:port",
        )


def test_project_scoped_candidate_requires_project_id() -> None:
    with pytest.raises(MemoryDomainError):
        _candidate(project_id=None)
    with pytest.raises(MemoryDomainError):
        # 非项目作用域不允许带 project_id，否则租户过滤会出现两种口径。
        _candidate(scope=Scope.USER, project_id="demo", subject_key="fact:user:demo:x")


# --------------------------------------------------------------------------- #
# 领域规则：verified
# --------------------------------------------------------------------------- #


def test_fact_verified_accepts_user_statement_or_tool_result() -> None:
    assert qualifies_as_verified(MemoryType.FACT, [_src(SourceType.USER_STATEMENT)])
    assert qualifies_as_verified(MemoryType.FACT, [_src(SourceType.TOOL_RESULT)])
    # 原始对话事件里也有 assistant 自述，不能算证据。
    assert not qualifies_as_verified(MemoryType.FACT, [_src(SourceType.HISTORY_EVENT)])
    assert not qualifies_as_verified(MemoryType.FACT, [])


def test_preference_verified_requires_explicit_user_statement() -> None:
    assert qualifies_as_verified(
        MemoryType.PREFERENCE, [_src(SourceType.USER_STATEMENT)]
    )
    # 工具证据不能替用户决定主观偏好（§6.5 规则 2）。
    assert not qualifies_as_verified(
        MemoryType.PREFERENCE, [_src(SourceType.TOOL_RESULT)]
    )


def test_self_declared_verification_without_evidence_is_rejected() -> None:
    with pytest.raises(MemoryDomainError):
        _candidate(verified=True, sources=(_src(SourceType.HISTORY_EVENT),))
    with pytest.raises(MemoryDomainError):
        _candidate(verified=True, sources=())
    with pytest.raises(MemoryDomainError):
        # 偏好 + 工具证据同样不允许自称已验证。
        _candidate(
            memory_type=MemoryType.PREFERENCE,
            subject_key=PREF_SUBJECT,
            verified=True,
            sources=(_src(SourceType.TOOL_RESULT),),
        )
    # 有合格来源时放行。
    assert _candidate(verified=True).verified is True


def test_confidence_ceiling_follows_source_grade() -> None:
    assert confidence_ceiling([_src(SourceType.USER_STATEMENT)]) == 1.0
    assert confidence_ceiling([_src(SourceType.TOOL_RESULT)]) == 0.95
    assert confidence_ceiling([_src(SourceType.HISTORY_EVENT)]) == 0.6
    # 多条来源取最高等级。
    assert (
        confidence_ceiling(
            [_src(SourceType.HISTORY_EVENT), _src(SourceType.TOOL_RESULT)]
        )
        == 0.95
    )
    assert confidence_ceiling([]) == 0.5


def test_importance_is_mapped_from_enum_not_float() -> None:
    assert importance_for_level("low") == 0.3
    assert importance_for_level("high") == 0.9
    with pytest.raises(MemoryDomainError):
        importance_for_level("very-high")


# --------------------------------------------------------------------------- #
# 领域规则：状态机与有效期
# --------------------------------------------------------------------------- #


def test_status_machine_only_allows_leaving_active() -> None:
    assert allowed_status_transitions("active") == {
        MemoryStatus.SUPERSEDED,
        MemoryStatus.EXPIRED,
        MemoryStatus.DELETED,
    }
    for terminal in ("superseded", "expired", "deleted"):
        assert allowed_status_transitions(terminal) == frozenset()

    ensure_status_transition("active", "superseded")
    # 同一个状态之间的"迁移"是幂等的，不算错误。
    ensure_status_transition("superseded", "superseded")

    for terminal in ("superseded", "expired", "deleted"):
        with pytest.raises(InvalidStatusTransitionError):
            ensure_status_transition(terminal, "active")
    # 终态之间也不能互相跳（expired 与 expired 相同，属于幂等，不算迁移）。
    with pytest.raises(InvalidStatusTransitionError):
        ensure_status_transition("expired", "deleted")

    with pytest.raises(MemoryDomainError):
        ensure_status_transition("nonsense", "active")


def test_validity_window_must_be_half_open_and_ordered() -> None:
    start = _now()
    validate_validity_window(start, None)
    validate_validity_window(start, start + timedelta(seconds=1))
    with pytest.raises(MemoryDomainError):
        validate_validity_window(start, start)
    with pytest.raises(MemoryDomainError):
        validate_validity_window(start, start - timedelta(seconds=1))


def test_is_valid_at_matches_as_of_semantics() -> None:
    start = _now()
    closed = _version(valid_from=start, valid_to=start + timedelta(hours=1))
    assert is_valid_at(closed, start + timedelta(minutes=30))
    assert not is_valid_at(closed, start - timedelta(minutes=1))
    # 半开区间：右端点不算有效。
    assert not is_valid_at(closed, start + timedelta(hours=1))

    open_ended = _version(valid_from=start)
    assert is_valid_at(open_ended, start + timedelta(days=30))


# --------------------------------------------------------------------------- #
# 领域规则：内容判重
# --------------------------------------------------------------------------- #


def test_same_content_ignores_whitespace_but_not_meaning() -> None:
    assert same_content("默认 shell 是 bash", " 默认 shell  是 bash ")
    assert same_content("a\r\nb", "a\nb")
    assert not same_content("默认 shell 是 bash", "默认 shell 是 zsh")


def test_additional_source_keys_detects_new_evidence() -> None:
    first = _src(sid="rows-1")
    again = MemorySource(
        source_type=SourceType.USER_STATEMENT,
        source_id="rows-1",
        observed_at=_now(),
    )
    fresh = _src(sid="rows-2")
    assert additional_source_keys([first], [again]) == frozenset()
    assert additional_source_keys([first], [first, fresh]) == {
        (SourceType.USER_STATEMENT, "rows-2")
    }


# --------------------------------------------------------------------------- #
# 决策引擎（纯函数）
# --------------------------------------------------------------------------- #


def test_no_source_candidate_is_rejected() -> None:
    plan = plan_consolidation(_candidate(sources=()), None)
    assert plan.decision is WriteDecision.REJECT
    assert "来源" in plan.reason


def test_explicit_reject_is_honored_even_for_new_subject() -> None:
    plan = plan_consolidation(_candidate(proposed=WriteDecision.REJECT), None)
    assert plan.decision is WriteDecision.REJECT


def test_new_subject_becomes_add() -> None:
    plan = plan_consolidation(_candidate(), None)
    assert plan.decision is WriteDecision.ADD
    assert plan.target_memory_id is None
    assert plan.writes is True


def test_superseded_record_is_treated_as_no_current_value() -> None:
    # 已失效的记录不再是"当前有效版本"，同一 subject 可以重新成立。
    retired = _record(status=MemoryStatus.SUPERSEDED)
    plan = plan_consolidation(_candidate(), retired)
    assert plan.decision is WriteDecision.ADD


def test_identical_content_without_new_evidence_is_noop() -> None:
    known = _src(sid="rows-1")
    active = _record(active_version=_version(content=PY311, sources=(known,)))
    plan = plan_consolidation(_candidate(content=PY311, sources=(known,)), active)
    assert plan.decision is WriteDecision.NOOP
    assert plan.target_memory_id == active.id
    assert plan.writes is False


def test_identical_content_with_new_source_becomes_update() -> None:
    known = _src(sid="rows-1")
    active = _record(
        active_version=_version(content=PY311, sources=(known,)),
        confidence=0.6,
    )
    candidate = _candidate(content=PY311, sources=(known, _src(sid="rows-2")))
    plan = plan_consolidation(candidate, active)
    assert plan.decision is WriteDecision.UPDATE
    assert "新来源" in plan.reason


def test_identical_content_with_higher_confidence_becomes_update() -> None:
    known = _src(sid="rows-1")
    active = _record(
        active_version=_version(content=PY311, sources=(known,)), confidence=0.5
    )
    plan = plan_consolidation(
        _candidate(content=PY311, sources=(known,), confidence=0.9), active
    )
    assert plan.decision is WriteDecision.UPDATE
    assert "置信度" in plan.reason


def test_changed_content_without_proposal_is_rejected() -> None:
    active = _record(active_version=_version(content=PY38))
    plan = plan_consolidation(_candidate(), active)
    assert plan.decision is WriteDecision.REJECT
    assert "静默覆盖" in plan.reason


def test_episode_never_supersedes_and_falls_back_to_update() -> None:
    active = _record(
        subject_key=EPISODE_SUBJECT,
        memory_type=MemoryType.EPISODE,
        scope=Scope.PROJECT,
        active_version=_version(content="第一次部署失败：端口被占用"),
    )
    plan = plan_consolidation(
        _candidate(
            memory_type=MemoryType.EPISODE,
            subject_key=EPISODE_SUBJECT,
            content="补充：端口被占用是因为上一条规则没清理",
            proposed=WriteDecision.SUPERSEDE,
        ),
        active,
    )
    assert plan.decision is WriteDecision.UPDATE


def test_proposed_update_keeps_same_record() -> None:
    active = _record(active_version=_version(content=PY38))
    plan = plan_consolidation(
        _candidate(proposed=WriteDecision.UPDATE, content="本项目运行时要求 Python 3.8（补充说明）"),
        active,
    )
    assert plan.decision is WriteDecision.UPDATE
    assert plan.target_memory_id == active.id


def test_add_proposal_with_existing_active_is_rejected() -> None:
    active = _record(active_version=_version(content=PY38))
    plan = plan_consolidation(
        _candidate(proposed=WriteDecision.ADD, content=PY311), active
    )
    assert plan.decision is WriteDecision.REJECT


def test_supersede_allowed_when_new_value_is_evidenced() -> None:
    active = _record(
        active_version=_version(content=PY38, sources=(_src(SourceType.TOOL_RESULT),))
    )
    plan = plan_consolidation(
        _candidate(content=PY311, proposed=WriteDecision.SUPERSEDE), active
    )
    assert plan.decision is WriteDecision.SUPERSEDE
    assert plan.target_memory_id == active.id


def test_supersede_rejected_when_old_value_is_verified_and_new_is_not() -> None:
    """旧值有证据、新值只有对话记录：保留矛盾而不是替代（§6.5 规则 4/5）。"""

    active = _record(
        active_version=_version(
            content=PY38,
            verified=True,
            sources=(_src(SourceType.TOOL_RESULT),),
        )
    )
    plan = plan_consolidation(
        _candidate(
            content=PY311,
            proposed=WriteDecision.SUPERSEDE,
            sources=(_src(SourceType.HISTORY_EVENT),),
        ),
        active,
    )
    assert plan.decision is WriteDecision.REJECT
    assert plan.suggested_relation is RelationType.CONTRADICTS


def test_preference_supersede_requires_user_statement() -> None:
    active = _record(
        subject_key=PREF_SUBJECT,
        memory_type=MemoryType.PREFERENCE,
        active_version=_version(content="回答保持简短", sources=(_src(),)),
    )
    plan = plan_consolidation(
        _candidate(
            memory_type=MemoryType.PREFERENCE,
            subject_key=PREF_SUBJECT,
            content="回答要详细一些",
            proposed=WriteDecision.SUPERSEDE,
            sources=(_src(SourceType.TOOL_RESULT),),
        ),
        active,
    )
    assert plan.decision is WriteDecision.REJECT
    assert plan.suggested_relation is RelationType.CONTRADICTS

    # 用户明确改口时允许替代。
    plan = plan_consolidation(
        _candidate(
            memory_type=MemoryType.PREFERENCE,
            subject_key=PREF_SUBJECT,
            content="回答要详细一些",
            proposed=WriteDecision.SUPERSEDE,
            sources=(_src(SourceType.USER_STATEMENT),),
        ),
        active,
    )
    assert plan.decision is WriteDecision.SUPERSEDE


def test_plan_reason_is_always_present() -> None:
    for plan in (
        plan_consolidation(_candidate(), None),
        plan_consolidation(_candidate(sources=()), None),
    ):
        assert isinstance(plan, ConsolidationPlan)
        assert plan.reason.strip()


# --------------------------------------------------------------------------- #
# 真库夹具
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过版本管理集成测试"
            "（「历史正文不可修改」「同 subject 原子替代」必须在真库上证明）"
        )
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过版本管理集成测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:
        pytest.fail(f"{TEST_DSN_ENV} 已设置但连不上：{exc}")
    return dsn


@pytest.fixture(scope="module")
def db(admin_dsn: str):
    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, min_size=1, max_size=4)
        database.open()
        try:
            assert database.run_migrations() == [
                "0001_init",
                "0002_roles_and_grants",
                "0003_immutable_versions",
                "0004_governance",
                "0005_governance_provenance",
                "0006_memory_maintenance",
        "0007_memory_maintenance_proposals",
            ]
            yield database
        finally:
            database.close()


def _repo(database: MemoryDatabase, tenant: Tenant = ALICE) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _new_record(
    *,
    subject_key: str = FACT_SUBJECT,
    content: str = PY38,
    memory_type: MemoryType = MemoryType.FACT,
    scope: Scope = Scope.PROJECT,
    project_id: str | None = "demo",
    sources: tuple[MemorySource, ...] | None = None,
    verified: bool = False,
    confidence: float = 0.6,
    valid_from: datetime | None = None,
) -> NewRecordCommand:
    return NewRecordCommand(
        user_id=ALICE.user_id,
        scope=scope,
        memory_type=memory_type,
        subject_key=subject_key,
        content=content,
        project_id=project_id,
        sources=sources if sources is not None else (_src(),),
        verified=verified,
        confidence=confidence,
        valid_from=valid_from,
    )


# --------------------------------------------------------------------------- #
# 真库：版本不可变
# --------------------------------------------------------------------------- #


def test_schema_declares_immutability_trigger_and_verified_column(db) -> None:
    with db.diagnostic_session() as conn:
        column = conn.execute(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'memory_versions' AND column_name = 'verified'"
        ).fetchone()
        assert column is not None
        assert column["data_type"] == "boolean"
        assert column["is_nullable"] == "NO"

        trigger = conn.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'memory_versions'::regclass AND NOT tgisinternal"
        ).fetchall()
        assert [row["tgname"] for row in trigger] == ["memory_versions_immutable"]


def test_direct_update_of_version_content_is_blocked_by_database(db) -> None:
    """最核心的一条：库里没有任何路径可以改写既有版本的正文。"""

    record = _repo(db).add_record(_new_record())
    version_id = UUID(record.active_version_id)

    statements = (
        "UPDATE memory_versions SET content = '被改写' WHERE id = %s",
        "UPDATE memory_versions SET content_hash = 'deadbeef' WHERE id = %s",
        "UPDATE memory_versions SET lexical_text = 'tampered' WHERE id = %s",
        "UPDATE memory_versions SET observed_at = now() WHERE id = %s",
        "UPDATE memory_versions SET valid_from = valid_from - interval '1 day' WHERE id = %s",
        "UPDATE memory_versions SET version = 99 WHERE id = %s",
    )
    # 每条语句单独开一个事务：PostgreSQL 事务一旦报错就整体 aborted，
    # 后续语句会抛 25P02（in_failed_sql_transaction）而不是触发器里的 2F002，
    # 那样就测不到"触发器到底拦没拦"。
    for statement in statements:
        with db.tenant_session(ALICE) as conn:
            with pytest.raises(Exception) as excinfo:
                conn.execute(statement, (version_id,))
        # 2F002 = modifying_sql_data_not_permitted
        assert getattr(excinfo.value, "sqlstate", None) == "2F002", statement

    stored = _repo(db).get_version(record.active_version_id)
    assert stored is not None and stored.content == PY38


def test_valid_to_and_embedding_are_still_mutable(db) -> None:
    """不可变的是正文；有效期与向量必须能改，否则替代和异步 embedding 都没法做。"""

    repo = _repo(db)
    record = repo.add_record(_new_record(subject_key="fact:project:demo:mutability"))
    version_id = record.active_version_id

    repo.set_embedding(
        version_id,
        tuple([1.0] + [0.0] * 1023),
        embedding_model="text-embedding-v4",
        embedding_version="1",
    )
    repo.mark_expired(record.id, _now() + timedelta(hours=1))

    stored = repo.get_version(version_id)
    assert stored is not None
    assert stored.embedding_model == "text-embedding-v4"
    assert stored.valid_to is not None


def test_verified_flag_is_one_way(db) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _new_record(subject_key="fact:project:demo:verify_flag")
    )
    version_id = UUID(record.active_version_id)

    with db.tenant_session(ALICE) as conn:
        conn.execute(
            "UPDATE memory_versions SET verified = true WHERE id = %s", (version_id,)
        )
    assert repo.get_version(str(version_id)).verified is True

    with db.tenant_session(ALICE) as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                "UPDATE memory_versions SET verified = false WHERE id = %s",
                (version_id,),
            )
        assert getattr(excinfo.value, "sqlstate", None) == "2F002"


# --------------------------------------------------------------------------- #
# 真库：同 subject 原子替代
# --------------------------------------------------------------------------- #


def test_replace_active_swaps_same_subject_atomically(db) -> None:
    repo = _repo(db)
    old = repo.add_record(_new_record(subject_key="fact:project:demo:runtime", content=PY38))

    new = repo.replace_active(
        old.id,
        _new_record(subject_key="fact:project:demo:runtime", content=PY311),
        reason="项目升级到 3.11",
    )

    # 新记忆接管了同一个 subject。
    assert new.subject_key == old.subject_key
    assert new.id != old.id
    assert new.status is MemoryStatus.ACTIVE
    assert new.active_version is not None and new.active_version.content == PY311

    # 旧记忆变成 superseded，且它的当前版本结束了有效期。
    retired = repo.get_record(old.id)
    assert retired is not None and retired.status is MemoryStatus.SUPERSEDED
    assert repo.get_active(old.id) is None
    old_versions = repo.list_versions(old.id)
    assert old_versions[0].valid_to is not None

    # 关系与替代同事务落库。
    active = repo.find_active_by_subject("fact:project:demo:runtime")
    assert active is not None and active.id == new.id
    with db.diagnostic_session() as conn:
        relation = conn.execute(
            "SELECT relation_type, reason FROM memory_relations "
            "WHERE from_memory_id = %s AND to_memory_id = %s",
            (UUID(new.id), UUID(old.id)),
        ).fetchone()
    assert relation == {
        "relation_type": "supersedes",
        "reason": "项目升级到 3.11",
    }


def test_replace_active_requires_same_subject_and_active_target(db) -> None:
    repo = _repo(db)
    old = repo.add_record(_new_record(subject_key="fact:project:demo:same_subject"))
    other_subject = _new_record(
        subject_key="fact:project:demo:other_subject", content="另一个主题"
    )

    with pytest.raises(MemoryDomainError):
        repo.replace_active(old.id, other_subject)

    # 被替代过一次之后不能再被替代。
    repo.replace_active(
        old.id,
        _new_record(subject_key="fact:project:demo:same_subject", content=PY311),
    )
    with pytest.raises(RecordNotActiveError):
        repo.replace_active(
            old.id,
            _new_record(subject_key="fact:project:demo:same_subject", content="再来一次"),
        )


def test_replace_active_refuses_episodes(db) -> None:
    repo = _repo(db)
    episode = repo.add_record(
        _new_record(
            memory_type=MemoryType.EPISODE,
            subject_key=EPISODE_SUBJECT,
            content="第一次部署失败：端口被占用",
        )
    )
    with pytest.raises(MemoryDomainError):
        repo.replace_active(
            episode.id,
            _new_record(
                memory_type=MemoryType.EPISODE,
                subject_key=EPISODE_SUBJECT,
                content="换个说法",
            ),
        )


def test_failed_replace_leaves_no_partial_state(db) -> None:
    """失败路径不能留下"旧记录已失效、新记录没写"的中间态。"""

    repo = _repo(db)
    subject = "fact:project:demo:partial"
    old = repo.add_record(_new_record(subject_key=subject))
    before_versions = len(repo.list_versions(old.id))

    with pytest.raises(MemoryDomainError):
        repo.replace_active(
            old.id,
            _new_record(subject_key="fact:project:demo:different", content="不匹配"),
        )

    after = repo.get_record(old.id)
    assert after is not None and after.status is MemoryStatus.ACTIVE
    assert len(repo.list_versions(old.id)) == before_versions
    # 没有因为失败而多出一条 active 记忆。
    with db.diagnostic_session() as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM memory_records WHERE user_id = 'alice' "
            "AND subject_key = 'fact:project:demo:different'"
        ).fetchone()["n"]
    assert count == 0


# --------------------------------------------------------------------------- #
# 真库：有效期与状态机
# --------------------------------------------------------------------------- #


def test_add_version_closes_previous_validity_window(db) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _new_record(subject_key="fact:project:demo:window", content=PY38)
    )
    first = repo.list_versions(record.id)[0]
    assert first.valid_to is None

    repo.add_version(
        NewVersionCommand(memory_id=record.id, content=PY311, confidence=0.9)
    )

    versions = repo.list_versions(record.id)
    assert [v.version for v in versions] == [1, 2]
    # 旧版本被封口，新版本未失效：as_of 才不会对同一 subject 返回两条。
    assert versions[0].valid_to is not None
    assert versions[1].valid_to is None
    # 两个时间窗不重叠。允许 1 微秒的封口补偿：写入时用
    # GREATEST(new_valid_from, old_valid_from + 1µs) 保证 valid_to > valid_from。
    assert versions[0].valid_to <= versions[1].valid_from + timedelta(microseconds=1)


def test_confidence_only_rises_on_new_version(db) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _new_record(subject_key="fact:project:demo:conf", confidence=0.5)
    )
    repo.add_version(
        NewVersionCommand(memory_id=record.id, content=PY311, confidence=0.9)
    )
    assert repo.get_record(record.id).confidence == pytest.approx(0.9)

    # 后续一次描述改写带着更低的置信度，不允许把它拉回去。
    repo.add_version(
        NewVersionCommand(
            memory_id=record.id, content=PY311 + "（措辞调整）", confidence=0.4
        )
    )
    assert repo.get_record(record.id).confidence == pytest.approx(0.9)


def test_attach_sources_raises_confidence_and_flips_verified(db) -> None:
    """「只补充来源」的 UPDATE：不新增版本，但把证据和置信度补上。"""

    repo = _repo(db)
    subject = "fact:project:demo:attach"
    # 起始版本只由对话事件支撑：既不能算已验证，置信度也被压在低位。
    record = repo.add_record(
        _new_record(
            subject_key=subject,
            sources=(_src(SourceType.HISTORY_EVENT, sid="rows-1"),),
            confidence=0.5,
        )
    )
    version_id = record.active_version_id
    assert repo.get_version(version_id).verified is False

    refreshed = repo.attach_sources(
        record.id,
        (_src(SourceType.TOOL_RESULT, sid="tool-9"),),
        confidence=0.9,
        verified=True,
    )

    assert len(repo.list_versions(record.id)) == 1
    assert len(repo.list_sources(version_id)) == 2
    assert repo.get_record(record.id).confidence == pytest.approx(0.9)
    assert refreshed.verified is True

    # 幂等：同一来源重复挂载不产生重复行，置信度也不会被拉低。
    repo.attach_sources(record.id, (_src(SourceType.TOOL_RESULT, sid="tool-9"),), confidence=0.2)
    assert len(repo.list_sources(version_id)) == 2
    assert repo.get_record(record.id).confidence == pytest.approx(0.9)


def test_attach_sources_rejects_self_declared_verification(db) -> None:
    """补充来源时同样不允许"自封已验证"：来源等级不够就必须报错。"""

    repo = _repo(db)
    subject = "fact:project:demo:attach_guard"
    record = repo.add_record(
        _new_record(
            subject_key=subject,
            sources=(_src(SourceType.HISTORY_EVENT, sid="rows-2"),),
        )
    )
    with pytest.raises(MemoryDomainError):
        repo.attach_sources(
            record.id,
            (_src(SourceType.HISTORY_EVENT, sid="rows-3"),),
            verified=True,
        )


def test_attach_sources_rejects_non_active_record(db) -> None:
    repo = _repo(db)
    subject = "fact:project:demo:attach_retired"
    old = repo.add_record(_new_record(subject_key=subject, content=PY38))
    repo.replace_active(old.id, _new_record(subject_key=subject, content=PY311))

    with pytest.raises(RecordNotActiveError):
        repo.attach_sources(old.id, (_src(sid="rows-4"),))


def test_as_of_returns_the_value_that_was_valid_then(db) -> None:
    """时态查询：替代发生之后，仍然能问出"当时是什么"。"""

    repo = _repo(db)
    subject = "fact:project:demo:as_of"
    old = repo.add_record(_new_record(subject_key=subject, content=PY38))
    old_version = repo.list_versions(old.id)[0]

    switch = _now() + timedelta(milliseconds=1)
    new = repo.replace_active(
        old.id,
        _new_record(subject_key=subject, content=PY311, valid_from=switch),
    )

    # 注意：本文件的 `db` 夹具是 **module scope**，同一个库里还有本文件其它用例
    # 造的数据。这些记录的 lexical_text 往往与本文相同（都是 "python 项目 运行 要求"），
    # ts_rank_cd 分数完全相同，而 `ORDER BY ts_rank_cd DESC, v.id` 在同分组里
    # 退化成**随机 UUID 顺序**——limit 给小了，本条会被挤到候选之外，断言随机失败
    # （实测 8 次里偶发 1 次）。所以这里显式给一个大 limit，让"当前视图 / as_of
    # 视图的语义"成为唯一被检验的东西。
    wide_limit = 200

    # 切换之前：这个 subject 只能看到 3.8。
    # 断言里按 subject_key 过滤：库里还有同一轮其它用例造的记录，
    # 直接比对整个结果列表会在加用例时误伤。
    before = repo.search(
        MemoryQuery(
            text="Python 3.8",
            limit=wide_limit,
            as_of=old_version.valid_from,
        )
    )
    assert [
        hit.version.content for hit in before if hit.record.subject_key == subject
    ] == [PY38]

    # 切换之后：只能看到 3.11。
    after = repo.search(
        MemoryQuery(
            text="Python 3.11",
            limit=wide_limit,
            as_of=switch + timedelta(seconds=1),
        )
    )
    assert [
        hit.version.content for hit in after if hit.record.subject_key == subject
    ] == [PY311]

    # 当前视图只看新值。
    current = repo.search(MemoryQuery(text="Python", limit=wide_limit))
    assert [hit.record.id for hit in current if hit.record.subject_key == subject] == [
        new.id
    ]
    # 再给一条不依赖排序的等价断言：按 subject 找 active 记忆就该是新记录。
    active = repo.find_active_by_subject(subject)
    assert active is not None and active.id == new.id


def test_mark_expired_rejects_non_active_and_bad_window(db) -> None:
    repo = _repo(db)
    subject = "fact:project:demo:expire_guard"
    old = repo.add_record(_new_record(subject_key=subject, content=PY38))
    repo.replace_active(
        old.id, _new_record(subject_key=subject, content=PY311)
    )

    # 已被替代的记忆不能再被标记过期——否则会去改一个已封存版本的时间窗。
    with pytest.raises(RecordNotActiveError):
        repo.mark_expired(old.id, _now() + timedelta(hours=1))

    alive = repo.add_record(_new_record(subject_key="fact:project:demo:expire_ok"))
    with pytest.raises(MemoryDomainError):
        repo.mark_expired(alive.id, _now() - timedelta(days=1))


def test_episodes_are_not_folded_by_search_after_multiple_versions(db) -> None:
    """情景记忆追加版本后，当前视图只返回最新描述，不会同时命中两版。"""

    repo = _repo(db)
    subject = "episode:evt-multi"
    unique = "Klonet 部署流水线第二步卡在依赖安装"
    episode = repo.add_record(
        _new_record(
            memory_type=MemoryType.EPISODE,
            subject_key=subject,
            content=unique,
        )
    )
    repo.add_version(
        NewVersionCommand(
            memory_id=episode.id, content=unique + "，根因是镜像里缺 libpq"
        )
    )
    # limit 同样给宽：理由与 test_as_of 那处相同（module scope 共享库 +
    # 同分排序退化成随机 UUID 顺序）。
    hits = [
        hit
        for hit in repo.search(MemoryQuery(text="依赖安装", limit=200))
        if hit.record.subject_key == subject
    ]
    assert len(hits) == 1
    assert hits[0].version.version == 2
    # 旧版本的正文不再出现在当前视图里。
    assert hits[0].version.content != unique


# --------------------------------------------------------------------------- #
# 真库：决策引擎端到端
# --------------------------------------------------------------------------- #


def _consolidate(repo, candidate: MemoryCandidate, *, candidate_id: str | None = None):
    existing = repo.find_active_by_subject(candidate.subject_key)
    sources = (
        repo.list_sources(existing.active_version_id)
        if existing is not None and existing.active_version_id
        else None
    )
    plan = plan_consolidation(candidate, existing, existing_sources=sources)
    return apply_plan(repo, plan, candidate, candidate_id=candidate_id)


def test_consolidation_add_then_noop_then_supersede(db) -> None:
    repo = _repo(db)
    subject = "fact:project:demo:engine"
    known = _src(sid="rows-100")

    def make(
        content: str,
        *,
        sources: tuple[MemorySource, ...] | None = None,
        proposed: WriteDecision | None = None,
        verified: bool = False,
    ) -> MemoryCandidate:
        return MemoryCandidate(
            memory_type=MemoryType.FACT,
            scope=Scope.PROJECT,
            subject_key=subject,
            content=content,
            user_id=ALICE.user_id,
            project_id="demo",
            sources=(known,) if sources is None else sources,
            proposed_decision=proposed,
            verified=verified,
            confidence=0.6,
        )

    # ① 第一次：ADD
    outcome = _consolidate(repo, make(PY38))
    assert outcome.decision is WriteDecision.ADD
    first_id = outcome.record.id

    # ② 同一句话 + 同一来源再来一次：NOOP，不产生第二条记忆
    outcome = _consolidate(repo, make(PY38))
    assert outcome.decision is WriteDecision.NOOP
    assert outcome.record is None
    assert len(repo.list_versions(first_id)) == 1

    # ③ 同一句话但带来新来源：UPDATE，走"补充来源"而不是追加版本
    #    （版本表上 (memory_id, content_hash) 唯一，同内容不允许出现两个版本）。
    outcome = _consolidate(
        repo, make(PY38, sources=(known, _src(sid="rows-101")))
    )
    assert outcome.decision is WriteDecision.UPDATE
    assert outcome.record.id == first_id
    assert len(repo.list_versions(first_id)) == 1
    assert len(repo.list_sources(outcome.record.active_version_id)) == 2

    # ④ 值变了且新值有证据：SUPERSEDE
    outcome = _consolidate(
        repo, make(PY311, proposed=WriteDecision.SUPERSEDE, verified=True)
    )
    assert outcome.decision is WriteDecision.SUPERSEDE
    assert outcome.record.id != first_id
    assert repo.get_record(first_id).status is MemoryStatus.SUPERSEDED


def test_consolidation_update_appends_version_to_same_record(db) -> None:
    repo = _repo(db)
    subject = "fact:project:demo:engine_update"
    command = _new_record(subject_key=subject, content=PY38)
    record = repo.add_record(command)

    candidate = MemoryCandidate(
        memory_type=MemoryType.FACT,
        scope=Scope.PROJECT,
        subject_key=subject,
        content=PY38 + "（补记：见 config.py）",
        user_id=ALICE.user_id,
        project_id="demo",
        sources=command.sources,
        proposed_decision=WriteDecision.UPDATE,
    )
    outcome = _consolidate(repo, candidate)

    assert outcome.decision is WriteDecision.UPDATE
    assert outcome.record.id == record.id
    assert [v.version for v in repo.list_versions(record.id)] == [1, 2]


def test_consolidation_records_candidate_decision(db) -> None:
    repo = _repo(db)
    candidate = MemoryCandidate(
        memory_type=MemoryType.FACT,
        scope=Scope.PROJECT,
        subject_key="fact:project:demo:engine_audit",
        content=PY38,
        user_id=ALICE.user_id,
        project_id="demo",
        sources=(_src(),),
    )
    candidate_id = repo.add_candidate(
        candidate, idempotency_key="evt-9", source_event_range={"start": "rows-1"}
    )

    outcome = _consolidate(repo, candidate, candidate_id=candidate_id)
    assert outcome.decision is WriteDecision.ADD

    decided = repo.list_candidates(decision=WriteDecision.ADD)
    audit = [item for item in decided if item.id == candidate_id]
    assert len(audit) == 1
    assert audit[0].decision_reason
    assert repo.list_candidates() == []


def test_consolidation_reject_is_audited_not_silently_dropped(db) -> None:
    repo = _repo(db)
    candidate = MemoryCandidate(
        memory_type=MemoryType.FACT,
        scope=Scope.PROJECT,
        subject_key="fact:project:demo:engine_reject",
        content="没有任何来源的说法",
        user_id=ALICE.user_id,
        project_id="demo",
    )
    candidate_id = repo.add_candidate(
        candidate, idempotency_key="evt-10", source_event_range={"start": "rows-3"}
    )

    outcome = _consolidate(repo, candidate, candidate_id=candidate_id)
    assert outcome.decision is WriteDecision.REJECT
    assert outcome.record is None

    rejected = [item for item in repo.list_candidates(decision=WriteDecision.REJECT)]
    assert [item.id for item in rejected] == [candidate_id]
    assert repo.find_active_by_subject(candidate.subject_key) is None
