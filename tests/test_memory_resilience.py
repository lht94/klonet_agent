"""阶段 7：删除权与写入路径的鲁棒性测试。

计划明确要求"删除请求完成后正文、向量和导出视图均不可再召回"，并要求做
**进程重启、embedding 中断、数据库事务回滚**三类验证。这里把它们收敛到
同一条主线上——"删除之后，任何后台/重启路径都不能把它变回来"：

1. 进程重启：换一个全新的仓库对象（模拟新进程）再查，仍然查不到；
2. embedding 中断：删除后 worker 再跑一轮，不会给已删除版本补向量；
3. 事务回滚：删除中途失败必须整体回滚，不留"状态已删、向量还在"的半成品。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

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

ALICE = Tenant(user_id="alice-resilience", project_id="demo")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _command(content: str, *, subject_key: str) -> NewRecordCommand:
    return NewRecordCommand(
        user_id=ALICE.user_id,
        scope=Scope.PROJECT,
        memory_type=MemoryType.FACT,
        subject_key=subject_key,
        content=content,
        project_id=ALICE.project_id,
        sources=(
            MemorySource(
                source_type=SourceType.USER_STATEMENT,
                source_id="rows-1",
                observed_at=_now(),
                source_excerpt="用户说明",
            ),
        ),
        confidence=0.9,
    )


def _vec(seed: float = 0.4):
    return tuple(seed for _ in range(EMBEDDING_DIMENSIONS))


# --------------------------------------------------------------------------- #
# 真库夹具（本模块全部用例都需要真库）
# --------------------------------------------------------------------------- #


@pytest.fixture
def dsn() -> str:
    value = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not value:
        pytest.skip(f"未设置 {TEST_DSN_ENV}，跳过删除权鲁棒性测试")
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        pytest.skip("未安装 psycopg，跳过真库测试")
    try:
        with psycopg.connect(value, connect_timeout=5.0):
            pass
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"无法连接 {TEST_DSN_ENV}：{exc}")
    return value


@pytest.fixture
def db(dsn: str):
    from klonet_agent.memory.database import MemoryDatabase, temporary_database

    with temporary_database(dsn) as temp_dsn:
        database = MemoryDatabase(temp_dsn, min_size=1, max_size=4)
        database.open()
        try:
            database.run_migrations()
            yield database
        finally:
            database.close()


def _repo(db):
    from klonet_agent.memory.postgres import PostgresMemoryRepository

    return PostgresMemoryRepository(db, ALICE)


# --------------------------------------------------------------------------- #
# 1. 进程重启
# --------------------------------------------------------------------------- #


def test_deleted_memory_stays_gone_after_a_restart(db) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _command("重启之后也不该再出现", subject_key="fact:project:demo:restart")
    )
    repo.set_embedding(
        record.active_version_id,
        _vec(0.6),
        embedding_model="test-model",
        embedding_version="v1",
    )
    repo.delete_memory(record.id, reason="用户要求删除")

    # 新进程 = 新的仓库与新的连接，只有库里的状态是共享的。
    restarted = _repo(db)
    assert restarted.get_record(record.id).status.value == "deleted"
    assert restarted.search(MemoryQuery(text="重启 之后 出现", limit=50)) == []
    assert (
        restarted.search(
            MemoryQuery(text="重启 之后 出现", limit=50, as_of=_now() + timedelta(days=1))
        )
        == []
    )


# --------------------------------------------------------------------------- #
# 2. embedding 中断
# --------------------------------------------------------------------------- #


class _CountingEmbedder:
    """只数调用次数；真的被调用就说明有人想给已删除的版本补向量。"""

    model = "counting"

    def __init__(self):
        self.calls = 0

    def embed(self, text: str):
        self.calls += 1
        return _vec(0.1)


def test_embedding_worker_does_not_resurrect_a_deleted_memory(db) -> None:
    from klonet_agent.memory.embedding_worker import EmbeddingWorker

    repo = _repo(db)
    record = repo.add_record(
        _command("待补向量的一条", subject_key="fact:project:demo:pending")
    )
    # 还没补向量就被删除：outbox 里有一条待办。
    pending_before = repo.embedding_outbox_stats()
    assert pending_before.pending >= 1

    repo.delete_memory(record.id, reason="用户要求删除")

    embedder = _CountingEmbedder()
    stats = EmbeddingWorker(repo, embedder).run()
    assert stats.claimed == 0, "已删除版本的任务不该再被领取"
    assert embedder.calls == 0, "worker 不该为已删除的正文生成向量"

    stored = repo.get_version(record.active_version_id)
    assert stored is not None and stored.embedding is None


# --------------------------------------------------------------------------- #
# 3. 数据库事务回滚
# --------------------------------------------------------------------------- #


class _FailingConn:
    """在第 3 条语句（清向量）处注入失败，模拟"删到一半"中断。"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        text = str(sql)
        if "UPDATE memory_versions" in text and "embedding = NULL" in text:
            raise RuntimeError("injected failure while clearing embeddings")
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_failed_deletion_rolls_back_completely(db, monkeypatch) -> None:
    repo = _repo(db)
    record = repo.add_record(
        _command("删到一半失败的一条", subject_key="fact:project:demo:halfway")
    )
    repo.set_embedding(
        record.active_version_id,
        _vec(0.8),
        embedding_model="test-model",
        embedding_version="v1",
    )

    original_session = repo._session

    @contextmanager
    def failing_session(*, readonly: bool = False):
        with original_session(readonly=readonly) as conn:
            yield _FailingConn(conn)

    monkeypatch.setattr(repo, "_session", failing_session)

    with pytest.raises(RuntimeError):
        repo.delete_memory(record.id, reason="故意失败")

    monkeypatch.undo()

    # 全有或全无：状态、有效期、向量、outbox 一处都不能变。
    fresh = repo.get_record(record.id)
    assert fresh is not None and fresh.status.value == "active"
    version = repo.get_version(record.active_version_id)
    assert version is not None
    assert version.valid_to is None
    assert version.embedding is not None, "回滚后向量必须还在"


def test_purge_requires_an_explicit_retention_threshold(db) -> None:
    """物理清理不可逆，缺少保留期门槛时必须拒绝执行而不是猜一个默认值。"""

    repo = _repo(db)
    repo.add_record(_command("一条普通记忆", subject_key="fact:project:demo:keep"))

    with pytest.raises(Exception):
        repo.purge_deleted(older_than=None)
    # 拒绝执行之后，数据必须原封不动。
    assert repo.get_record(
        repo.list_records(limit=5)[0].id
    ) is not None
