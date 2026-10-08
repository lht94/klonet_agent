"""阶段 7 的管理接口测试：查看、删除、归档与物理清理。

核心是那条完成标准：**删除请求完成后正文、向量和导出视图均不可再召回**。
所以这里不只断言状态字段，还用真实检索去证明"搜不到了"。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from klonet_agent.memory.admin import MemoryAdmin, RetentionPolicy
from klonet_agent.memory.domain import (
    MemoryQuery,
    MemorySource,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
)
from klonet_agent.memory.repository import EMBEDDING_DIMENSIONS, NewRecordCommand

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"

ALICE = Tenant(user_id="alice", project_id="demo")
BOB = Tenant(user_id="bob", project_id="demo")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_record(content: str, *, subject_key: str, user_id: str = ALICE.user_id):
    return NewRecordCommand(
        user_id=user_id,
        scope=Scope.PROJECT,
        memory_type=MemoryType.FACT,
        subject_key=subject_key,
        content=content,
        project_id="demo",
        sources=(
            MemorySource(
                source_type=SourceType.USER_STATEMENT,
                source_id="rows-9",
                observed_at=_now(),
                source_excerpt="用户说明",
            ),
        ),
        confidence=0.9,
    )


def _vec(seed: float = 0.5):
    return tuple(seed for _ in range(EMBEDDING_DIMENSIONS))


# --------------------------------------------------------------------------- #
# 真库夹具（本模块全部测试都需要真库）
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin_dsn() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过管理接口集成测试"
            "（「删除后不可召回」必须在真库上证明）"
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


def _repo(db, tenant: Tenant = ALICE):
    from klonet_agent.memory.postgres import PostgresMemoryRepository

    return PostgresMemoryRepository(db, tenant)


# --------------------------------------------------------------------------- #
# 删除：三件事必须一起做到
# --------------------------------------------------------------------------- #


def test_forget_makes_the_memory_unrecallable(db) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _new_record("本项目运行时要求 Python 3.11", subject_key="fact:project:demo:runtime")
    )
    repo.set_embedding(
        record.active_version_id, _vec(0.7),
        embedding_model="test-model", embedding_version="v1",
    )
    admin = MemoryAdmin(repo)
    assert admin.recall_check(record.id, text="Python 运行时要求") is True

    outcome = admin.forget(record.id, reason="用户要求删除")

    assert outcome.deleted is True
    # 1) 检索层面不可召回（current 与 as_of 都查不到）。
    assert admin.recall_check(record.id, text="Python 运行时要求") is False
    historical = repo.search(
        MemoryQuery(text="Python 运行时要求", as_of=_now() + timedelta(days=1), limit=200)
    )
    assert record.id not in {hit.record.id for hit in historical}


def test_forget_clears_the_vector_and_outbox(db) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _new_record("端口 8080 被占用", subject_key="fact:project:demo:port")
    )
    version_id = UUID(record.active_version_id)
    repo.set_embedding(
        version_id, _vec(0.3), embedding_model="test-model", embedding_version="v1"
    )

    MemoryAdmin(repo).forget(record.id)

    stored = repo.get_version(record.active_version_id)
    assert stored is not None
    # 2) 向量必须同步清除：留着它，语义通道还会命中。
    assert stored.embedding is None
    assert stored.embedding_model is None and stored.embedding_version is None
    with db.tenant_session(ALICE) as conn:
        remaining = conn.execute(
            "SELECT count(*) AS n FROM memory_embedding_outbox WHERE memory_version_id = %s",
            (version_id,),
        ).fetchone()["n"]
    assert remaining == 0


def test_forget_is_idempotent(db) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _new_record("重复删除应当安全", subject_key="fact:project:demo:twice")
    )
    admin = MemoryAdmin(repo)

    admin.forget(record.id)
    admin.forget(record.id)  # 不该抛
    assert repo.get_record(record.id).status.value == "deleted"


def test_forget_does_not_touch_other_tenants(db) -> None:
    alice = _repo(db, ALICE)
    bob = _repo(db, BOB)
    record = alice.add_record(
        _new_record("alice 的私有事实", subject_key="fact:project:demo:alice_only")
    )

    with pytest.raises(Exception):
        MemoryAdmin(bob).forget(record.id)
    assert alice.get_record(record.id).status.value == "active"


def test_explain_reports_status_versions_and_sources(db) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _new_record("可审计的一条记忆", subject_key="fact:project:demo:audit")
    )
    admin = MemoryAdmin(repo)

    before = admin.explain(record.id)
    assert before["found"] is True
    assert before["status"] == "active"
    assert before["sources"] == ["user_statement:rows-9"]

    admin.forget(record.id, reason="隐私")
    after = admin.explain(record.id)
    assert after["status"] == "deleted"
    assert after["valid_to"] is not None


def test_explain_for_unknown_id_is_not_an_error(db) -> None:
    result = MemoryAdmin(_repo(db)).explain("00000000-0000-0000-0000-000000000000")
    assert result == {
        "memory_id": "00000000-0000-0000-0000-000000000000",
        "found": False,
    }


# --------------------------------------------------------------------------- #
# 查看与物理清理
# --------------------------------------------------------------------------- #


def test_list_memories_can_include_or_hide_deleted(db) -> None:
    repo = _repo(db)
    kept = repo.add_record(
        _new_record("保留的一条", subject_key="fact:project:demo:keep")
    )
    gone = repo.add_record(
        _new_record("要删的一条", subject_key="fact:project:demo:gone")
    )
    admin = MemoryAdmin(repo)
    admin.forget(gone.id)

    with_deleted = {item.id for item in admin.list_memories(limit=100)}
    without_deleted = {
        item.id for item in admin.list_memories(limit=100, include_deleted=False)
    }
    assert {kept.id, gone.id} <= with_deleted
    assert kept.id in without_deleted and gone.id not in without_deleted


def test_purge_only_removes_deleted_records_past_retention(db) -> None:
    repo = _repo(db)
    admin = MemoryAdmin(repo)
    deleted = repo.add_record(
        _new_record("删了很久的一条", subject_key="fact:project:demo:old")
    )
    alive = repo.add_record(
        _new_record("还活着的一条", subject_key="fact:project:demo:alive")
    )
    admin.forget(deleted.id)

    policy = RetentionPolicy(soft_delete_retention_days=30)
    # 保留期内的删除记录不该被清掉。
    assert admin.purge(policy=policy) == 0
    assert repo.get_record(deleted.id) is not None

    # 把门槛推到未来（等价于"保留期已过"），才允许物理清理。
    removed = admin.purge(policy=policy, now=_now() + timedelta(days=60))
    assert removed == 1
    assert repo.get_record(deleted.id) is None
    # 没被删除的记录一根毫毛都不能少。
    assert repo.get_record(alive.id) is not None


def test_archive_expired_is_a_noop_for_healthy_data(db) -> None:
    """正常情况下 active 记录都有未结束的有效期，归档不该误伤。"""

    repo = _repo(db)
    repo.add_record(_new_record("正常的一条", subject_key="fact:project:demo:ok"))
    assert MemoryAdmin(repo).archive_expired() == 0


def test_admin_traces_deletion_when_a_tracer_is_given(db) -> None:
    class _Tracer:
        def __init__(self):
            self.events: list[str] = []

        def record_privileged_event(self, **kwargs):
            self.events.append(str(kwargs.get("event")))

    repo = _repo(db)
    record = repo.add_record(
        _new_record("被审计的删除", subject_key="fact:project:demo:traced")
    )
    tracer = _Tracer()
    MemoryAdmin(repo, tracer=tracer).forget(record.id, reason="用户要求")

    assert tracer.events == ["memory_forgotten"]
