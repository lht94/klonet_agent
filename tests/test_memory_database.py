"""记忆库基础设施的离线测试（不需要真库）。

真库相关的验收在 ``tests/test_memory_postgres_repository.py``；这里只覆盖不依赖
数据库连接的纯逻辑，这样"脱敏有没有失效""迁移文件有没有被改名"这类问题在
任何机器上都能被立刻发现，而不是等到有人配了 DSN 才暴露。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from klonet_agent.memory.database import (
    DSN_ENV_VARS,
    MIGRATIONS_DIR,
    REQUIRED_TABLES,
    MemoryDatabase,
    MemoryDatabaseError,
    _assert_in_transaction,
    load_migration_files,
    mask_dsn,
    memory_dsn,
    migration_checksum,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_SQL = REPO_ROOT / "migrations" / "0001_init.sql"


# --------------------------------------------------------------------------- #
# DSN 脱敏
# --------------------------------------------------------------------------- #


def test_mask_dsn_redacts_password_in_url_form() -> None:
    masked = mask_dsn("postgresql://klonet_app:s3cr3t@10.0.0.5:5432/klonet_memory")
    assert "s3cr3t" not in masked
    assert masked == "postgresql://klonet_app:***@10.0.0.5:5432/klonet_memory"


def test_mask_dsn_redacts_password_in_keyword_form() -> None:
    """回归：libpq 关键字/值形式曾经被原样返回，把明文密码写进了报告。

    ``psycopg.conninfo.make_conninfo()`` 产出的就是这种形式，程序内部拼 DSN
    时很常见，所以这条比 URL 形式更危险。
    """

    masked = mask_dsn(
        "user=klonet_app password=s3cr3t dbname=klonet_memory host=127.0.0.1 port=5432"
    )
    assert "s3cr3t" not in masked
    assert masked == (
        "user=klonet_app password=*** dbname=klonet_memory host=127.0.0.1 port=5432"
    )


def test_mask_dsn_redacts_quoted_password_in_keyword_form() -> None:
    masked = mask_dsn("host=127.0.0.1 password='p a s s' dbname=klonet_memory")
    assert "p a s s" not in masked
    assert "password=***" in masked


def test_mask_dsn_redacts_password_in_query_string() -> None:
    masked = mask_dsn("postgresql:///klonet_memory?host=127.0.0.1&password=s3cr3t")
    assert "s3cr3t" not in masked
    assert "password=***" in masked


def test_mask_dsn_leaves_passwordless_dsn_untouched() -> None:
    # 生产上常用 unix socket + peer 认证，本来就没有密码，不应该被改写。
    dsn = "postgresql:///klonet_memory?host=/var/run/postgresql"
    assert mask_dsn(dsn) == dsn


# --------------------------------------------------------------------------- #
# DSN 解析
# --------------------------------------------------------------------------- #


def test_memory_dsn_uses_first_configured_variable() -> None:
    assert memory_dsn({}) is None
    assert memory_dsn({"KLONET_AGENT_MEMORY_DSN": "  "}) is None
    assert memory_dsn({"KLONET_AGENT_MEMORY_DSN": "a", "DATABASE_URL": "b"}) == "a"
    assert memory_dsn({"MEMORY_DATABASE_URL": "b", "DATABASE_URL": "c"}) == "b"
    # 顺序以常量为准，测试与实现不各写一遍。
    assert DSN_ENV_VARS[0] == "KLONET_AGENT_MEMORY_DSN"


def test_from_env_without_dsn_raises_instead_of_silently_disabling() -> None:
    with pytest.raises(MemoryDatabaseError):
        MemoryDatabase.from_env(env={})


def test_database_config_rejects_invalid_pool_sizes() -> None:
    with pytest.raises(MemoryDatabaseError):
        MemoryDatabase("postgresql://x/y", min_size=4, max_size=2)
    with pytest.raises(MemoryDatabaseError):
        MemoryDatabase("postgresql://x/y", max_size=0)
    with pytest.raises(MemoryDatabaseError):
        MemoryDatabase("   ")


def test_using_an_unopened_database_raises() -> None:
    database = MemoryDatabase("postgresql://x/y")
    with pytest.raises(MemoryDatabaseError):
        database.applied_migrations()


def test_tenant_binding_refuses_to_run_outside_a_transaction() -> None:
    """事务外 ``set_config(is_local=true)`` 会立即失效，等价于没绑定。

    这种情况必须报错而不是静默通过——静默通过意味着 RLS 看到一个空租户，
    查询结果会变成"什么都查不到"而没人知道为什么。
    """

    psycopg = pytest.importorskip("psycopg")

    class _Info:
        def __init__(self, status: object) -> None:
            self.transaction_status = status

    class _Conn:
        def __init__(self, status: object) -> None:
            self.info = _Info(status)

        def execute(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("不应该执行任何 SQL")

    with pytest.raises(MemoryDatabaseError):
        _assert_in_transaction(_Conn(psycopg.pq.TransactionStatus.IDLE))

    # 事务中（INTRANS / INERROR）应当放行。
    _assert_in_transaction(_Conn(psycopg.pq.TransactionStatus.INTRANS))


# --------------------------------------------------------------------------- #
# 迁移文件
# --------------------------------------------------------------------------- #


def test_migration_files_are_discovered_in_order() -> None:
    migrations = load_migration_files()
    versions = [item.version for item in migrations]
    assert versions == sorted(versions)
    assert versions == [
        "0001_init",
        "0002_roles_and_grants",
        "0003_immutable_versions",
        "0004_governance",
        "0005_governance_provenance",
        "0006_memory_maintenance",
    ]
    for item in migrations:
        assert item.sql.strip()
        assert len(item.checksum) == 64


def test_migration_checksum_ignores_line_endings() -> None:
    # 同一份文件在 Windows/Linux 检出时行尾不同，不该被误判成"迁移被改过"。
    assert migration_checksum(b"a\r\nb\r\n") == migration_checksum(b"a\nb\n")
    # 内容真的变了必须体现在哈希上。
    assert migration_checksum(b"a\nb\n") != migration_checksum(b"a\nc\n")


def test_migration_loader_ignores_non_matching_files(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "notes.md").write_text("not a migration", encoding="utf-8")
    (tmp_path / "draft.sql").write_text("SELECT 2;", encoding="utf-8")

    versions = [item.version for item in load_migration_files(tmp_path)]
    assert versions == ["0001_init"]


def test_migration_loader_reports_missing_directory(tmp_path: Path) -> None:
    from klonet_agent.memory.database import MigrationError

    with pytest.raises(MigrationError):
        load_migration_files(tmp_path / "does-not-exist")


def test_schema_covers_every_required_table() -> None:
    """REQUIRED_TABLES 必须与迁移文件保持一致。

    启动自检靠这个常量判断 schema 完不完整；如果常量漏了表，自检就会放行一个
    残缺的库。
    """

    sql = INIT_SQL.read_text(encoding="utf-8")
    for table in REQUIRED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table} " in sql, table
    assert len(REQUIRED_TABLES) == 6

    # 每张业务表都必须开启并强制 RLS，否则"数据库层拒绝跨用户查询"不成立。
    for table in REQUIRED_TABLES:
        assert f"ALTER TABLE {table} " in sql
    assert sql.count("ENABLE ROW LEVEL SECURITY") >= len(REQUIRED_TABLES)
    assert sql.count("FORCE ROW LEVEL SECURITY") >= len(REQUIRED_TABLES)


def test_migration_files_are_never_modified_after_being_applied() -> None:
    """迁移文件一旦执行即不可变。

    没有权威台账可比对时，退一步保证：迁移只增不改——新增迁移的编号必须
    严格大于已有的最大值。改了老文件的话这里发现不了，但执行器的 checksum
    台账会在真库上拦住它。
    """

    versions = [item.version for item in load_migration_files(MIGRATIONS_DIR)]
    numbers = [int(version.split("_", 1)[0]) for version in versions]
    assert numbers == sorted(set(numbers)), numbers
