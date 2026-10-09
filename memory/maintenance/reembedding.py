"""Embedding 模型迁移（04 计划 §6.5 / 阶段 6）的管理层。

三个不可谈判的约束：

1. **迁移必须显式创建**：没有任何路径允许"运维改 SQL 跳过迁移直接写向量"。
   写非 default profile 的向量之前，账本里必须有一条非终态迁移。
2. **状态机落库**（``migrations/0008`` 的触发器）：本模块只做"顺序合法的
   状态推进 + 门禁校验"，跳变会被数据库拒绝（``2F002``）。
3. **default profile 的读写路径一字不动**：``memory_versions.embedding``
   仍是 default 的存储（02 语义零回归）；新 profile 落
   ``public.memory_embeddings``（0008），检索按 active profile 路由。
   原子切换 = 单例行 ``memory_maintenance.embedding_active_profile`` 的
   一条 UPDATE（与迁移状态同事务）。

维度变化**必须**新建 profile + 新表（禁止 ``ALTER COLUMN vector(N)``）；
1024 维迁移用 ``memory_embeddings``，换维度的迁移由新迁移建新表，本模块
在 ``create`` 时校验目标表存在且向量列维度匹配。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "EmbeddingMigration",
    "EmbeddingMigrationError",
    "EmbeddingMigrationGateError",
    "EmbeddingMigrationStateError",
    "EmbeddingMigrationManager",
    "ValidationReport",
    "TERMINAL_STATUSES",
]

#: 终态：不能再转出（触发器同样拒绝）。
TERMINAL_STATUSES = frozenset({"retired", "cancelled"})

#: 迁移未通过门禁时的默认门槛（§6.5 验收：coverage ≥ 99% 且 eval 达标）。
DEFAULT_MIN_COVERAGE = 0.99
DEFAULT_MIN_RECALL = 0.70

ValidateSearchFn = Callable[[str, str], Sequence[Any]]
"""``(query_text, profile_id) -> hits``。hits 元素带 ``record.id``（MemoryHit）。"""


class EmbeddingMigrationError(RuntimeError):
    """迁移操作失败（表缺失、维度不匹配等部署级问题）。"""


class EmbeddingMigrationStateError(EmbeddingMigrationError):
    """非法状态转换或对不存在的迁移操作。"""


class EmbeddingMigrationGateError(EmbeddingMigrationError):
    """门禁不达标（coverage / recall 不足），拒绝 promote。"""


@dataclass(frozen=True)
class EmbeddingMigration:
    """一条 embedding 迁移的账本行。"""

    migration_id: str
    status: str
    source_profile_id: str
    target_profile_id: str
    target_model: str
    target_model_version: str
    target_dimensions: int
    target_table: str
    cursor: Mapping[str, Any] | None = None
    stats: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def live(self) -> bool:
        return self.status not in TERMINAL_STATUSES


@dataclass(frozen=True)
class ValidationReport:
    """一次 validation 的结果（原样写入迁移账本的 ``stats``）。"""

    migration_id: str
    coverage: float
    eligible: int
    completed: int
    recall_overlap: float
    probes: int
    k: int
    latency_ms: Mapping[str, float]
    min_coverage: float
    min_recall: float
    passed: bool
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "coverage": round(self.coverage, 6),
            "eligible": self.eligible,
            "completed": self.completed,
            "recall_overlap": round(self.recall_overlap, 6),
            "probes": self.probes,
            "k": self.k,
            "latency_ms": {key: round(value, 2) for key, value in self.latency_ms.items()},
            "min_coverage": self.min_coverage,
            "min_recall": self.min_recall,
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


class EmbeddingMigrationManager:
    """迁移账本的生命周期操作。跨租户（走 ``diagnostic_session``），与
    ``MaintenanceRepository`` 同一连接纪律：**只经池、每操作一事务**。
    """

    def __init__(
        self,
        database: Any,
        *,
        source_profile_id: str = "default",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._source_profile_id = str(source_profile_id)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------ 查询 ----

    def get(self, migration_id: str) -> EmbeddingMigration | None:
        pool = self._database._require_pool()
        with pool.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_maintenance.embedding_migrations
                 WHERE migration_id = %s
                """,
                [migration_id],
            ).fetchone()
        return _migration_from_row(row) if row is not None else None

    def get_live(self) -> EmbeddingMigration | None:
        pool = self._database._require_pool()
        with pool.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM memory_maintenance.embedding_migrations
                 WHERE status NOT IN ('retired', 'cancelled')
                 ORDER BY created_at DESC
                 LIMIT 1
                """
            ).fetchone()
        return _migration_from_row(row) if row is not None else None

    def list(self) -> list[EmbeddingMigration]:
        pool = self._database._require_pool()
        with pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_maintenance.embedding_migrations
                 ORDER BY created_at DESC
                """
            ).fetchall()
        return [_migration_from_row(row) for row in rows]

    def active_profile(self) -> dict[str, Any]:
        """当前生效 profile（检索路由的权威来源）。"""
        pool = self._database._require_pool()
        with pool.connection() as conn:
            row = conn.execute(
                """
                SELECT profile_id, model, dimensions
                  FROM memory_maintenance.embedding_active_profile
                 WHERE id = 1
                """
            ).fetchone()
        if row is None:  # pragma: no cover - 0008 已播种单例行
            raise EmbeddingMigrationError("embedding_active_profile 单例行缺失，检查 0008 迁移")
        return {
            "profile_id": str(row["profile_id"]),
            "model": str(row["model"]),
            "dimensions": int(row["dimensions"]),
        }

    # ------------------------------------------------------------ 生命周期 ----

    def create(
        self,
        *,
        migration_id: str,
        target_profile_id: str,
        target_model: str,
        target_model_version: str,
        target_dimensions: int = 1024,
        target_table: str = "memory_embeddings",
        source_profile_id: str | None = None,
    ) -> EmbeddingMigration:
        """显式创建迁移（status='planned'）。同一时刻只允许一个非终态迁移。"""

        migration_id = str(migration_id or "").strip()
        target_profile_id = str(target_profile_id or "").strip()
        source = str(source_profile_id or self._source_profile_id).strip()
        if not migration_id:
            raise EmbeddingMigrationError("migration_id 不能为空")
        if not target_profile_id:
            raise EmbeddingMigrationError("target_profile_id 不能为空")
        if target_profile_id == source:
            raise EmbeddingMigrationError(
                f"target_profile_id 与 source_profile_id 相同（{source}），没有可迁移的东西"
            )
        if int(target_dimensions) <= 0:
            raise EmbeddingMigrationError("target_dimensions 必须为正数")
        if not str(target_model or "").strip() or not str(target_model_version or "").strip():
            raise EmbeddingMigrationError("target_model 与 target_model_version 必须给出")

        self._verify_target_table(target_table, int(target_dimensions))
        if self.get_live() is not None:
            raise EmbeddingMigrationStateError(
                "已存在一个未终态的迁移；先让它走完或 cancel，再创建新的"
                "（两条迁移并发回填同一批版本会互相踩踏）"
            )

        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO memory_maintenance.embedding_migrations
                           (migration_id, status, source_profile_id, target_profile_id,
                            target_model, target_model_version, target_dimensions,
                            target_table)
                    VALUES (%s, 'planned', %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        migration_id,
                        source,
                        target_profile_id,
                        str(target_model),
                        str(target_model_version),
                        int(target_dimensions),
                        str(target_table),
                    ],
                )
        created = self.get(migration_id)
        if created is None:  # pragma: no cover - 插入后立即可读
            raise EmbeddingMigrationStateError(f"迁移 {migration_id} 创建后读不到")
        return created

    def start(self, migration_id: str) -> EmbeddingMigration:
        """planned/failed/paused → backfilling，并播种回填 outbox 行。"""

        current = self._require(migration_id)
        if current.status not in ("planned", "failed", "paused"):
            raise EmbeddingMigrationStateError(
                f"start 只对 planned/failed/paused 合法，当前 {current.status}"
            )
        seeded = self._seed_backfill_outbox(current)
        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET status = 'backfilling',
                           cursor = COALESCE(cursor, '{}'::jsonb),
                           started_at = COALESCE(started_at, now())
                     WHERE migration_id = %s
                    """,
                    [migration_id],
                )
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET stats = stats || %s::jsonb
                     WHERE migration_id = %s
                    """,
                    [_json({"last_seed_count": seeded}), migration_id],
                )
        return self._require(migration_id)

    def pause(self, migration_id: str) -> EmbeddingMigration:
        return self._transition(migration_id, "paused", allowed_from=("backfilling",))

    def resume(self, migration_id: str) -> EmbeddingMigration:
        """paused → backfilling（cursor 保留，outbox 里的 pending 行继续被领）。"""

        return self._transition(migration_id, "backfilling", allowed_from=("paused",))

    def mark_validating(self, migration_id: str) -> EmbeddingMigration:
        """backfilling → validating（回填队列清空后的自然推进；
        :class:`ReembeddingJob` 在 outbox 清空时调用）。"""

        return self._transition(migration_id, "validating", allowed_from=("backfilling",))

    def retry(self, migration_id: str) -> EmbeddingMigration:
        """failed → backfilling（失败重启从 outbox/cursor 续跑，不重复已成功条目）。"""

        return self._transition(migration_id, "backfilling", allowed_from=("failed",))

    def cancel(self, migration_id: str) -> EmbeddingMigration:
        migration = self._require(migration_id)
        if migration.status in TERMINAL_STATUSES:
            raise EmbeddingMigrationStateError(
                f"迁移 {migration_id} 已是终态（{migration.status}），不能 cancel"
            )
        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET status = 'cancelled', cursor = NULL
                     WHERE migration_id = %s
                    """,
                    [migration_id],
                )
                # 取消即撤掉未完成的目标 profile 队列行；已完成的向量留着
                #（下一步操作者可以 DELETE，或留给运维脚本清理）。
                conn.execute(
                    """
                    DELETE FROM memory_embedding_outbox o
                     USING memory_maintenance.embedding_migrations m
                     WHERE m.migration_id = %s
                       AND o.embedding_profile_id = m.target_profile_id
                       AND o.status IN ('pending', 'processing')
                    """,
                    [migration_id],
                )
        return self._require(migration_id)

    # ------------------------------------------------------- 回填 / 校验 ----

    def seed_backfill(self, migration_id: str) -> int:
        """（重新）播种回填 outbox 行，返回本次新插入的行数。

        独立暴露是为了 ReembeddingJob 在 backfilling 中周期性补种——迁移开始
        之后**新写入的** active 版本也要进队列。幂等：completed 行不重播。
        """

        return self._seed_backfill_outbox(self._require(migration_id))

    def eligible_count(self, migration_id: str) -> int:
        """当前应被回填的版本数（active 记录的当前版本、未失效）。"""

        migration = self._require(migration_id)
        pool = self._database._require_pool()
        with pool.connection() as conn:
            row = conn.execute(
                _ELIGIBLE_COUNT_SQL,
                [migration.target_profile_id],
            ).fetchone()
        return int(row["eligible"]) if row is not None else 0

    def backfill_progress(self, migration_id: str) -> tuple[int, int]:
        """（completed, eligible）——coverage 的分子与分母。"""

        migration = self._require(migration_id)
        pool = self._database._require_pool()
        # 注意：CTE 用 f-string 内联（常量、不含占位符），参数只有 profile 一个。
        # 不要用 "% body" 的写法——它会把 CTE 的占位符一起吞掉（真踩过：
        # "query has 1 placeholders but 2 parameters were passed"）。
        sql = f"""
        WITH eligible AS ({_ELIGIBLE_CTE_SQL_BODY})
        SELECT
            (SELECT count(*) FROM eligible) AS eligible,
            (SELECT count(*) FROM memory_embedding_outbox o
               JOIN eligible e ON e.memory_version_id = o.memory_version_id
              WHERE o.embedding_profile_id = %s
                AND o.status = 'completed') AS completed
        """
        with pool.connection() as conn:
            row = conn.execute(sql, [migration.target_profile_id]).fetchone()
        if row is None:  # pragma: no cover
            return (0, 0)
        return (int(row["completed"]), int(row["eligible"]))

    def validate(
        self,
        migration_id: str,
        *,
        search_fn: ValidateSearchFn,
        probes: Sequence[str],
        k: int = 10,
        min_coverage: float = DEFAULT_MIN_COVERAGE,
        min_recall: float = DEFAULT_MIN_RECALL,
    ) -> ValidationReport:
        """shadow 对比 + coverage，写 stats；达标则 validating → ready。

        ``search_fn(query_text, profile_id)`` 由调用方提供（通常包一层
        ``repository.search(query, embedding_profile_id=...)``），manager 只
        负责度量与判定——这样门禁逻辑可以离线重放，不绑检索实现。
        """

        migration = self._require(migration_id)
        if migration.status != "validating":
            raise EmbeddingMigrationStateError(
                f"validate 只对 validating 合法，当前 {migration.status}"
            )
        probes = [str(text).strip() for text in probes if str(text).strip()]
        if not probes:
            raise EmbeddingMigrationError("validate 需要至少一条探针查询")

        completed, eligible = self.backfill_progress(migration_id)
        coverage = (completed / eligible) if eligible else 0.0

        source_ids: list[set[str]] = []
        target_ids: list[set[str]] = []
        latency: dict[str, list[float]] = {"source": [], "target": []}
        for text in probes:
            for role, profile in (
                ("source", migration.source_profile_id),
                ("target", migration.target_profile_id),
            ):
                begin = time.perf_counter()
                hits = list(search_fn(text, profile))
                latency[role].append((time.perf_counter() - begin) * 1000.0)
                ids = set()
                for hit in hits[:k]:
                    record = getattr(hit, "record", None)
                    hit_id = getattr(record, "id", None) or getattr(hit, "id", None)
                    if hit_id is not None:
                        ids.add(str(hit_id))
                (source_ids if role == "source" else target_ids).append(ids)

        overlaps = [
            (len(a & b) / len(a)) if a else 0.0
            for a, b in zip(source_ids, target_ids)
        ]
        recall_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0

        reasons: list[str] = []
        if eligible == 0:
            reasons.append("eligible=0：没有可回填的版本，先确认库里确有 active 记忆")
        if coverage < min_coverage:
            reasons.append(
                f"coverage {coverage:.4f} < 门槛 {min_coverage:.4f}"
            )
        if recall_overlap < min_recall:
            reasons.append(
                f"recall@{k} overlap {recall_overlap:.4f} < 门槛 {min_recall:.4f}"
            )
        passed = not reasons

        report = ValidationReport(
            migration_id=migration_id,
            coverage=coverage,
            eligible=eligible,
            completed=completed,
            recall_overlap=recall_overlap,
            probes=len(probes),
            k=k,
            latency_ms={
                "source": sum(latency["source"]) / len(latency["source"]),
                "target": sum(latency["target"]) / len(latency["target"]),
            },
            min_coverage=min_coverage,
            min_recall=min_recall,
            passed=passed,
            reasons=tuple(reasons),
        )

        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET stats = stats || %s::jsonb,
                           status = CASE WHEN %s THEN 'ready' ELSE status END
                     WHERE migration_id = %s
                    """,
                    [_json(report.to_json()), passed, migration_id],
                )
        return report

    def promote(self, migration_id: str) -> EmbeddingMigration:
        """ready → active，并**原子**改写单例 active profile（同一事务）。"""

        migration = self._require(migration_id)
        if migration.status != "ready":
            raise EmbeddingMigrationStateError(
                f"promote 只对 ready 合法，当前 {migration.status}"
            )
        stats = dict(migration.stats)
        if not stats.get("passed"):
            raise EmbeddingMigrationGateError(
                "promote 被门禁拒绝：必须先 validate 且 passed=true"
                f"（当前 stats.passed={stats.get('passed')!r}）"
            )
        coverage = float(stats.get("coverage", 0.0))
        if coverage + 1e-9 < float(
            stats.get("min_coverage", DEFAULT_MIN_COVERAGE)
        ):
            raise EmbeddingMigrationGateError(
                f"promote 被门禁拒绝：coverage {coverage:.4f} 不足"
            )
        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET status = 'active'
                     WHERE migration_id = %s
                    """,
                    [migration_id],
                )
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_active_profile
                       SET profile_id = %s, model = %s, dimensions = %s,
                           updated_at = now()
                     WHERE id = 1
                    """,
                    [
                        migration.target_profile_id,
                        migration.target_model,
                        migration.target_dimensions,
                    ],
                )
        return self._require(migration_id)

    def rollback(self, migration_id: str) -> EmbeddingMigration:
        """active → retiring → retired，单例 active profile 切回 source。

        回滚是一个操作内完成的（§6.5 验收：切换后可在一个操作内回滚）：
        一个事务里连推两个状态 + 改单例。旧 profile 向量保留（回滚窗口），
        清理由后续 purge 决定，这里绝不删数据。
        """

        migration = self._require(migration_id)
        if migration.status != "active":
            raise EmbeddingMigrationStateError(
                f"rollback 只对 active 合法，当前 {migration.status}"
            )
        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET status = 'retiring'
                     WHERE migration_id = %s
                    """,
                    [migration_id],
                )
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET status = 'retired'
                     WHERE migration_id = %s
                    """,
                    [migration_id],
                )
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_active_profile
                       SET profile_id = %s, model = %s, dimensions = %s,
                           updated_at = now()
                     WHERE id = 1
                    """,
                    [
                        migration.source_profile_id,
                        "legacy",
                        1024,
                    ],
                )
        return self._require(migration_id)

    # ------------------------------------------------------------ 内部 ----

    def _require(self, migration_id: str) -> EmbeddingMigration:
        migration = self.get(migration_id)
        if migration is None:
            raise EmbeddingMigrationStateError(f"迁移 {migration_id} 不存在")
        return migration

    def _transition(
        self, migration_id: str, to_status: str, *, allowed_from: tuple[str, ...]
    ) -> EmbeddingMigration:
        current = self._require(migration_id)
        if current.status not in allowed_from:
            raise EmbeddingMigrationStateError(
                f"{to_status} 只对 {'/'.join(allowed_from)} 合法，当前 {current.status}"
            )
        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE memory_maintenance.embedding_migrations
                       SET status = %s
                     WHERE migration_id = %s
                    """,
                    [to_status, migration_id],
                )
        return self._require(migration_id)

    def _seed_backfill_outbox(self, migration: EmbeddingMigration) -> int:
        """把"该被回填"的版本插进 outbox（target profile），幂等。

        计划 §6.5 的排除条件：deleted 与已归档一律跳过——active 记录的
        当前版本且 ``valid_to IS NULL`` 才入队（``valid_to <= now()`` 的
        已失效版本被 ``IS NULL`` 天然排除；as_of 历史回放不属于回填范围）。
        """

        pool = self._database._require_pool()
        with pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    _SEED_BACKFILL_SQL,
                    [migration.target_profile_id, migration.target_profile_id],
                ).fetchone()
        return int(row["seeded"]) if row is not None else 0

    def _verify_target_table(self, table: str, dimensions: int) -> None:
        """目标向量表必须存在且向量列维度匹配（禁止改列维度，见文件头）。"""

        pool = self._database._require_pool()
        with pool.connection() as conn:
            row = conn.execute(
                """
                SELECT a.atttypmod AS typmod
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a
                    ON a.attrelid = c.oid AND a.attname = 'embedding'
                 WHERE n.nspname = 'public' AND c.relname = %s
                """,
                [table],
            ).fetchone()
        if row is None:
            raise EmbeddingMigrationError(
                f"目标向量表 public.{table} 不存在——维度变化必须由迁移新建表，"
                "禁止 ALTER COLUMN vector(N)"
            )
        # vector 列的 atttypmod = 维度；-1 表示未声明（不安全，拒绝）。
        typmod = int(row["typmod"] or -1)
        if typmod != int(dimensions):
            raise EmbeddingMigrationError(
                f"public.{table}.embedding 维度 {typmod} 与迁移目标 {dimensions} 不一致"
            )


