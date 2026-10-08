"""记忆库 PostgreSQL 后端的集成测试。

**没有真库时整个模块 skip**，这不是"跳过就算过"：阶段 1 的完成标准是
"全新数据库可由 migration 一次创建；重复 migration 无副作用；跨用户查询在数据库
层被拒绝"，这三条离开真库无法证明。

跑法（本机 Docker 验证）：

    ./scripts/pg_local.sh up
    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_postgres_repository.py -q

只认 ``KLONET_AGENT_TEST_PG_DSN``：本模块会**创建并删除数据库**，绝不能指向生产库，
所以刻意不复用业务用的 ``KLONET_AGENT_MEMORY_DSN``。

RLS 相关的断言需要能 ``SET LOCAL ROLE klonet_app`` / ``klonet_ops``（即 DSN 账号是
超级用户或这两个角色的成员）；做不到时这几条会带着原因 skip，而不是假装通过。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copytree
from typing import Any
from uuid import UUID, uuid4

import pytest

from klonet_agent.memory.database import (
    MemoryDatabase,
    MigrationError,
    temporary_database,
)
from klonet_agent.memory.domain import (
    MemoryCandidate,
    MemoryQuery,
    MemorySource,
    MemoryStatus,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
    WriteDecision,
)
from klonet_agent.memory.postgres import (
    EMBEDDING_DIMENSIONS,
    PostgresMemoryRepository,
)
from klonet_agent.memory.repository import (
    ActiveSubjectConflictError,
    CandidateNotFoundError,
    DuplicateVersionError,
    NewRecordCommand,
    NewVersionCommand,
    RecordNotFoundError,
    RecordNotActiveError,
    ScopeViolationError,
)

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"
APP_ROLE = "klonet_app"
OPS_ROLE = "klonet_ops"

ALICE = Tenant(user_id="alice", project_id="demo")
BOB = Tenant(user_id="bob", project_id="demo")
NOBODY = Tenant(user_id="nobody-at-all", project_id="demo")
SHARED = Tenant(user_id="shared", project_id=None)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过记忆库集成测试"
            "（阶段 1 的验收标准需要真库，见本文件头部说明）"
        )
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过记忆库集成测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:
        # 设置了这个变量却连不上，是配置错误，不能当"没库"跳过。
        pytest.fail(f"{TEST_DSN_ENV} 已设置但连不上：{exc}")
    return dsn


@pytest.fixture(scope="module")
def db(admin_dsn: str) -> Any:
    """在一个一次性数据库上跑完迁移并交出已打开的 MemoryDatabase。"""

    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, min_size=1, max_size=4)
        database.open()
        try:
            assert database.run_migrations() == [
                "0001_init",
                "0002_roles_and_grants",
                "0003_immutable_versions",
            ]
            yield database
        finally:
            database.close()


def _role_available(database: MemoryDatabase, role: str) -> bool:
    with database.diagnostic_session() as conn:
        exists = conn.execute(
            "SELECT 1 AS ok FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if not exists:
            return False
        row = conn.execute(
            """
            SELECT pg_has_role(current_user, %s, 'member') AS member,
                   (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super
            """,
            (role,),
        ).fetchone()
    return bool(row and (row["member"] or row["is_super"]))


@pytest.fixture(scope="module")
def app_role(db: MemoryDatabase) -> str:
    if not _role_available(db, APP_ROLE):
        pytest.skip(
            f"{APP_ROLE} 不存在或当前 DSN 账号无法切换过去"
            "（0002 需要 CREATEROLE 权限；RLS 断言无法在此环境成立）"
        )
    return APP_ROLE


@pytest.fixture(scope="module")
def ops_role(db: MemoryDatabase) -> str:
    if not _role_available(db, OPS_ROLE):
        pytest.skip(f"{OPS_ROLE} 不存在或当前 DSN 账号无法切换过去")
    return OPS_ROLE


@contextmanager
def _as_role(database: MemoryDatabase, tenant: Tenant, role: str):
    """在已绑定租户的事务里切到指定角色。

    ``SET LOCAL ROLE`` 是事务级的，事务结束自动 RESET，不会把角色残留在池化连接上。
    切过去之后 current_user 不再是超级用户，RLS 才真正生效。
    """

    from psycopg import sql

    with database.tenant_session(tenant) as conn:
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        yield conn


def _repo(database: MemoryDatabase, tenant: Tenant = ALICE) -> PostgresMemoryRepository:
    return PostgresMemoryRepository(database, tenant)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unit_vector(index: int) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[index] = 1.0
    return tuple(values)


def _fact(
    *,
    tenant: Tenant = ALICE,
    subject_key: str = "fact:project:demo:python_version",
    content: str = "后端项目使用 Python 3.11 运行",
    sources: tuple[MemorySource, ...] | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
    scope: Scope = Scope.PROJECT,
) -> NewRecordCommand:
    if sources is None:
        sources = (
            MemorySource(
                source_type=SourceType.USER_STATEMENT,
                source_id=f"rows-{uuid4().hex[:8]}",
                observed_at=_now(),
                source_excerpt="用户在对话里明确说明",
            ),
        )
    return NewRecordCommand(
        user_id=user_id if user_id is not None else tenant.user_id,
        scope=scope,
        memory_type=MemoryType.FACT,
        subject_key=subject_key,
        content=content,
        project_id=project_id if project_id is not None else tenant.project_id,
        sources=sources,
    )


# --------------------------------------------------------------------------- #
# 迁移
# --------------------------------------------------------------------------- #


def test_migration_creates_extension_tables_indexes_and_policies(db: MemoryDatabase) -> None:
    info = db.assert_ready()
    assert info["pgvector_version"]
    assert info["applied_migrations"] == [
        "0001_init",
        "0002_roles_and_grants",
        "0003_immutable_versions",
    ]
    # 报告里带回来的 DSN 必须已脱敏。注意这里要同时检查 URL 形式和 libpq 关键字
    # 形式——`temporary_database` 用 make_conninfo() 拼出来的正是关键字形式。
    assert "password=klonet" not in info["dsn"]
    assert ":klonet@" not in info["dsn"]

    with db.diagnostic_session() as conn:
        tables = {
            row["relname"]
            for row in conn.execute(
                "SELECT relname FROM pg_class WHERE relkind = 'r' "
                "AND relname LIKE 'memory\\_%'"
            ).fetchall()
        }
        assert tables == {
            "memory_records",
            "memory_versions",
            "memory_sources",
            "memory_relations",
            "memory_write_candidates",
            "memory_embedding_outbox",
        }

        indexes = {
            row["indexname"]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            ).fetchall()
        }
        for expected in (
            "idx_memory_records_scope",
            "uq_memory_records_active_subject",
            "idx_memory_versions_lexical",
            "idx_memory_versions_validity",
        ):
            assert expected in indexes, expected

        policies = {
            row["policyname"]
            for row in conn.execute(
                "SELECT policyname FROM pg_policies WHERE schemaname = 'public'"
            ).fetchall()
        }
        assert "memory_records_tenant" in policies
        assert "memory_versions_tenant" in policies

        # 六张表都必须开启并强制 RLS。
        forced = {
            row["relname"]
            for row in conn.execute(
                "SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity"
            ).fetchall()
        }
        assert {
            "memory_records",
            "memory_versions",
            "memory_sources",
            "memory_relations",
            "memory_write_candidates",
            "memory_embedding_outbox",
        } <= forced


def test_rerunning_migrations_is_a_noop(db: MemoryDatabase) -> None:
    before = db.applied_migrations()
    assert db.run_migrations() == []
    assert db.applied_migrations() == before


def test_tampered_migration_file_is_rejected(admin_dsn: str, tmp_path: Path) -> None:
    """已执行的迁移不允许被改动；改动必须报错，而不是静默重放。"""

    source = Path(__file__).resolve().parent.parent / "migrations"
    target = tmp_path / "migrations"
    copytree(source, target)

    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, migrations_dir=target)
        database.open()
        try:
            database.run_migrations()
            victim = target / "0001_init.sql"
            victim.write_text(
                victim.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8"
            )
            with pytest.raises(MigrationError):
                database.run_migrations()
        finally:
            database.close()


# --------------------------------------------------------------------------- #
# 写入 / 版本
# --------------------------------------------------------------------------- #


def test_add_record_creates_first_version_and_writes_bookkeeping(db: MemoryDatabase) -> None:
    record = _repo(db).add_record(_fact(content="项目运行在 Python 3.11 上"))

    assert record.status is MemoryStatus.ACTIVE
    assert record.active_version_id is not None
    assert record.active_version is not None
    assert record.active_version.version == 1
    assert record.active_version.lexical_text  # 写入时必须带上分词结果

    versions = _repo(db).list_versions(record.id)
    assert [v.version for v in versions] == [1]
    assert versions[0].content_hash

    sources = _repo(db).list_sources(record.active_version_id)
    assert len(sources) == 1

    with db.diagnostic_session() as conn:
        outbox = conn.execute(
            "SELECT status FROM memory_embedding_outbox WHERE memory_version_id = %s",
            (UUID(record.active_version_id),),
        ).fetchall()
    assert [row["status"] for row in outbox] == ["pending"]


def test_second_active_record_with_same_subject_is_rejected(db: MemoryDatabase) -> None:
    repo = _repo(db)
    repo.add_record(_fact(subject_key="fact:project:demo:editor", content="编辑器用 vim"))

    with pytest.raises(ActiveSubjectConflictError) as excinfo:
        repo.add_record(_fact(subject_key="fact:project:demo:editor", content="编辑器改用 vscode"))

    assert excinfo.value.subject_key == "fact:project:demo:editor"
    # 冲突必须翻译成领域异常，且能指出已存在的记录。
    UUID(excinfo.value.existing_memory_id)


def test_duplicate_content_is_reported_as_noop_not_new_version(db: MemoryDatabase) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _fact(subject_key="fact:project:demo:shell", content="默认 shell 是 bash")
    )

    with pytest.raises(DuplicateVersionError):
        repo.add_version(NewVersionCommand(memory_id=record.id, content="默认 shell 是 bash"))

    assert [v.version for v in repo.list_versions(record.id)] == [1]


def test_add_version_keeps_history_and_moves_active_pointer(db: MemoryDatabase) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _fact(subject_key="fact:project:demo:python_minor", content="Python 版本是 3.10")
    )

    new_version = repo.add_version(
        NewVersionCommand(memory_id=record.id, content="Python 版本是 3.11")
    )

    assert new_version.version == 2
    versions = repo.list_versions(record.id)
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].content != versions[1].content

    refreshed = repo.get_record(record.id)
    assert refreshed is not None
    assert refreshed.active_version_id == new_version.id


def test_add_version_is_rejected_after_supersede(db: MemoryDatabase) -> None:
    repo = _repo(db)
    old = repo.add_record(_fact(subject_key="fact:project:demo:timezone", content="时区是 UTC"))
    new = repo.add_record(
        _fact(
            subject_key="fact:project:demo:timezone_new",
            content="时区改成 Asia/Shanghai",
        )
    )
    repo.supersede(old.id, new.id, reason="用户改口")

    with pytest.raises(RecordNotActiveError):
        repo.add_version(NewVersionCommand(memory_id=old.id, content="再改一次"))


def test_failed_add_version_rolls_back_completely(db: MemoryDatabase) -> None:
    """失败的新版本不能留下半条记录：版本表、active 指针都必须保持原样。"""

    repo = _repo(db)
    record = repo.add_record(
        _fact(subject_key="fact:project:demo:rollback", content="回滚测试初始内容")
    )
    before = repo.get_record(record.id)
    assert before is not None

    with pytest.raises(DuplicateVersionError):
        repo.add_version(
            NewVersionCommand(memory_id=record.id, content="回滚测试初始内容")
        )

    after = repo.get_record(record.id)
    assert after is not None
    assert after.active_version_id == before.active_version_id
    assert [v.version for v in repo.list_versions(record.id)] == [1]


def test_supersede_is_atomic_and_audited(db: MemoryDatabase) -> None:
    repo = _repo(db)
    old = repo.add_record(
        _fact(subject_key="fact:project:demo:db_engine", content="数据库用 MySQL")
    )
    new = repo.add_record(
        _fact(subject_key="fact:project:demo:db_engine_pg", content="数据库改用 PostgreSQL")
    )

    repo.supersede(old.id, new.id, reason="架构调整", confidence=0.9)

    retired = repo.get_record(old.id)
    assert retired is not None
    assert retired.status is MemoryStatus.SUPERSEDED
    # 版本级 valid_to 才是"当时有效的事实"的依据。
    old_versions = repo.list_versions(old.id)
    assert old_versions[0].valid_to is not None
    assert repo.get_active(old.id) is None

    # 关系与状态在同一事务里落库，必须都在。
    with db.diagnostic_session() as conn:
        relations = conn.execute(
            "SELECT relation_type, reason, confidence FROM memory_relations "
            "WHERE from_memory_id = %s AND to_memory_id = %s",
            (UUID(new.id), UUID(old.id)),
        ).fetchall()
    assert len(relations) == 1
    assert relations[0]["relation_type"] == "supersedes"
    assert relations[0]["reason"] == "架构调整"

    # 重复 supersede 同一个旧记录必须报错（它已经不是 active）。
    with pytest.raises(RecordNotActiveError):
        repo.supersede(old.id, new.id)


def test_supersede_rejects_unknown_memory(db: MemoryDatabase) -> None:
    repo = _repo(db)
    real = repo.add_record(_fact(subject_key="fact:project:demo:unknown", content="真实记录"))
    with pytest.raises(RecordNotFoundError):
        repo.supersede(str(uuid4()), real.id)


def test_mark_expired_ends_validity_without_deleting_history(db: MemoryDatabase) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _fact(subject_key="fact:project:demo:expiring", content="这条记忆会过期")
    )
    repo.mark_expired(record.id, _now() + timedelta(seconds=1))

    expired = repo.get_record(record.id)
    assert expired is not None
    assert expired.status is MemoryStatus.EXPIRED
    assert repo.list_versions(record.id)[0].valid_to is not None


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #


def test_candidate_idempotency_key_returns_the_same_candidate(db: MemoryDatabase) -> None:
    repo = _repo(db)
    candidate = MemoryCandidate(
        memory_type=MemoryType.FACT,
        scope=Scope.PROJECT,
        subject_key="fact:project:demo:candidate_target",
        content="候选内容",
        user_id=ALICE.user_id,
        project_id=ALICE.project_id,
    )
    first = repo.add_candidate(
        candidate, idempotency_key="evt-1", source_event_range={"start": "rows-1", "end": "rows-2"}
    )
    second = repo.add_candidate(
        candidate, idempotency_key="evt-1", source_event_range={"start": "rows-1", "end": "rows-2"}
    )
    assert first == second

    with db.diagnostic_session() as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM memory_write_candidates WHERE idempotency_key = 'evt-1'"
        ).fetchone()["n"]
    assert count == 1

    pending = repo.list_candidates()
    assert [item.id for item in pending] == [first]

    repo.record_decision(first, WriteDecision.ADD, reason="来源充分")
    # 候选决策只允许发生一次：重复决策必须报错，而不是覆盖审计。
    with pytest.raises(CandidateNotFoundError):
        repo.record_decision(first, WriteDecision.NOOP, reason="重复决策")
    assert repo.list_candidates() == []
    assert len(repo.list_candidates(decision=WriteDecision.ADD)) == 1


def test_source_and_outbox_are_idempotent(db: MemoryDatabase) -> None:
    repo = _repo(db)
    source = MemorySource(
        source_type=SourceType.TOOL_RESULT,
        source_id="tool-call-1",
        observed_at=_now(),
        source_excerpt="工具输出摘要",
    )
    record = repo.add_record(
        _fact(subject_key="fact:project:demo:idem", content="幂等测试", sources=(source,))
    )
    version_id = record.active_version_id
    assert version_id is not None

    repo.add_source(version_id, source)
    repo.enqueue_embedding(version_id)

    assert len(repo.list_sources(version_id)) == 1
    with db.diagnostic_session() as conn:
        outbox = conn.execute(
            "SELECT count(*) AS n FROM memory_embedding_outbox WHERE memory_version_id = %s",
            (UUID(version_id),),
        ).fetchone()["n"]
    assert outbox == 1


def test_set_embedding_closes_outbox_and_enforces_dimension(db: MemoryDatabase) -> None:
    from klonet_agent.memory.domain import MemoryDomainError

    repo = _repo(db)
    record = repo.add_record(
        _fact(subject_key="fact:project:demo:embedding", content="向量写入测试")
    )
    version_id = record.active_version_id
    assert version_id is not None

    with pytest.raises(MemoryDomainError):
        repo.set_embedding(version_id, (1.0, 0.0), embedding_model="m", embedding_version="v1")

    repo.set_embedding(
        version_id,
        _unit_vector(0),
        embedding_model="text-embedding-v4",
        embedding_version="1",
    )
    stored = repo.get_version(version_id)
    assert stored is not None
    assert stored.embedding_model == "text-embedding-v4"
    assert stored.embedding is not None and len(stored.embedding) == EMBEDDING_DIMENSIONS

    with db.diagnostic_session() as conn:
        status = conn.execute(
            "SELECT status FROM memory_embedding_outbox WHERE memory_version_id = %s",
            (UUID(version_id),),
        ).fetchone()["status"]
    assert status == "completed"


# --------------------------------------------------------------------------- #
# 租户隔离（应用层 + RLS 双保险）
# --------------------------------------------------------------------------- #


def test_scope_violation_is_rejected_before_hitting_sql(db: MemoryDatabase) -> None:
    with pytest.raises(ScopeViolationError):
        _repo(db, ALICE).add_record(_fact(tenant=BOB, user_id=BOB.user_id))


def test_application_layer_isolation_holds_even_without_rls(db: MemoryDatabase) -> None:
    """即使连接是超级用户/BYPASSRLS，应用层过滤也不能跨租户。

    这条测试是刻意在**未切角色**的会话里跑的：DSN 账号通常是超级用户，RLS 对它
    不生效。如果仓库只依赖 RLS，这里就会读到别人的记忆。
    """

    alice_repo = _repo(db, ALICE)
    record = alice_repo.add_record(
        _fact(subject_key="fact:project:demo:app_isolation", content="alice 的私有事实")
    )

    bob_repo = _repo(db, BOB)
    assert bob_repo.get_record(record.id) is None
    assert bob_repo.list_versions(record.id) == []
    assert bob_repo.get_version(record.active_version_id) is None
    assert bob_repo.list_sources(record.active_version_id) == []
    assert bob_repo.find_active_by_subject("fact:project:demo:app_isolation") is None
    assert bob_repo.search(MemoryQuery(text="alice 的私有事实")) == []


def test_rls_denies_cross_user_rows_at_database_layer(
    db: MemoryDatabase, app_role: str
) -> None:
    alice_record = _repo(db, ALICE).add_record(
        _fact(subject_key="fact:project:demo:rls_target", content="只有 alice 能读")
    )

    with _as_role(db, BOB, app_role) as conn:
        assert conn.execute("SELECT count(*) AS n FROM memory_records").fetchone()["n"] == 0
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM memory_records WHERE id = %s",
                (UUID(alice_record.id),),
            ).fetchone()["n"]
            == 0
        )
        # 子表也必须一起被挡住。
        assert conn.execute("SELECT count(*) AS n FROM memory_versions").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM memory_sources").fetchone()["n"] == 0


def test_rls_blocks_writing_rows_for_another_user(
    db: MemoryDatabase, app_role: str
) -> None:
    with pytest.raises(Exception) as excinfo:
        with _as_role(db, BOB, app_role) as conn:
            conn.execute(
                """
                INSERT INTO memory_records
                    (id, user_id, project_id, scope, memory_type, subject_key)
                VALUES (%s, 'alice', 'demo', 'project', 'fact', 'fact:project:demo:stolen')
                """,
                (uuid4(),),
            )
    # 42501 = insufficient_privilege，即 RLS 的 WITH CHECK 拒绝。
    assert getattr(excinfo.value, "sqlstate", None) == "42501"


def test_tenant_variable_does_not_leak_across_pooled_transactions(
    db: MemoryDatabase, app_role: str
) -> None:
    """SET LOCAL 是事务级的：上一个请求的租户不能留在池化连接里。

    注意 ``current_setting('app.user_id', true)`` 在事务结束后返回的是**空串**
    而不是 NULL——PostgreSQL 的自定义 GUC 一旦被赋过值就会留下占位符，
    revert 后变成 ''。所以断言写成"既不是 NULL 也不是上个租户"，而不是只判 NULL。
    这个细节直接关系到 RLS 的正确性：因为 ``user_id`` 和 ``project_id`` 都有
    ``<> ''`` 的 CHECK，空串租户匹配不到任何行，失败方向是安全的。
    """

    _repo(db, ALICE).add_record(
        _fact(subject_key="fact:project:demo:leak_probe", content="泄漏探针")
    )

    # 直接看结构状态：事务结束后不能还留着 alice。
    with db.diagnostic_session() as conn:
        leftover = conn.execute(
            "SELECT current_setting('app.user_id', true) AS v"
        ).fetchone()["v"]
    assert leftover in (None, ""), leftover

    # 换成另一个租户（项目相同，所以一旦泄漏就会读到 alice 的行）。
    with _as_role(db, NOBODY, app_role) as conn:
        assert conn.execute("SELECT count(*) AS n FROM memory_records").fetchone()["n"] == 0


def test_shared_ops_is_readable_only_by_the_ops_role(
    db: MemoryDatabase, app_role: str, ops_role: str
) -> None:
    shared_repo = _repo(db, SHARED)
    shared_repo.add_record(
        NewRecordCommand(
            user_id=SHARED.user_id,
            scope=Scope.SHARED_OPS,
            memory_type=MemoryType.FACT,
            subject_key="fact:shared_ops:cluster:deploy_target",
            content="共享部署目标 10.0.0.5",
            project_id=None,
        )
    )

    # 普通应用角色即使把租户设成 user_id='shared' 也读不到共享记忆。
    with _as_role(db, SHARED, app_role) as conn:
        assert conn.execute("SELECT count(*) AS n FROM memory_records").fetchone()["n"] == 0

    with _as_role(db, SHARED, ops_role) as conn:
        assert conn.execute("SELECT count(*) AS n FROM memory_records").fetchone()["n"] >= 1


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #


def test_search_fuses_lexical_and_semantic_channels(db: MemoryDatabase) -> None:
    repo = _repo(db)
    target = repo.add_record(
        _fact(
            subject_key="fact:project:demo:retrieval",
            content="Klonet 平台使用 nginx 反向代理 /VEMU2/ 路径",
        )
    )
    repo.add_record(
        _fact(
            subject_key="fact:project:demo:unrelated",
            content="无关内容：午餐吃的是面条",
        )
    )
    version_id = target.active_version_id
    assert version_id is not None
    repo.set_embedding(
        version_id, _unit_vector(3), embedding_model="text-embedding-v4", embedding_version="1"
    )

    lexical_hits = repo.search(MemoryQuery(text="nginx 反向代理", limit=5))
    assert lexical_hits, "全文通道应当能召回"
    assert lexical_hits[0].record.id == target.id
    assert "lexical" in lexical_hits[0].reasons

    semantic_hits = repo.search(
        MemoryQuery(text="完全不相干的问法", limit=5), query_embedding=_unit_vector(3)
    )
    assert semantic_hits, "向量通道应当能召回"
    assert "semantic" in semantic_hits[0].reasons

    # 精确标识符通道：路径类 token 会被分词器切碎，必须靠 exact 通道兜住。
    exact_hits = repo.search(MemoryQuery(text="/VEMU2/", limit=5))
    assert exact_hits
    assert exact_hits[0].record.id == target.id
    assert exact_hits[0].exact_match is True


def test_search_rejects_mismatched_query_dimension(db: MemoryDatabase) -> None:
    from klonet_agent.memory.domain import MemoryDomainError

    with pytest.raises(MemoryDomainError):
        _repo(db).search(MemoryQuery(text="维度不对"), query_embedding=(1.0, 2.0))


def test_search_excludes_superseded_and_expired(db: MemoryDatabase) -> None:
    repo = _repo(db)
    old = repo.add_record(
        _fact(subject_key="fact:project:demo:search_old", content="检索用旧事实 alpha")
    )
    new = repo.add_record(
        _fact(subject_key="fact:project:demo:search_new", content="检索用新事实 beta")
    )
    repo.supersede(old.id, new.id)

    hits = repo.search(MemoryQuery(text="检索用旧事实 alpha", limit=5))
    assert all(hit.record.id != old.id for hit in hits)
