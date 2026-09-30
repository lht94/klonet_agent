"""PostgreSQL 连接、租户绑定与迁移执行器。

这一层只认 DSN 环境变量，**不感知部署形态**：本机 Docker 容器里的 pgvector 和
远程服务器上原生安装的 PostgreSQL 走完全相同的代码路径，区别只在 DSN、
`pg_hba.conf` 和角色密码。这是刻意的——测试环境和真实执行环境如果走两套代码，
验证就没有意义。

三件事：

1. **连接池**：`MemoryDatabase` 持有一个 `psycopg_pool.ConnectionPool`。
2. **租户绑定**：每个事务开头用 `SET LOCAL` 写入 `app.user_id` / `app.project_id`，
   事务结束由 PostgreSQL 自动清除，绝不把租户变量留在池化会话里
   （否则下一个请求可能读到上一个用户的记忆）。
3. **迁移执行器**：按文件名顺序执行 `migrations/*.sql`，用 `schema_migrations`
   台账保证幂等，并记录 checksum 防止已执行的迁移被偷偷改写。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from klonet_agent.memory.domain import Tenant

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

# 按优先级依次尝试。第一个是项目专用变量，后两个兼容通用部署习惯。
DSN_ENV_VARS: tuple[str, ...] = (
    "KLONET_AGENT_MEMORY_DSN",
    "MEMORY_DATABASE_URL",
    "DATABASE_URL",
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"

# 迁移互斥锁：多个进程同时跑迁移时只有一个真正执行，其余等待后走"已应用"分支。
MIGRATION_ADVISORY_LOCK_KEY = 0x6B6C6F6E_6D656D  # "klonmem"

# 阶段 1 建的表。assert_ready() 用它做启动自检。
REQUIRED_TABLES: tuple[str, ...] = (
    "memory_records",
    "memory_versions",
    "memory_sources",
    "memory_relations",
    "memory_write_candidates",
    "memory_embedding_outbox",
)

_MIGRATION_FILE_PATTERN = re.compile(r"^(?P<version>\d{4}_[A-Za-z0-9_]+)\.sql$")

_CREATE_LEDGER_SQL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
    version    text        PRIMARY KEY,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #


class MemoryDatabaseError(RuntimeError):
    """数据库基础设施错误的基类。"""


class DriverNotInstalledError(MemoryDatabaseError):
    """没有安装 psycopg。属于部署错误，不做任何静默降级。"""


class MigrationError(MemoryDatabaseError):
    """迁移失败，或已应用的迁移被改动。"""


# --------------------------------------------------------------------------- #
# 驱动惰性加载
# --------------------------------------------------------------------------- #
#
# 整个仓库原本零数据库依赖，绝大多数测试和 CLI 路径不需要 psycopg。
# 顶层 import 会让"没装驱动"变成"整个包 import 失败"，所以这里惰性加载，
# 只在真正要用数据库时才报错，并且报错信息直接给出修复命令。


def load_psycopg() -> tuple[Any, Any]:
    """返回 ``(psycopg 模块, dict_row)``，未安装时抛 DriverNotInstalledError。"""

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise DriverNotInstalledError(
            "未安装 PostgreSQL 驱动，无法使用记忆系统数据库后端。"
            "请执行：pip install 'psycopg[binary,pool]'"
        ) from exc
    return psycopg, dict_row


# --------------------------------------------------------------------------- #
# DSN 解析
# --------------------------------------------------------------------------- #


def memory_dsn(env: Mapping[str, str] | None = None) -> str | None:
    """按优先级从环境变量里取记忆库 DSN，全部为空时返回 None。"""

    source: Mapping[str, str] = os.environ if env is None else env
    for name in DSN_ENV_VARS:
        value = (source.get(name) or "").strip()
        if value:
            return value
    return None


def mask_dsn(dsn: str) -> str:
    """把 DSN 里的密码换成 ``***``，用于日志和 trace。

    记忆库 DSN 属于敏感配置，任何对外输出（日志、评测报告、异常信息）都必须先过
    这个函数。

    **必须同时处理两种 DSN 写法**，否则脱敏会静默失效：

    - URL 形式：``postgresql://user:pw@host:5432/db``
    - libpq 关键字/值形式：``user=postgres password=pw dbname=db host=host``

    第二种正是 ``psycopg.conninfo.make_conninfo()`` 的产物，程序内部拼 DSN 时很常见；
    早期版本只按 URL 解析，遇到关键字形式直接原样返回，把明文密码写进了报告。
    """

    text = str(dsn)
    if "://" not in text:
        return _mask_keyword_dsn(text)

    try:
        parts = urlsplit(text)
    except ValueError:
        return "<unparsable-dsn>"
    if not parts.scheme:
        return _mask_keyword_dsn(text)

    masked_query = _DSN_KEYWORD_PASSWORD_RE.sub(r"\1***", parts.query)
    if parts.password is None and masked_query == parts.query:
        # 没有密码就原样返回。``urlunsplit`` 会把 ``postgresql:///db`` 重写成
        # ``postgresql:/db``（空 netloc 的写法被吃掉一个斜杠），凭空改坏一个
        # 本来正确的 DSN——而 unix socket 形式正是这种写法。
        return text

    netloc = parts.netloc
    if parts.password is not None:
        host = parts.hostname or ""
        netloc = f"{parts.username or ''}:***@{host}"
        if parts.port:
            netloc = f"{netloc}:{parts.port}"

    return urlunsplit((parts.scheme, netloc, parts.path, masked_query, parts.fragment))


# 关键字/值形式里的 password（值可能被单引号或双引号包住）。
_DSN_KEYWORD_PASSWORD_RE = re.compile(
    r"(?i)(\bpassword\s*=\s*)('[^']*'|\"[^\"]*\"|\S+)"
)


def _mask_keyword_dsn(text: str) -> str:
    masked = _DSN_KEYWORD_PASSWORD_RE.sub(r"\1***", text)
    # 连 password 关键字都没有的话，原样返回；但已经禁用了 URL 解析失败时的原样返回。
    return masked


# --------------------------------------------------------------------------- #
# 迁移文件
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Migration:
    """一个待执行或已执行的迁移文件。"""

    version: str
    path: Path
    sql: str
    checksum: str


def migration_checksum(raw: bytes) -> str:
    """对迁移内容算 sha256。

    先把 CRLF 归一成 LF 再哈希：同一份文件在 Windows/Linux 检出时行尾不同，
    不应该被误判成"迁移被人改过"。
    """

    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def load_migration_files(directory: Path | None = None) -> list[Migration]:
    """按文件名升序读取迁移目录。

    只接受 ``NNNN_name.sql`` 形式的文件；其它文件（文档、草稿）直接忽略，
    避免误执行。
    """

    base = Path(directory or MIGRATIONS_DIR)
    if not base.is_dir():
        raise MigrationError(f"迁移目录不存在: {base}")

    migrations: list[Migration] = []
    for path in sorted(base.glob("*.sql")):
        match = _MIGRATION_FILE_PATTERN.match(path.name)
        if not match:
            continue
        raw = path.read_bytes()
        migrations.append(
            Migration(
                version=match.group("version"),
                path=path,
                sql=raw.decode("utf-8"),
                checksum=migration_checksum(raw),
            )
        )
    if not migrations:
        raise MigrationError(f"迁移目录里没有可执行的 .sql 迁移: {base}")
    return migrations


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DatabaseConfig:
    """连接池与迁移配置。"""

    dsn: str
    min_size: int = 1
    max_size: int = 8
    # 连接超时按秒给到 libpq；默认值刻意偏短，避免记忆库故障拖垮整个对话回合。
    connect_timeout_seconds: float = 5.0
    migrations_dir: Path = MIGRATIONS_DIR

    def __post_init__(self) -> None:
        if not str(self.dsn).strip():
            raise MemoryDatabaseError("DSN 为空，无法建立记忆库连接")
        if self.min_size < 0 or self.max_size < 1:
            raise MemoryDatabaseError("连接池尺寸非法")
        if self.min_size > self.max_size:
            raise MemoryDatabaseError("min_size 不能大于 max_size")


# --------------------------------------------------------------------------- #
# MemoryDatabase
# --------------------------------------------------------------------------- #


class MemoryDatabase:
    """记忆库的连接池 + 租户事务 + 迁移执行器。

    用法::

        db = MemoryDatabase.from_env()
        db.open()
        db.run_migrations()

        with db.tenant_session(Tenant(user_id="u1", project_id="p1")) as conn:
            conn.execute("SELECT count(*) FROM memory_records")
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        connect_timeout_seconds: float = 5.0,
        migrations_dir: Path | None = None,
    ) -> None:
        self.config = DatabaseConfig(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            connect_timeout_seconds=connect_timeout_seconds,
            migrations_dir=Path(migrations_dir or MIGRATIONS_DIR),
        )
        self._pool: Any | None = None

    # ---------------------------------------------------------------- 构造 --

    @classmethod
    def from_env(cls, *, env: Mapping[str, str] | None = None, **kwargs: Any) -> "MemoryDatabase":
        """从环境变量构造；未配置 DSN 时抛错而不是返回 None。

        调用方若要区分"没配库"，应先自己调用 :func:`memory_dsn`。
        """

        dsn = memory_dsn(env)
        if not dsn:
            raise MemoryDatabaseError(
                "未配置记忆库 DSN，请在环境变量里设置 "
                + " 或 ".join(DSN_ENV_VARS)
            )
        return cls(dsn, **kwargs)

    @property
    def is_open(self) -> bool:
        return self._pool is not None

    @property
    def masked_dsn(self) -> str:
        return mask_dsn(self.config.dsn)

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise MemoryDatabaseError("记忆库连接池未打开，请先调用 open()")
        return self._pool

    # ------------------------------------------------------------ 生命周期 --

    def open(self) -> "MemoryDatabase":
        """打开连接池。重复调用是幂等的。"""

        if self._pool is not None:
            return self
        _, dict_row = load_psycopg()
        from psycopg_pool import ConnectionPool

        self.config  # 触发配置校验
        self._pool = ConnectionPool(
            conninfo=self.config.dsn,
            min_size=self.config.min_size,
            max_size=self.config.max_size,
            # 池取连接的等待上限：比单条 SQL 的超时更短，故障时尽快失败。
            timeout=self.config.connect_timeout_seconds,
            kwargs={
                "autocommit": False,
                "connect_timeout": self.config.connect_timeout_seconds,
                "row_factory": dict_row,
                # 应用名出现在 pg_stat_activity 里，便于排查"哪个进程占着库"。
                "application_name": "klonet-agent-memory",
            },
            open=True,
            name="klonet-agent-memory",
        )
        return self

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def __enter__(self) -> "MemoryDatabase":
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- 租户事务 --

    @contextmanager
    def tenant_session(
        self, tenant: Tenant, *, readonly: bool = False
    ) -> Iterator[Any]:
        """打开一个已绑定租户的事务。

        整个 `with` 块是一个事务：正常退出提交，抛异常整体回滚。租户变量用
        ``set_config(..., is_local=true)`` 写入，因此在事务提交/回滚时被
        PostgreSQL 自动清除，不会残留在池化连接里。
        """

        pool = self._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                if readonly:
                    # SET TRANSACTION 必须是事务里的第一条语句。
                    conn.execute("SET TRANSACTION READ ONLY")
                _bind_tenant(conn, tenant)
                yield conn

    @contextmanager
    def diagnostic_session(self) -> Iterator[Any]:
        """**不绑定租户**的事务，只用于启动自检和测试观察连接状态。

        绝不可用于业务读取：在 RLS 生效的角色下它什么都读不到（这正是我们
        要断言的失败方向——忘记绑定租户时读不到，而不是读到别人的）。
        超级用户账号下它会看到全部数据，所以它也只适合做"现在到底有没有
        残留租户变量"这类结构性检查。
        """

        pool = self._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                yield conn

    # ---------------------------------------------------------------- 迁移 --
    def applied_migrations(self) -> dict[str, str]:
        """返回 ``{version: checksum}``；台账表不存在时返回空字典。"""

        pool = self._require_pool()
        with pool.connection() as conn:
            exists = conn.execute(
                "SELECT to_regclass(%s) AS oid", (SCHEMA_MIGRATIONS_TABLE,)
            ).fetchone()
            if not exists or not exists["oid"]:
                return {}
            rows = conn.execute(
                f"SELECT version, checksum FROM {SCHEMA_MIGRATIONS_TABLE}"
            ).fetchall()
        return {row["version"]: row["checksum"] for row in rows}

    def run_migrations(self) -> list[str]:
        """执行尚未应用的迁移，返回本次真正执行的版本号列表。

        - 全部迁移放在**同一个事务**里：任何一步失败都整体回滚，不留半套 schema。
        - 用 advisory lock 串行化并发调用。
        - 已应用且 checksum 一致 → 跳过（这就是"重复 migration 无副作用"）。
        - 已应用但 checksum 变了 → 直接报错。迁移文件一经执行即不可变，
          要改结构就加新文件。
        """

        pool = self._require_pool()
        migrations = load_migration_files(self.config.migrations_dir)
        applied_now: list[str] = []

        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (MIGRATION_ADVISORY_LOCK_KEY,),
                )
                conn.execute(_CREATE_LEDGER_SQL)
                recorded = {
                    row["version"]: row["checksum"]
                    for row in conn.execute(
                        f"SELECT version, checksum FROM {SCHEMA_MIGRATIONS_TABLE}"
                    ).fetchall()
                }

                for migration in migrations:
                    previous = recorded.get(migration.version)
                    if previous is not None:
                        if previous != migration.checksum:
                            raise MigrationError(
                                f"迁移 {migration.version} 已应用但内容被改动"
                                f"（台账 {previous[:12]}… vs 文件 {migration.checksum[:12]}…）。"
                                "已执行的迁移不可修改，请新增迁移文件。"
                            )
                        continue
                    try:
                        conn.execute(migration.sql)
                    except Exception as exc:  # pragma: no cover - 依赖真实数据库
                        raise MigrationError(
                            f"迁移 {migration.version}（{migration.path.name}）执行失败：{exc}"
                        ) from exc
                    conn.execute(
                        f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version, checksum) "
                        "VALUES (%s, %s)",
                        (migration.version, migration.checksum),
                    )
                    applied_now.append(migration.version)

        return applied_now

    def assert_ready(self) -> dict[str, Any]:
        """启动自检：扩展、扩展版本、六张表、已应用迁移。

        计划 §阶段 4 要求"pgvector 扩展缺失视为部署错误"——这里把它提前到启动期，
        而不是等到第一次向量检索才炸。
        """

        pool = self._require_pool()
        with pool.connection() as conn:
            server = conn.execute("SHOW server_version").fetchone()["server_version"]
            extension = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            if not extension:
                raise MemoryDatabaseError(
                    "记忆库缺少 pgvector 扩展。请在该库执行：CREATE EXTENSION vector;"
                    "（本机 Docker 请使用 pgvector/pgvector 镜像，"
                    "远程原生安装见 doc/15_memory_postgres_deployment.md）"
                )
            missing = [
                name
                for name in REQUIRED_TABLES
                if not (conn.execute("SELECT to_regclass(%s) AS oid", (name,)).fetchone()["oid"])
            ]
            if missing:
                raise MemoryDatabaseError(
                    "记忆库 schema 不完整，缺少表：" + ", ".join(missing)
                    + "。请先运行迁移：MemoryDatabase.run_migrations()"
                )
        return {
            "server_version": server,
            "pgvector_version": extension["extversion"],
            "applied_migrations": sorted(self.applied_migrations()),
            "dsn": self.masked_dsn,
        }


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _bind_tenant(conn: Any, tenant: Tenant) -> None:
    """在当前事务里写入租户上下文。

    用参数化的 ``set_config`` 而不是拼 ``SET`` 语句：``user_id`` 来自用户输入，
    字符串拼接会直接变成 SQL 注入点。
    """

    _assert_in_transaction(conn)
    conn.execute("SELECT set_config('app.user_id', %s, true)", (tenant.user_id,))
    # project_id 为空时写空串：policy 里是相等比较，空串不会匹配任何真实项目，
    # 因此非项目作用域的请求不会误拿到别的项目记忆。
    conn.execute(
        "SELECT set_config('app.project_id', %s, true)", (tenant.project_id or "",)
    )