# --------------------------------------------------------------------------- #
# SQL 片段（eligible 集合的两处复用必须同一口径）
# --------------------------------------------------------------------------- #

_ELIGIBLE_CTE_SQL_BODY = """
    SELECT v.id AS memory_version_id
      FROM memory_versions v
      JOIN memory_records r ON r.id = v.memory_id
     WHERE r.status = 'active'
       AND r.active_version_id = v.id
       AND v.valid_to IS NULL
"""

_ELIGIBLE_COUNT_SQL = (
    "SELECT count(*) AS eligible FROM (" + _ELIGIBLE_CTE_SQL_BODY + ") e"
)

_SEED_BACKFILL_SQL = f"""
    WITH eligible AS ({_ELIGIBLE_CTE_SQL_BODY})
    INSERT INTO memory_embedding_outbox
           (memory_version_id, embedding_profile_id)
    SELECT e.memory_version_id, %s
      FROM eligible e
     WHERE NOT EXISTS (
           SELECT 1 FROM memory_embedding_outbox o
            WHERE o.memory_version_id = e.memory_version_id
              AND o.embedding_profile_id = %s
              AND o.status = 'completed')
    ON CONFLICT (memory_version_id, embedding_profile_id) DO NOTHING
    RETURNING 1 AS seeded
"""


# --------------------------------------------------------------------------- #
# 行映射 / JSON 小工具
# --------------------------------------------------------------------------- #


def _migration_from_row(row: Any) -> EmbeddingMigration:
    return EmbeddingMigration(
        migration_id=str(row["migration_id"]),
        status=str(row["status"]),
        source_profile_id=str(row["source_profile_id"]),
        target_profile_id=str(row["target_profile_id"]),
        target_model=str(row["target_model"]),
        target_model_version=str(row["target_model_version"]),
        target_dimensions=int(row["target_dimensions"]),
        target_table=str(row["target_table"]),
        cursor=row["cursor"] if isinstance(row["cursor"], dict) else None,
        stats=row["stats"] if isinstance(row["stats"], dict) else {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _json(payload: Mapping[str, Any]) -> str:
    import json

    return json.dumps(dict(payload), ensure_ascii=False)
