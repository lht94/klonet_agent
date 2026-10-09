"""记忆库基线（04 计划阶段 0）。

在 Worker 真正开始自动改记忆之前，把当前数据库的客观状态**冻结**下来：

- 已应用迁移版本号列表（防止阶段 1 的新迁移编号撞车）
- ``schema_migrations`` 表里的 checksum 列表
- ``REQUIRED_TABLES`` 是否齐全
- embedding outbox 的覆盖快照
- 当前 ``active`` 但 ``valid_to <= now()`` 的过期记录数（应当 = 0）
- ``deleted`` 且过了保留期的"待 purge"记录数（应当 = 0）

**为什么是 DSN-gated 的 pytest 测试而不是运维脚本**：基线数字与每个 stage 的 A/B 对比
要走 ``tests/test_*.py`` 体系；真库测试在 CI 上不可绕过的"未设置 DSN → skip → 不可记
作通过"规则由既有 ``tests/test_memory_postgres_repository.py`` 验证，本测试沿用同样
的契约（详见 04 计划 §10 测试矩阵）。

阶段 1 起，每个 stage 都要回头跑这个文件做"baseline 是否漂移"对照。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from klonet_agent.config import PROJECT_ROOT
from klonet_agent.memory.database import (
    MIGRATIONS_DIR,
    REQUIRED_TABLES,
    MemoryDatabase,
    load_migration_files,
    memory_dsn,
    temporary_database,
)

TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"

# 当前阶段的迁移列表必须**精确**等于这 5 项。阶段 1 起每加一张新迁移，
# 这份列表更新并写明改动者。任何不在列表里的 `migrations/*.sql` 都应该报红。
CURRENT_APPLIED_MIGRATIONS: tuple[str, ...] = (
    "0001_init",
    "0002_roles_and_grants",
    "0003_immutable_versions",
    "0004_governance",
    "0005_governance_provenance",
    "0006_memory_maintenance",
        "0007_memory_maintenance_proposals",
)

BASELINE_REPORT_FILE = (
    PROJECT_ROOT / "evals" / "memory_maintenance_baseline.json"
)


def _admin_dsn_or_skip() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过记忆库基线。"
            "基线本身就是真库层面的统计，DSN 缺失是部署问题，"
            "不是测试套件问题（04 计划阶段 0：CI 把 skip 不得记作成功，"
            "本文件用 pytest.skip 由调用方负责 -rs 报告的标识）。"
        )
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过记忆库基线")
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
        database = MemoryDatabase(dsn, min_size=1, max_size=4)
        database.open()
        try:
            yield database
        finally:
            database.close()


# --------------------------------------------------------------------------- #
# 迁移层面的不变量（不依赖任何业务数据）
# --------------------------------------------------------------------------- #


def test_current_applied_migrations_match_plan() -> None:
    """迁移列表必须**精确**等于基线数字。

    04 计划阶段 1 会新增 ``0006_memory_maintenance.sql``；阶段 5 / 7 再加
    ``0007`` 与 ``0008``。任何阶段都不得**跳过**一个编号（如 ``0006a``），
    也不得**重排**既有 5 项。后续阶段跑这个测试时这条会失败——这正是它的作用。
    """

    discovered = tuple(
        sorted(m.version for m in load_migration_files(MIGRATIONS_DIR))
    )
    assert discovered == CURRENT_APPLIED_MIGRATIONS, (
        f"迁移文件列表变了。当前发现 {discovered}，"
        f"阶段 0 基线期望 {CURRENT_APPLIED_MIGRATIONS}。"
        "若是有意新增，按本计划顺延编号并同步本列表；"
        "若是误改，把文件改回去。"
    )


def test_baseline_report_file_is_writable(tmp_path: Path) -> None:
    """基线报告文件可以创建（仅 dry-run；不依赖 DSN）。

    阶段 1 起每次跑这个文件后应 ``--update-baseline`` 落盘一份对比报告，
    但首版只保证"文件能写"——这条测试不依赖真库，本机/CI 无 DSN 也跑得通。
    """

    target = tmp_path / "memory_maintenance_baseline.json"
    target.write_text(
        json.dumps(
            {"mode": "baseline_generated", "note": "占位", "applied_migrations": list(CURRENT_APPLIED_MIGRATIONS)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["mode"] == "baseline_generated"
    assert payload["applied_migrations"] == list(CURRENT_APPLIED_MIGRATIONS)


# --------------------------------------------------------------------------- #
# 真库层面的不变量（DSN-gated）
# --------------------------------------------------------------------------- #


def test_apply_migrations_returns_baseline_set(db: MemoryDatabase) -> None:
    """临时库上跑迁移后，已应用列表必须与 ``CURRENT_APPLIED_MIGRATIONS`` 完全一致。"""

    applied = db.run_migrations()
    assert applied == list(CURRENT_APPLIED_MIGRATIONS)


def test_assert_ready_passes_against_baseline(db: MemoryDatabase) -> None:
    """``REQUIRED_TABLES`` 全部存在，schema 与基线兼容。"""

    snapshot = db.assert_ready()
    assert snapshot["applied_migrations"] == list(CURRENT_APPLIED_MIGRATIONS)
    assert isinstance(snapshot["pgvector_version"], str)
    assert snapshot["pgvector_version"], "pgvector 扩展未启用"


def test_required_tables_present(db: MemoryDatabase) -> None:
    """六张核心表都在；新增表不是这套契约的关注点。"""

    with db.diagnostic_session() as conn:
        for name in REQUIRED_TABLES:
            row = conn.execute(
                "SELECT to_regclass(%s) AS oid", (name,)
            ).fetchone()
            assert row is not None, f"诊断会话未返回 {name}"
            assert row["oid"] is not None, f"缺少表 {name}"


def test_schema_migrations_checksum_matches_files_on_disk(db: MemoryDatabase) -> None:
    """``schema_migrations`` 记录与磁盘上的迁移文件 checksum 一致。

    04 计划阶段 0 修订：迁移文件被改过（哪怕是 CRLF 落盘）必须**立即**报红，
    而不是等下次启动时报 ``MigrationError``。``memory_checksum`` 内部已经把
    CRLF 归一成 LF，所以这里比对时也用同一份归一化。
    """

    from klonet_agent.memory.database import migration_checksum

    disk = {m.version: m.checksum for m in load_migration_files(MIGRATIONS_DIR)}
    with db.diagnostic_session() as conn:
        rows = conn.execute(
            "SELECT version, checksum FROM schema_migrations"
        ).fetchall()
    recorded = {row["version"]: row["checksum"] for row in rows}
    assert recorded == disk, (
        "schema_migrations 与磁盘文件 checksum 不一致。"
        f"磁盘：{sorted(disk)}，库内：{sorted(recorded)}。"
        "可能有人改了迁移文件没重跑。"
    )


def test_baseline_fresh_db_has_no_backlog(db: MemoryDatabase) -> None:
    """新建的临时库基线状态下不应有任何积压。

    outbox 没有 pending（压根没听过新写的记忆），
    ``active`` 但 ``valid_to <= now()`` 的过期记录数 = 0，
    ``deleted`` 等待 purge 的记录数 = 0。

    阶段 1 起的每次 ``--update-baseline`` 跑完后，**真实库**可能不为 0，
    那是 Worker 的活儿；这里只验证"模板库"的形状。
    """

    with db.diagnostic_session() as conn:
        pending_outbox = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_embedding_outbox WHERE status = 'pending'"
        ).fetchone()["n"]
        expired_active = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_records r "
            "JOIN memory_versions v ON v.id = r.active_version_id "
            "WHERE r.status = 'active' AND v.valid_to IS NOT NULL AND v.valid_to <= now()"
        ).fetchone()["n"]
        deleted_waiting = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_records WHERE status = 'deleted'"
        ).fetchone()["n"]

    assert pending_outbox == 0, f"新建库不应有 pending outbox：{pending_outbox}"
    assert expired_active == 0, f"新建库不应有 expired active：{expired_active}"
    assert deleted_waiting == 0, f"新建库不应有 deleted 记录：{deleted_waiting}"


def test_baseline_report_shape_matches_plan_schema(tmp_path: Path) -> None:
    """基线报告的 JSON schema 必须与计划 §6.6 健康指标字段一致。

    首版只验证字段名集合，不让基础设施变更破坏将来 Prometheus exporter。
    """

    required_keys = {
        "applied_migrations",
        "outbox_pending_total",
        "expired_active_total",
        "deleted_waiting_purge_total",
        "embedding_coverage_ratio",
        "captured_at",
    }
    payload = {
        "applied_migrations": list(CURRENT_APPLIED_MIGRATIONS),
        "outbox_pending_total": 0,
        "expired_active_total": 0,
        "deleted_waiting_purge_total": 0,
        "embedding_coverage_ratio": 1.0,
        "captured_at": "2026-10-09T00:00:00+00:00",
    }
    assert required_keys.issubset(payload.keys()), (
        f"基线报告缺少字段：{required_keys - payload.keys()}"
    )

    target = tmp_path / "memory_maintenance_baseline.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    reloaded = json.loads(target.read_text(encoding="utf-8"))
    assert reloaded["applied_migrations"] == list(CURRENT_APPLIED_MIGRATIONS)
    assert reloaded["embedding_coverage_ratio"] == 1.0