def _assert_in_transaction(conn: Any) -> None:
    """确认连接处于事务中。

    ``set_config(..., is_local=true)`` 在事务外设置的值会随该语句所在隐式事务
    立即失效，等价于没绑定。与其静默不生效，不如直接报错。
    """

    status = conn.info.transaction_status
    psycopg, _ = load_psycopg()
    if status == psycopg.pq.TransactionStatus.IDLE:
        raise MemoryDatabaseError(
            "租户绑定必须在事务内进行：set_config(is_local=true) 在事务外会立即失效"
        )


@contextmanager
def temporary_database(
    admin_dsn: str, *, prefix: str = "klonet_memory_test"
) -> Iterator[str]:
    """建一个临时库、交出它的 DSN、退出时删掉。

    只给集成测试用。``CREATE DATABASE`` 不能在事务里跑，所以管理连接必须是
    autocommit；删除时用 ``WITH (FORCE)`` 踢掉残留连接（PostgreSQL 13+）。
    """

    import uuid

    psycopg, _ = load_psycopg()
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"{prefix}_{uuid.uuid4().hex[:12]}"

    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5.0) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    params = conninfo_to_dict(admin_dsn)
    params["dbname"] = name
    test_dsn = make_conninfo(**params)
    try:
        yield test_dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5.0) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
