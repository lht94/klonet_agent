"""PostgreSQL 记忆仓库实现。

设计取舍（对应计划 §7 与 §12）：

- **原始 SQL，不引 ORM**。检索要同时用 tsvector、pgvector 的 ``<=>`` 和部分索引，
  这些在 ORM 里都要退回 ``text()``，还不如直接写 SQL 可读。
- **租户过滤写在每一条 SQL 里，不只靠 RLS**。计划 §5.3 的要求是"应用层过滤之外
  再启用 RLS"。只靠 RLS 的话，任何一次用超级用户或带 ``BYPASSRLS`` 的维护角色跑
  在线查询都会静默跨用户；只靠应用层的话，一次漏写 where 就全泄露。两者都要。
- **仓库对象构造时就绑定 ``Tenant``**，没有"忘记传 user_id"的方法签名可以调用。
- **写操作的冲突翻译成领域异常**。唯一索引冲突不向上抛 `psycopg` 的
  `UniqueViolation`——调用方需要区分"内容重复（应当 NOOP）"和"同一 subject 已有
  active（必须走 SUPERSEDE）"，这是两种不同的决策。
- **可能冲突的语句跑在 savepoint 里**。PostgreSQL 事务一旦出错就 aborted，必须
  回滚到 savepoint 才能继续查（例如冲突后去查已有记录的 id）。

检索的三通道权重与 ``knowledge/multi_stage.py`` 保持一致（lexical 1.0 /
semantic 1.0 / exact 2.0，RRF k=60，缩放 1.3），避免同一个仓库里有两套融合口径。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from klonet_agent.memory.database import MemoryDatabase, load_psycopg
from klonet_agent.memory.domain import (
    MemoryCandidate,
    MemoryDomainError,
    MemoryHit,
    MemoryQuery,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryVersion,
    RelationType,
    Scope,
    SourceType,
    Tenant,
    WriteDecision,
    content_hash,
    ensure_status_transition,
    ensure_verification_supported,
    normalize_content,
    type_allows_supersede,
    validate_validity_window,
)
from klonet_agent.memory.repository import (
    DEFAULT_EMBEDDING_PROFILE_ID,
    EMBEDDING_DIMENSIONS,
    ActiveSubjectConflictError,
    CandidateNotFoundError,
    CandidateRecord,
    DuplicateVersionError,
    EmbeddingOutboxStats,
    MemoryRepositoryError,
    NewRecordCommand,
    NewVersionCommand,
    PendingEmbedding,
    RecordNotFoundError,
    RecordNotActiveError,
    ScopeViolationError,
)

__all__ = [
    "DEFAULT_EMBEDDING_PROFILE_ID",
    "EMBEDDING_DIMENSIONS",
    "PostgresMemoryRepository",
    "default_lexical_text",
]


# 阶段 1 只允许一个 active embedding profile，名字固定；维度对应 schema 的
# ``vector(1024)``。两个常量都定义在 ``memory/repository.py``（契约侧），
# 这里 import 后再导出，避免同一件事在契约与实现里各写一遍、改一处漏一处。

# 与 knowledge/multi_stage.py 对齐的融合参数。
_RRF_K = 60
_RRF_SCALE = 1.3
_CHANNEL_WEIGHTS: Mapping[str, float] = {
    "lexical": 1.0,
    "semantic": 1.0,
    "exact": 2.0,
}

# 精确匹配通道关心的 token：路径、版本号、点分/下划线标识符。
# 这类 token 会被分词器切碎，走全文通道会漏。
# 路径段用 `*` 而不是 `+`：`/VEMU2/` 这种单段路径只有一个段，
# 用 `+` 会要求至少两段，正好把它漏掉。
_EXACT_TOKEN_RE = re.compile(
    r"/[A-Za-z0-9_<>.-]+(?:/[A-Za-z0-9_<>.-]+)*/?"
    r"|v?\d+(?:\.\d+){1,3}"
    r"|[A-Za-z_][A-Za-z0-9_]*[._-][A-Za-z0-9_.-]*"
)

# 单通道候选池大小：最终按 limit 截断，先多取一些给融合留空间。
_CANDIDATE_POOL_FACTOR = 4
_CANDIDATE_POOL_MIN = 20

# ``last_error`` 只用于诊断，不承担"完整日志"的职责：整篇 traceback 会把
# outbox 表撑大，而且错误串本身可能带上调用方没预料到的敏感内容。
_MAX_OUTBOX_ERROR_LENGTH = 2000


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_uuid(value: str | UUID | None, field: str) -> UUID | None:
    """把外部传来的 id 转成 UUID，非法时给出可读错误。"""

    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise MemoryDomainError(f"{field} 不是合法 UUID: {value!r}") from exc


def _check_confidence(value: float) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise MemoryDomainError(f"confidence 必须落在 [0, 1]: {value!r}")
    return number


def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _vector_literal(values: Sequence[float]) -> str:
    """把向量转成 pgvector 的字面量 ``[1,2,3]``。"""

    return "[" + ",".join(repr(float(item)) for item in values) + "]"


def _parse_vector(value: Any) -> tuple[float, ...] | None:
    """把驱动返回的向量解析成 tuple。

    装了 ``pgvector`` 的 Python 包时返回带 ``to_list()`` 的对象，没装时驱动直接
    给 ``'[1,2,3]'`` 字符串。两种情况都要能吃下，否则"是否装了可选包"会变成一个
    隐性行为差异。
    """

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().strip("[]")
        return tuple(float(part) for part in text.split(",")) if text else ()
    to_list = getattr(value, "to_list", None)
    if callable(to_list):
        return tuple(float(item) for item in to_list())
    return tuple(float(item) for item in value)


def default_lexical_text(content: str) -> str:
    """用项目统一分词器生成全文检索用的 token 串。

    复用 ``knowledge/tokenizer.py``：那里已经处理了中英文混排、源码标识符和 API
    路由，记忆库不应该长出第二套分词口径。分词失败时退化为原文——宁可检索质量
    差一点，也不要因为一个可选依赖让写入整体失败。
    """

    try:
        from klonet_agent.knowledge.tokenizer import DEFAULT_TOKENIZER
    except Exception:  # pragma: no cover - 取决于可选依赖
        return normalize_content(content)
    return " ".join(
        token for token in DEFAULT_TOKENIZER.tokenize(str(content)) if token.strip()
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _candidate_payload(candidate: MemoryCandidate) -> dict[str, Any]:
    """候选的结构化存档。

    刻意不存完整工具输出：candidate 只带经过领域层限长的来源摘录，完整证据留在
    原事件系统里（见计划 §5.2 与 memory/domain.py 的限长校验）。
    """

    return {
        "memory_type": candidate.memory_type.value,
        "scope": candidate.scope.value,
        "subject_key": candidate.subject_key,
        "content": candidate.content,
        "summary": candidate.summary,
        "project_id": candidate.project_id,
        "importance": candidate.importance,
        "confidence": candidate.confidence,
        "verified": candidate.verified,
        "observed_at": _iso(candidate.observed_at),
        "valid_from": _iso(candidate.valid_from),
        "metadata": dict(candidate.metadata),
        "sources": [
            {
                "source_type": source.source_type.value,
                "source_id": source.source_id,
                "source_excerpt": source.source_excerpt,
                "observed_at": _iso(source.observed_at),
            }
            for source in candidate.sources
        ],
    }


def _record_from_row(
    row: Mapping[str, Any], *, active_version: MemoryVersion | None = None
) -> MemoryRecord:
    active_id = row["active_version_id"]
    return MemoryRecord(
        id=str(row["id"]),
        user_id=row["user_id"],
        scope=Scope(row["scope"]),
        memory_type=MemoryType(row["memory_type"]),
        subject_key=row["subject_key"],
        status=MemoryStatus(row["status"]),
        project_id=row["project_id"],
        active_version_id=str(active_id) if active_id else None,
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        active_version=active_version,
    )


def _version_from_row(row: Mapping[str, Any]) -> MemoryVersion:
    return MemoryVersion(
        id=str(row["id"]),
        memory_id=str(row["memory_id"]),
        version=int(row["version"]),
        content=row["content"],
        summary=row["summary"],
        metadata=dict(row["metadata"] or {}),
        lexical_text=row["lexical_text"],
        embedding=_parse_vector(row["embedding"]),
        embedding_model=row["embedding_model"],
        embedding_version=row["embedding_version"],
        content_hash=row["content_hash"],
        observed_at=row["observed_at"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        created_at=row["created_at"],
        verified=bool(row.get("verified", False)),
    )


def _source_from_row(row: Mapping[str, Any]) -> MemorySource:
    return MemorySource(
        source_type=SourceType(row["source_type"]),
        source_id=row["source_id"],
        observed_at=row["observed_at"],
        memory_version_id=str(row["memory_version_id"]),
        source_excerpt=row["source_excerpt"] or "",
    )


def _relation_from_row(row: Mapping[str, Any]) -> MemoryRelation:
    return MemoryRelation(
        from_memory_id=str(row["from_memory_id"]),
        relation_type=RelationType(row["relation_type"]),
        to_memory_id=str(row["to_memory_id"]),
        confidence=float(row["confidence"]),
        created_at=row["created_at"],
    )


# --------------------------------------------------------------------------- #
# 仓库
# --------------------------------------------------------------------------- #


class PostgresMemoryRepository:
    """``memory/repository.py`` 契约的 PostgreSQL 实现。

    一个实例绑定一个 ``Tenant``。构造时就要 tenant，之后每个方法都在该 tenant
    的事务与过滤条件下执行，不存在"忘了传 user_id"的调用方式。
    """

    def __init__(self, database: MemoryDatabase, tenant: Tenant) -> None:
        self._database = database
        self._tenant = tenant

    # ------------------------------------------------------------ 基础设施 --

    @property
    def tenant(self) -> Tenant:
        return self._tenant

    def with_tenant(self, tenant: Tenant) -> "PostgresMemoryRepository":
        """返回一个换了租户的同构仓库（复用同一个连接池）。"""

        return PostgresMemoryRepository(self._database, tenant)

    @contextmanager
    def _session(self, *, readonly: bool = False) -> Iterator[Any]:
        with self._database.tenant_session(self._tenant, readonly=readonly) as conn:
            yield conn

    def _require_tenant_scope(self, user_id: str, project_id: str | None) -> None:
        """应用层提前拦截跨租户写入。

        RLS 本来就会拒绝，但驱动抛的是权限错误，排查成本高；这里给出"哪条记忆
        属于谁"的可读原因，数据库那道闸仍然保留。
        """

        if str(user_id) != str(self._tenant.user_id):
            raise ScopeViolationError(
                f"写入 user_id={user_id!r} 与当前会话租户 "
                f"{self._tenant.user_id!r} 不一致"
            )
        if project_id and str(project_id) != str(self._tenant.project_id or ""):
            raise ScopeViolationError(
                f"写入 project_id={project_id!r} 与当前会话项目 "
                f"{self._tenant.project_id!r} 不一致"
            )

    @staticmethod
    def _resolve_verified(
        memory_type: MemoryType | None,
        verified: bool,
        sources: Sequence[MemorySource] | None,
    ) -> bool:
        """校验 ``verified`` 的说法有没有证据支撑。

        刻意**不从来源自动推导** verified：用户陈述是证据，但"这条陈述是否确认了
        当前这个值"是逐版本的判断，仅凭"来源里有 user_statement"就自动打勾会把
        verified 变成装饰。所以这里只做校验——声称已验证就必须拿得出合格来源
        （见计划 §7.1）。
        """

        if memory_type is not None:
            ensure_verification_supported(memory_type, verified, sources)
        return bool(verified)

    def _record_scope(
        self, alias: str = "r"
    ) -> tuple[str, list[Any]]:
        """返回 ``(SQL 条件, 参数)``，把查询限制在当前租户可见的记忆上。

        口径与 RLS policy 一致：必须同 user；绑定了项目时，项目记忆要么属于该项目，
        要么是 user 作用域（project_id 为 NULL）的通用记忆；未绑定项目时只看
        project_id 为 NULL 的记录。
        """

        user_id = self._tenant.user_id
        project_id = self._tenant.project_id
        if project_id:
            return (
                f"{alias}.user_id = %s "
                f"AND ({alias}.project_id IS NULL OR {alias}.project_id = %s)",
                [user_id, project_id],
            )
        return f"{alias}.user_id = %s AND {alias}.project_id IS NULL", [user_id]

    def _candidate_scope(self) -> tuple[str, list[Any]]:
        return "user_id = %s", [self._tenant.user_id]

    @staticmethod
    def _try_execute(conn: Any, sql: str, params: Sequence[Any]) -> Exception | None:
        """在 savepoint 里执行，返回异常（成功则返回 None）。

        冲突之后往往还要查一次库（"已有哪条记录占了这个 subject"），而 PostgreSQL
        事务一旦报错就 aborted，必须回滚到 savepoint 才能继续。把这件事收在这里，
        业务代码就不用到处写 savepoint。
        """

        try:
            with conn.transaction():
                conn.execute(sql, params)
        except Exception as exc:  # noqa: BLE001 - 由调用方判定是否可识别
            return exc
        return None

    # ---------------------------------------------------------------- 写入 --

    def add_candidate(
        self,
        candidate: MemoryCandidate,
        *,
        idempotency_key: str,
        source_event_range: Mapping[str, Any],
    ) -> str:
        self._require_tenant_scope(candidate.user_id, candidate.project_id)
        return self.add_candidate_payload(
            _candidate_payload(candidate),
            user_id=candidate.user_id,
            project_id=candidate.project_id,
            idempotency_key=idempotency_key,
            source_event_range=source_event_range,
        )

    def add_candidate_payload(
        self,
        payload: Mapping[str, Any],
        *,
        user_id: str,
        project_id: str | None,
        idempotency_key: str,
        source_event_range: Mapping[str, Any],
    ) -> str:
        key = str(idempotency_key or "").strip()
        if not key:
            raise MemoryDomainError("候选必须带 idempotency_key")
        self._require_tenant_scope(user_id, project_id)

        candidate_id = uuid4()
        sql = """
        WITH inserted AS (
            INSERT INTO memory_write_candidates
                (id, idempotency_key, user_id, project_id,
                 source_event_range, candidate_payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
        )
        SELECT id FROM inserted
        UNION ALL
        SELECT id FROM memory_write_candidates WHERE idempotency_key = %s
        LIMIT 1
        """
        with self._session() as conn:
            row = conn.execute(
                sql,
                (
                    candidate_id,
                    key,
                    user_id,
                    project_id,
                    _jsonb(dict(source_event_range)),
                    _jsonb(dict(payload)),
                    key,
                ),
            ).fetchone()
        if row is None:
            # 幂等键已存在但对当前租户不可见：跨用户的键碰撞，不能当成"已存在"
            # 返回别人的候选 id。
            raise MemoryRepositoryError(f"幂等键 {key!r} 已被当前租户之外的数据占用")
        return str(row["id"])

    def processed_source_ranges(
        self, *, limit: int = 1000
    ) -> list[Mapping[str, Any]]:
        if limit <= 0:
            raise MemoryDomainError("limit 必须为正整数")
        scope, scope_params = self._candidate_scope()
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT source_event_range
                  FROM memory_write_candidates
                 WHERE {scope}
                 ORDER BY source_event_range
                 LIMIT %s
                """,
                [*scope_params, limit],
            ).fetchall()
        return [dict(row["source_event_range"] or {}) for row in rows]

    def record_decision(
        self,
        candidate_id: str,
        decision: WriteDecision | str,
        *,
        reason: str,
        processed_at: datetime | None = None,
    ) -> None:
        try:
            resolved = WriteDecision(decision)
        except ValueError as exc:
            raise MemoryDomainError(f"非法写入决策: {decision!r}") from exc
        cid = _as_uuid(candidate_id, "candidate_id")
        scope, scope_params = self._candidate_scope()
        with self._session() as conn:
            row = conn.execute(
                f"""
                UPDATE memory_write_candidates
                   SET decision = %s,
                       decision_reason = %s,
                       processed_at = COALESCE(%s, now())
                 WHERE id = %s AND {scope} AND decision IS NULL
                RETURNING id
                """,
                [resolved.value, reason, processed_at, cid, *scope_params],
            ).fetchone()
            if row is None:
                raise CandidateNotFoundError(
                    f"候选 {candidate_id} 不存在、不属于当前租户，或已经决策过"
                )

    def add_record(self, command: NewRecordCommand) -> MemoryRecord:
        self._require_tenant_scope(command.user_id, command.project_id)
        content = normalize_content(command.content)
        if not content:
            raise MemoryDomainError("记忆正文为空")
        digest = content_hash(content)
        lexical = (
            command.lexical_text
            if command.lexical_text is not None
            else default_lexical_text(content)
        )
        record_id = _as_uuid(command.record_id, "record_id") or uuid4()
        version_id = _as_uuid(command.version_id, "version_id") or uuid4()
        observed_at = command.observed_at or _now()
        valid_from = command.valid_from or observed_at
        validate_validity_window(valid_from, None)
        # 只有第一条版本能这么插：库里 uq_memory_records_active_subject 保证
        # 一个 subject 至多一条 active 记忆。
        verified = self._resolve_verified(
            command.memory_type, command.verified, command.sources
        )
        psycopg, _ = load_psycopg()

        with self._session() as conn:
            failure = self._try_execute(
                conn,
                """
                INSERT INTO memory_records
                    (id, user_id, project_id, scope, memory_type, subject_key,
                     importance, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record_id,
                    command.user_id,
                    command.project_id,
                    command.scope.value,
                    command.memory_type.value,
                    command.subject_key,
                    command.importance,
                    command.confidence,
                ),
            )
            if isinstance(failure, psycopg.errors.UniqueViolation):
                raise self._active_subject_conflict(conn, failure, command.subject_key)
            if failure is not None:
                raise failure

            failure = self._try_execute(
                conn,
                """
                INSERT INTO memory_versions
                    (id, memory_id, version, content, summary, metadata,
                     lexical_text, content_hash, observed_at, valid_from, valid_to,
                     verified)
                VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
                """,
                (
                    version_id,
                    record_id,
                    content,
                    command.summary,
                    _jsonb(dict(command.metadata)),
                    lexical,
                    digest,
                    observed_at,
                    valid_from,
                    verified,
                ),
            )
            if isinstance(failure, psycopg.errors.UniqueViolation):
                raise self._duplicate_content(conn, record_id, digest)
            if failure is not None:
                raise failure

            conn.execute(
                "UPDATE memory_records SET active_version_id = %s, updated_at = now() "
                "WHERE id = %s",
                (version_id, record_id),
            )
            self._insert_sources(conn, version_id, command.sources)
            self._enqueue_outbox(conn, version_id)
            row = self._select_record(conn, record_id)
            version = self._select_version_object(conn, version_id)

        return _record_from_row(row, active_version=version)

    def add_version(self, command: NewVersionCommand) -> MemoryVersion:
        memory_id = _as_uuid(command.memory_id, "memory_id")
        content = normalize_content(command.content)
        if not content:
            raise MemoryDomainError("记忆正文为空")
        if command.confidence is not None:
            _check_confidence(command.confidence)
        digest = content_hash(content)
        lexical = (
            command.lexical_text
            if command.lexical_text is not None
            else default_lexical_text(content)
        )
        version_id = _as_uuid(command.version_id, "version_id") or uuid4()
        observed_at = command.observed_at or _now()
        valid_from = command.valid_from or observed_at
        validate_validity_window(valid_from, None)
        psycopg, _ = load_psycopg()

        with self._session() as conn:
            # 先锁住逻辑记录：并发追加版本时把 (memory_id, version) 的 MAX+1
            # 串行化，否则两个并发事务会算出同一个版本号。
            record = self._locked_record(conn, memory_id)
            if record["status"] != MemoryStatus.ACTIVE.value:
                # 状态机只有 ACTIVE 是非终态，这里直接给出可读原因。
                ensure_status_transition(
                    record["status"], MemoryStatus.SUPERSEDED, subject=command.memory_id
                )
                raise RecordNotActiveError(
                    f"记忆 {command.memory_id} 当前状态为 {record['status']}，"
                    "不能再追加版本"
                )
            # verified 的合法性依赖记忆类型（偏好只认用户陈述），
            # 类型在逻辑记录上，所以只能锁住记录之后再校验。
            verified = self._resolve_verified(
                MemoryType(record["memory_type"]), command.verified, command.sources
            )

            next_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM memory_versions "
                "WHERE memory_id = %s",
                (memory_id,),
            ).fetchone()["next"]

            # 结束上一版的有效期。**必须做**：否则同一时刻会有两个版本
            # 同时满足 `valid_from <= t AND valid_to IS NULL`，as_of 查询会
            # 对同一个 subject 返回两条"当时有效"的记录。
            # 用 GREATEST 兜住"新版本 valid_from 不晚于旧版本"的极端输入，
            # 同时保证满足 `valid_to > valid_from` 的 CHECK。
            if record["active_version_id"]:
                conn.execute(
                    """
                    UPDATE memory_versions
                       SET valid_to = GREATEST(%s::timestamptz,
                                               valid_from + interval '1 microsecond')
                     WHERE id = %s AND valid_to IS NULL
                    """,
                    (valid_from, record["active_version_id"]),
                )

            failure = self._try_execute(
                conn,
                """
                INSERT INTO memory_versions
                    (id, memory_id, version, content, summary, metadata,
                     lexical_text, content_hash, observed_at, valid_from, valid_to,
                     verified)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
                """,
                (
                    version_id,
                    memory_id,
                    next_version,
                    content,
                    command.summary,
                    _jsonb(dict(command.metadata)),
                    lexical,
                    digest,
                    observed_at,
                    valid_from,
                    verified,
                ),
            )
            if isinstance(failure, psycopg.errors.UniqueViolation):
                raise self._duplicate_content(conn, memory_id, digest)
            if failure is not None:
                raise failure

            # 置信度只允许抬高：它是随证据积累单调上升的量，
            # 一次描述改写不该让它倒退。
            if command.confidence is not None:
                conn.execute(
                    "UPDATE memory_records SET active_version_id = %s, "
                    "confidence = GREATEST(confidence, %s), updated_at = now() "
                    "WHERE id = %s",
                    (version_id, command.confidence, memory_id),
                )
            else:
                conn.execute(
                    "UPDATE memory_records SET active_version_id = %s, updated_at = now() "
                    "WHERE id = %s",
                    (version_id, memory_id),
                )
            self._insert_sources(conn, version_id, command.sources)
            self._enqueue_outbox(conn, version_id)
            row = self._select_version(conn, version_id)

        return _version_from_row(row)

    def replace_active(
        self,
        old_memory_id: str,
        command: NewRecordCommand,
        *,
        relation_confidence: float = 1.0,
        reason: str | None = None,
    ) -> MemoryRecord:
        """用同一 subject 的新记忆原子替代当前的 active 记忆。

        计划 §6.5 的迁移例子（Python 3.8 → 3.11）要求新旧记忆拥有**同一个**
        ``subject_key``。这种情况下不能拆成"先 add_record 再 supersede"：
        插入新记录时会撞上 ``uq_memory_records_active_subject``，因为旧记录此刻
        仍然是 active。唯一正确的顺序是在**同一个事务里**先让旧记录离开 active
        （状态转 superseded、当前版本结束有效期），再插入新记录——事务提交时
        该 subject 恰好只剩一条 active。

        ``relation_confidence`` 是新建的 ``supersedes`` 关系的置信度，
        与被替代/新建记忆各自的 confidence 无关。
        """

        self._require_tenant_scope(command.user_id, command.project_id)
        old_id = _as_uuid(old_memory_id, "old_memory_id")
        content = normalize_content(command.content)
        if not content:
            raise MemoryDomainError("记忆正文为空")
        digest = content_hash(content)
        lexical = (
            command.lexical_text
            if command.lexical_text is not None
            else default_lexical_text(content)
        )
        record_id = _as_uuid(command.record_id, "record_id") or uuid4()
        version_id = _as_uuid(command.version_id, "version_id") or uuid4()
        observed_at = command.observed_at or _now()
        valid_from = command.valid_from or observed_at
        validate_validity_window(valid_from, None)
        verified = self._resolve_verified(
            command.memory_type, command.verified, command.sources
        )
        psycopg, _ = load_psycopg()

        with self._session() as conn:
            old_record = self._locked_record(conn, old_id)
            if old_record["status"] != MemoryStatus.ACTIVE.value:
                raise RecordNotActiveError(
                    f"记忆 {old_memory_id} 状态为 {old_record['status']}，无需再替代"
                )
            if old_record["subject_key"] != command.subject_key:
                raise MemoryDomainError(
                    "替代只允许发生在同一 subject 上："
                    f"旧记忆是 {old_record['subject_key']!r}，"
                    f"新记忆是 {command.subject_key!r}。"
                    "不同 subject 之间请用 add_record + supersede()"
                )
            if not type_allows_supersede(old_record["memory_type"]):
                # 情景记忆按事件身份去重，两次不同的经历可以拥有同一个话题，
                # 所以"新经历替代旧经历"没有意义（计划 §6.5 规则 3）。
                # consolidation 层已经把这种候选降级成 UPDATE，这里再兜一道。
                raise MemoryDomainError(
                    f"{old_record['memory_type']} 不参与替代："
                    "情景记忆保留各自事件，请改用 add_version()"
                )

            # ① 旧记录先离开 active，腾出"一个 subject 一条 active"的名额。
            conn.execute(
                "UPDATE memory_records SET status = %s, updated_at = now() WHERE id = %s",
                (MemoryStatus.SUPERSEDED.value, old_id),
            )
            # ② 旧值结束有效期：这才是"当时有效的事实"的依据。
            if old_record["active_version_id"]:
                conn.execute(
                    """
                    UPDATE memory_versions
                       SET valid_to = GREATEST(%s::timestamptz,
                                               valid_from + interval '1 microsecond')
                     WHERE id = %s AND valid_to IS NULL
                    """,
                    (valid_from, old_record["active_version_id"]),
                )

            # ③ 新记录成为该 subject 唯一的 active。
            failure = self._try_execute(
                conn,
                """
                INSERT INTO memory_records
                    (id, user_id, project_id, scope, memory_type, subject_key,
                     importance, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record_id,
                    command.user_id,
                    command.project_id,
                    command.scope.value,
                    command.memory_type.value,
                    command.subject_key,
                    command.importance,
                    command.confidence,
                ),
            )
            if isinstance(failure, psycopg.errors.UniqueViolation):
                raise self._active_subject_conflict(conn, failure, command.subject_key)
            if failure is not None:
                raise failure

            failure = self._try_execute(
                conn,
                """
                INSERT INTO memory_versions
                    (id, memory_id, version, content, summary, metadata,
                     lexical_text, content_hash, observed_at, valid_from, valid_to,
                     verified)
                VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
                """,
                (
                    version_id,
                    record_id,
                    content,
                    command.summary,
                    _jsonb(dict(command.metadata)),
                    lexical,
                    digest,
                    observed_at,
                    valid_from,
                    verified,
                ),
            )
            if isinstance(failure, psycopg.errors.UniqueViolation):
                raise self._duplicate_content(conn, record_id, digest)
            if failure is not None:
                raise failure

            conn.execute(
                "UPDATE memory_records SET active_version_id = %s, updated_at = now() "
                "WHERE id = %s",
                (version_id, record_id),
            )
            # ④ 关系与替代同事务落库，保证"有替代必有关系"。
            conn.execute(
                """
                INSERT INTO memory_relations
                    (from_memory_id, relation_type, to_memory_id, confidence, reason)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (from_memory_id, relation_type, to_memory_id) DO NOTHING
                """,
                (
                    str(record_id),
                    RelationType.SUPERSEDES.value,
                    str(old_id),
                    relation_confidence,
                    reason,
                ),
            )
            self._insert_sources(conn, version_id, command.sources)
            self._enqueue_outbox(conn, version_id)
            row = self._select_record(conn, record_id)
            version = self._select_version_object(conn, version_id)

        return _record_from_row(row, active_version=version)

    def supersede(
        self,
        old_memory_id: str,
        new_memory_id: str,
        *,
        confidence: float = 1.0,
        reason: str | None = None,
    ) -> None:
        old_id = _as_uuid(old_memory_id, "old_memory_id")
        new_id = _as_uuid(new_memory_id, "new_memory_id")
        if old_id == new_id:
            raise MemoryDomainError("记忆不能替代自己")
        relation = MemoryRelation(
            from_memory_id=str(new_id),
            relation_type=RelationType.SUPERSEDES,
            to_memory_id=str(old_id),
            confidence=confidence,
        )

        with self._session() as conn:
            # 固定加锁顺序，避免两个互相 supersede 的事务死锁。
            for memory_id in sorted({old_id, new_id}, key=str):
                self._locked_record(conn, memory_id)
            old_record = self._locked_record(conn, old_id)
            if old_record["status"] != MemoryStatus.ACTIVE.value:
                raise RecordNotActiveError(
                    f"记忆 {old_memory_id} 状态为 {old_record['status']}，无需再替代"
                )

            conn.execute(
                """
                INSERT INTO memory_relations
                    (from_memory_id, relation_type, to_memory_id, confidence, reason)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (from_memory_id, relation_type, to_memory_id) DO NOTHING
                """,
                (
                    relation.from_memory_id,
                    relation.relation_type.value,
                    relation.to_memory_id,
                    relation.confidence,
                    reason,
                ),
            )
            # 旧事实结束有效期：版本级的 valid_to 才是"当时有效的事实"的依据，
            # 逻辑记录的状态只用于快速过滤。
            conn.execute(
                """
                UPDATE memory_versions AS v
                   SET valid_to = GREATEST(now(),
                                           v.valid_from + interval '1 microsecond')
                  FROM memory_records AS r
                 WHERE r.id = %s
                   AND r.active_version_id = v.id
                   AND v.valid_to IS NULL
                """,
                (old_id,),
            )
            conn.execute(
                "UPDATE memory_records SET status = %s, updated_at = now() "
                "WHERE id = %s AND status = %s",
                (MemoryStatus.SUPERSEDED.value, old_id, MemoryStatus.ACTIVE.value),
            )

    def mark_expired(self, memory_id: str, valid_to: datetime) -> None:
        """让一条记忆在 ``valid_to`` 之后失效，历史版本全部保留。

        只对 **active** 记忆有效。早期实现无条件更新"当前版本"的 ``valid_to``，
        对一条已经被替代的记忆调用它会把一个已经封存的版本的时间窗再改一次
        （记录状态因为 `status = 'active'` 条件不变，于是变成一次静默的历史篡改）。
        现在先锁记录再看状态，非 active 直接报错。
        """

        if valid_to is None:
            raise MemoryDomainError("过期时间不能为空")
        mid = _as_uuid(memory_id, "memory_id")
        with self._session() as conn:
            record = self._locked_record(conn, mid)
            if record["status"] != MemoryStatus.ACTIVE.value:
                raise RecordNotActiveError(
                    f"记忆 {memory_id} 状态为 {record['status']}，"
                    "只有 active 记忆才能被标记过期"
                )
            if record["active_version_id"]:
                current = self._select_version(conn, record["active_version_id"])
                if current is not None:
                    # 先按领域规则校验，避免把越界输入交给数据库 CHECK 报一个
                    # 看不出上下文的错误。
                    validate_validity_window(current["valid_from"], valid_to)
            conn.execute(
                """
                UPDATE memory_versions AS v
                   SET valid_to = %s
                  FROM memory_records AS r
                 WHERE r.id = %s
                   AND r.active_version_id = v.id
                   AND v.valid_to IS NULL
                """,
                (valid_to, mid),
            )
            conn.execute(
                "UPDATE memory_records SET status = %s, updated_at = now() "
                "WHERE id = %s AND status = %s",
                (MemoryStatus.EXPIRED.value, mid, MemoryStatus.ACTIVE.value),
            )

    def add_source(self, version_id: str, source: MemorySource) -> None:
        vid = _as_uuid(version_id, "version_id")
        with self._session() as conn:
            if self._select_version(conn, vid) is None:
                raise RecordNotFoundError(f"版本 {version_id} 不存在或不属于当前租户")
            self._insert_sources(conn, vid, (source,))

    def attach_sources(
        self,
        memory_id: str,
        sources: Sequence[MemorySource],
        *,
        confidence: float | None = None,
        verified: bool = False,
    ) -> MemoryVersion:
        """把新来源挂到当前 active 版本上（"只补充来源/置信度"的 UPDATE）。"""

        mid = _as_uuid(memory_id, "memory_id")
        if confidence is not None:
            _check_confidence(confidence)

        with self._session() as conn:
            record = self._locked_record(conn, mid)
            if record["status"] != MemoryStatus.ACTIVE.value:
                raise RecordNotActiveError(
                    f"记忆 {memory_id} 状态为 {record['status']}，不能补充来源"
                )
            version_id = record["active_version_id"]
            if not version_id:
                # 逻辑记录没有当前版本说明数据被外部改坏了（正常写入永远
                # 记录与版本同事务创建）。不猜，直接报错。
                raise MemoryRepositoryError(
                    f"记忆 {memory_id} 没有当前版本，无法补充来源"
                )
            current = self._select_version(conn, version_id)
            if current is None:
                raise RecordNotFoundError(f"记忆 {memory_id} 的当前版本不可见")

            # verified 的说法要拿"已有来源 + 这批新来源"一起校验，
            # 只看到新来源会误判（例如已有 user_statement，新来的是 history_event）。
            known = self._existing_source_objects(conn, version_id)
            resolved_verified = self._resolve_verified(
                MemoryType(record["memory_type"]), verified, (*known, *sources)
            )

            self._insert_sources(conn, version_id, sources)

            if resolved_verified and not current["verified"]:
                conn.execute(
                    "UPDATE memory_versions SET verified = true WHERE id = %s",
                    (version_id,),
                )
            if confidence is not None:
                conn.execute(
                    "UPDATE memory_records SET confidence = GREATEST(confidence, %s), "
                    "updated_at = now() WHERE id = %s",
                    (confidence, mid),
                )
            row = self._select_version(conn, version_id)

        return _version_from_row(row)

    def _existing_source_objects(
        self, conn: Any, version_id: UUID
    ) -> tuple[MemorySource, ...]:
        rows = conn.execute(
            "SELECT * FROM memory_sources WHERE memory_version_id = %s",
            (version_id,),
        ).fetchall()
        return tuple(_source_from_row(row) for row in rows)

    def add_relation(
        self,
        from_memory_id: str,
        relation_type: str,
        to_memory_id: str,
        *,
        confidence: float = 0.5,
    ) -> None:
        try:
            resolved = RelationType(relation_type)
        except ValueError as exc:
            raise MemoryDomainError(f"非法关系类型: {relation_type!r}") from exc
        relation = MemoryRelation(
            from_memory_id=from_memory_id,
            relation_type=resolved,
            to_memory_id=to_memory_id,
            confidence=confidence,
        )
        from_id = _as_uuid(relation.from_memory_id, "from_memory_id")
        to_id = _as_uuid(relation.to_memory_id, "to_memory_id")
        with self._session() as conn:
            # 两端都要可见，否则不允许建关系（RLS 也会拦，这里提前给原因）。
            self._locked_record(conn, from_id)
            self._locked_record(conn, to_id)
            conn.execute(
                """
                INSERT INTO memory_relations
                    (from_memory_id, relation_type, to_memory_id, confidence)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (from_memory_id, relation_type, to_memory_id) DO NOTHING
                """,
                (from_id, resolved.value, to_id, relation.confidence),
            )

    def enqueue_embedding(
        self, version_id: str, embedding_profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID
    ) -> None:
        vid = _as_uuid(version_id, "version_id")
        with self._session() as conn:
            if self._select_version(conn, vid) is None:
                raise RecordNotFoundError(f"版本 {version_id} 不存在或不属于当前租户")
            self._enqueue_outbox(conn, vid, embedding_profile_id)

    def set_embedding(
        self,
        version_id: str,
        embedding: Sequence[float],
        *,
        embedding_model: str,
        embedding_version: str,
    ) -> None:
        values = tuple(float(item) for item in embedding)
        if len(values) != EMBEDDING_DIMENSIONS:
            raise MemoryDomainError(
                f"向量维度 {len(values)} 与 schema 的 vector({EMBEDDING_DIMENSIONS}) 不一致。"
                "换维度需要新迁移，不能原地改"
            )
        if not embedding_model or not embedding_version:
            raise MemoryDomainError(
                "写入向量必须同时给出 embedding_model 与 embedding_version"
            )

        vid = _as_uuid(version_id, "version_id")
        with self._session() as conn:
            if self._select_version(conn, vid) is None:
                raise RecordNotFoundError(f"版本 {version_id} 不存在或不属于当前租户")
            updated = conn.execute(
                """
                UPDATE memory_versions
                   SET embedding = %s::vector,
                       embedding_model = %s,
                       embedding_version = %s
                 WHERE id = %s
                RETURNING id
                """,
                (_vector_literal(values), embedding_model, embedding_version, vid),
            ).fetchone()
            if updated is None:
                raise RecordNotFoundError(f"版本 {version_id} 不存在或不属于当前租户")
            conn.execute(
                """
                UPDATE memory_embedding_outbox
                   SET status = 'completed', last_error = NULL,
                       next_attempt_at = NULL, updated_at = now()
                 WHERE memory_version_id = %s AND embedding_profile_id = %s
                """,
                (vid, DEFAULT_EMBEDDING_PROFILE_ID),
            )

    # --------------------------------------------------------- embedding 队列 --

    def claim_pending_embeddings(
        self,
        *,
        limit: int = 20,
        lease_seconds: float = 300.0,
        profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID,
        now: datetime | None = None,
    ) -> list[PendingEmbedding]:
        if limit <= 0:
            return []
        profile = str(profile_id or "").strip()
        if not profile:
            raise MemoryDomainError("embedding_profile_id 不能为空")
        moment = now or _now()
        lease_until = moment + timedelta(seconds=max(1.0, float(lease_seconds)))
        scope, scope_params = self._record_scope("r")

        # 领取条件把三种情况都压在 ``next_attempt_at`` 这一个时间列上：
        #   pending + next_attempt_at IS NULL   → 新任务
        #   pending + next_attempt_at <= now()  → 失败退避到点了
        #   processing + 租约过期               → 上一个 worker 崩了
        # 于是"崩溃恢复"不需要额外的清理进程，一个越界的租约就是它的全部机制。
        sql = f"""
        WITH claimed AS (
            SELECT o.memory_version_id, o.embedding_profile_id
              FROM memory_embedding_outbox o
              JOIN memory_versions v ON v.id = o.memory_version_id
              JOIN memory_records r ON r.id = v.memory_id
             WHERE o.embedding_profile_id = %s
               AND o.status IN ('pending', 'processing')
               AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= %s)
               AND {scope}
             ORDER BY o.created_at, o.memory_version_id
             LIMIT %s
             FOR UPDATE OF o SKIP LOCKED
        )
        UPDATE memory_embedding_outbox o
           SET status = 'processing',
               attempt_count = o.attempt_count + 1,
               next_attempt_at = %s,
               updated_at = now()
          FROM claimed c, memory_versions v
         WHERE o.memory_version_id = c.memory_version_id
           AND o.embedding_profile_id = c.embedding_profile_id
           AND v.id = o.memory_version_id
        RETURNING o.memory_version_id, o.attempt_count, o.embedding_profile_id,
                  v.memory_id, v.content
        """
        with self._session() as conn:
            rows = conn.execute(
                sql,
                [profile, moment, *scope_params, int(limit), lease_until],
            ).fetchall()
        return [
            PendingEmbedding(
                version_id=str(row["memory_version_id"]),
                memory_id=str(row["memory_id"]),
                content=row["content"] or "",
                attempt_count=int(row["attempt_count"]),
                embedding_profile_id=str(row["embedding_profile_id"]),
            )
            for row in rows
        ]

    def mark_embedding_failed(
        self,
        version_id: str,
        error: str,
        *,
        retry_at: datetime | None,
        profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID,
    ) -> None:
        vid = _as_uuid(version_id, "version_id")
        profile = str(profile_id or "").strip() or DEFAULT_EMBEDDING_PROFILE_ID
        message = str(error or "").strip()[:_MAX_OUTBOX_ERROR_LENGTH]
        # retry_at 为空表示放弃自动重试。这条记录仍然留在 outbox 里（不删），
        # 因为"这个版本没有向量"本身就是要被观测到的事实。
        status = "pending" if retry_at is not None else "failed"
        with self._session() as conn:
            updated = conn.execute(
                """
                UPDATE memory_embedding_outbox
                   SET status = %s, last_error = %s, next_attempt_at = %s,
                       updated_at = now()
                 WHERE memory_version_id = %s AND embedding_profile_id = %s
                RETURNING memory_version_id
                """,
                (status, message or None, retry_at, vid, profile),
            ).fetchone()
            if updated is None:
                raise RecordNotFoundError(
                    f"版本 {version_id} 的 embedding 任务不存在或不属于当前租户"
                )

    def embedding_outbox_stats(
        self, *, profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID
    ) -> EmbeddingOutboxStats:
        profile = str(profile_id or "").strip() or DEFAULT_EMBEDDING_PROFILE_ID
        scope, scope_params = self._record_scope("r")
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                f"""
                SELECT o.status AS status, count(*) AS n
                  FROM memory_embedding_outbox o
                  JOIN memory_versions v ON v.id = o.memory_version_id
                  JOIN memory_records r ON r.id = v.memory_id
                 WHERE o.embedding_profile_id = %s AND {scope}
                 GROUP BY o.status
                """,
                [profile, *scope_params],
            ).fetchall()
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        return EmbeddingOutboxStats(
            pending=counts.get("pending", 0),
            processing=counts.get("processing", 0),
            completed=counts.get("completed", 0),
            failed=counts.get("failed", 0),
        )

    # ---------------------------------------------------------------- 读取 --

    def get_record(self, memory_id: str) -> MemoryRecord | None:
        mid = _as_uuid(memory_id, "memory_id")
        with self._session(readonly=True) as conn:
            row = self._select_record(conn, mid)
            if row is None:
                return None
            version = self._select_version_object(conn, row["active_version_id"])
        return _record_from_row(row, active_version=version)

    def get_active(self, memory_id: str) -> MemoryRecord | None:
        mid = _as_uuid(memory_id, "memory_id")
        scope, scope_params = self._record_scope("r")
        with self._session(readonly=True) as conn:
            row = conn.execute(
                f"""
                SELECT r.*
                  FROM memory_records r
                  JOIN memory_versions v ON v.id = r.active_version_id
                 WHERE r.id = %s
                   AND {scope}
                   AND r.status = 'active'
                   AND v.valid_to IS NULL
                """,
                [mid, *scope_params],
            ).fetchone()
            if row is None:
                return None
            version = self._select_version_object(conn, row["active_version_id"])
        return _record_from_row(row, active_version=version)

    def get_version(self, version_id: str) -> MemoryVersion | None:
        vid = _as_uuid(version_id, "version_id")
        with self._session(readonly=True) as conn:
            row = self._select_version(conn, vid)
        return _version_from_row(row) if row is not None else None

    def find_active_by_subject(self, subject_key: str) -> MemoryRecord | None:
        scope, scope_params = self._record_scope("r")
        with self._session(readonly=True) as conn:
            row = conn.execute(
                f"SELECT r.* FROM memory_records r "
                f"WHERE r.subject_key = %s AND r.status = 'active' AND {scope} "
                "LIMIT 1",
                [subject_key, *scope_params],
            ).fetchone()
            if row is None:
                return None
            # 必须把 active 版本一起带出来：consolidation 要拿它比内容 hash、
            # 比来源、读 verified。少这一步的话调用方会拿到一个
            # ``active_version is None`` 的记录，判重会全部失效。
            version = self._select_version_object(conn, row["active_version_id"])
        return _record_from_row(row, active_version=version)

    def list_sources(self, version_id: str) -> list[MemorySource]:
        vid = _as_uuid(version_id, "version_id")
        scope, scope_params = self._record_scope("r")
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                f"""
                SELECT s.*
                  FROM memory_sources s
                  JOIN memory_versions v ON v.id = s.memory_version_id
                  JOIN memory_records r ON r.id = v.memory_id
                 WHERE s.memory_version_id = %s AND {scope}
                 ORDER BY s.observed_at, s.id
                """,
                [vid, *scope_params],
            ).fetchall()
        return [_source_from_row(row) for row in rows]

    def list_versions(self, memory_id: str) -> list[MemoryVersion]:
        mid = _as_uuid(memory_id, "memory_id")
        scope, scope_params = self._record_scope("r")
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                f"""
                SELECT v.*
                  FROM memory_versions v
                  JOIN memory_records r ON r.id = v.memory_id
                 WHERE v.memory_id = %s AND {scope}
                 ORDER BY v.version
                """,
                [mid, *scope_params],
            ).fetchall()
        return [_version_from_row(row) for row in rows]

    def list_relations(
        self,
        memory_id: str,
        *,
        relation_type: RelationType | None = None,
    ) -> list[MemoryRelation]:
        mid = _as_uuid(memory_id, "memory_id")
        # 关系表没有 user_id，租户约束要沿两端外键回溯到 memory_records。
        # 两端都必须可见：一端可见就返回关系，等于把另一端的 id 泄露出去。
        left, left_params = self._record_scope("rf")
        right, right_params = self._record_scope("rt")
        clauses = [
            "(rel.from_memory_id = %s OR rel.to_memory_id = %s)",
            "EXISTS (SELECT 1 FROM memory_records rf "
            f"WHERE rf.id = rel.from_memory_id AND {left})",
            "EXISTS (SELECT 1 FROM memory_records rt "
            f"WHERE rt.id = rel.to_memory_id AND {right})",
        ]
        params: list[Any] = [mid, mid, *left_params, *right_params]
        if relation_type is not None:
            clauses.append("rel.relation_type = %s")
            params.append(RelationType(relation_type).value)
        with self._session(readonly=True) as conn:
            rows = conn.execute(
                f"""
                SELECT rel.* FROM memory_relations rel
                 WHERE {" AND ".join(clauses)}
                 ORDER BY rel.created_at, rel.from_memory_id, rel.to_memory_id
                """,
                params,
            ).fetchall()
        return [_relation_from_row(row) for row in rows]

    def list_candidates(
        self, *, decision: WriteDecision | None = None, limit: int = 100
    ) -> list[CandidateRecord]:
        if limit <= 0:
            raise MemoryDomainError("limit 必须为正整数")
        scope, scope_params = self._candidate_scope()
        with self._session(readonly=True) as conn:
            if decision is None:
                rows = conn.execute(
                    f"SELECT * FROM memory_write_candidates "
                    f"WHERE {scope} AND decision IS NULL "
                    "ORDER BY created_at LIMIT %s",
                    [*scope_params, limit],
                ).fetchall()
            else:
                try:
                    resolved = WriteDecision(decision)
                except ValueError as exc:
                    raise MemoryDomainError(f"非法写入决策: {decision!r}") from exc
                rows = conn.execute(
                    f"SELECT * FROM memory_write_candidates "
                    f"WHERE {scope} AND decision = %s "
                    "ORDER BY created_at LIMIT %s",
                    [*scope_params, resolved.value, limit],
                ).fetchall()
        return [
            CandidateRecord(
                id=str(row["id"]),
                idempotency_key=row["idempotency_key"],
                user_id=row["user_id"],
                project_id=row["project_id"],
                source_event_range=dict(row["source_event_range"] or {}),
                candidate_payload=dict(row["candidate_payload"] or {}),
                decision=WriteDecision(row["decision"]) if row["decision"] else None,
                decision_reason=row["decision_reason"],
                processed_at=row["processed_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ---------------------------------------------------------------- 检索 --

    def search(
        self,
        query: MemoryQuery,
        *,
        query_embedding: Sequence[float] | None = None,
    ) -> list[MemoryHit]:
        """三通道（全文 / 向量 / 精确标识符）召回 + RRF 融合。

        向量缺失时**只退化为全文与精确通道**，不伪造语义结果；调用方可以从
        ``reasons`` 看出每条命中实际来自哪些通道。
        """

        if query_embedding is not None:
            values = tuple(float(item) for item in query_embedding)
            if len(values) != EMBEDDING_DIMENSIONS:
                raise MemoryDomainError(
                    f"查询向量维度 {len(values)} 与 vector({EMBEDDING_DIMENSIONS}) 不一致"
                )
        else:
            values = ()

        filters, params = self._build_filters(query)
        pool = max(query.limit * _CANDIDATE_POOL_FACTOR, _CANDIDATE_POOL_MIN)

        channels: dict[str, list[UUID]] = {}
        with self._session(readonly=True) as conn:
            lexical = _lexical_candidates(conn, query, filters, params, pool)
            if lexical:
                channels["lexical"] = lexical
            if values:
                semantic = _semantic_candidates(
                    conn, _vector_literal(values), filters, params, pool
                )
                if semantic:
                    channels["semantic"] = semantic
            exact = _exact_candidates(conn, query, filters, params, pool)
            if exact:
                channels["exact"] = exact

            if not channels:
                return []

            scores = _fuse(channels)
            ordered = sorted(scores, key=lambda item: (-scores[item], str(item)))
            hits = self._hydrate(conn, ordered, scores, channels)

        return hits[: query.limit]

    def _build_filters(self, query: MemoryQuery) -> tuple[str, list[Any]]:
        """构造硬过滤条件：租户 + 作用域 + 时态 + 置信度。

        作用域过滤是必需项，不是可选优化。租户条件写在这里而不是只依赖 RLS，
        这样即便连接是超级用户/BYPASSRLS 角色也不会跨用户召回。
        """

        scope, params = self._record_scope("r")

        if query.as_of is None:
            # 当前视图：每条记忆只看它当前的 active 版本，且该版本尚未失效。
            clauses = [
                "r.status = 'active'",
                "r.active_version_id = v.id",
                "v.valid_to IS NULL",
                scope,
            ]
        else:
            # as_of 视图：问的不是"谁是当前 active"，而是"哪个版本在那个时刻有效"。
            # 一条事实被替代之后，它的历史版本恰恰是 as_of 要回答的对象，
            # 所以这里**不能**要求 r.status = 'active'，也不能要求
            # r.active_version_id = v.id——否则 as_of 永远只能返回当前版本，
            # 时态查询等于没实现。
            clauses = ["r.status <> 'deleted'", scope]
            clauses.append("v.valid_from <= %s")
            params.append(query.as_of)
            clauses.append("(v.valid_to IS NULL OR v.valid_to > %s)")
            params.append(query.as_of)

        if query.scopes:
            clauses.append("r.scope = ANY(%s)")
            params.append([scope_value.value for scope_value in query.scopes])
        if query.memory_types:
            clauses.append("r.memory_type = ANY(%s)")
            params.append([memory_type.value for memory_type in query.memory_types])
        if query.min_confidence > 0:
            clauses.append("r.confidence >= %s")
            params.append(query.min_confidence)
        if not query.include_shared_ops:
            # 应用层再拦一道；RLS 对 klonet_app 也会拒绝 shared_ops。
            clauses.append("r.scope <> 'shared_ops'")

        return " AND ".join(clauses), params

    def _hydrate(
        self,
        conn: Any,
        version_ids: Sequence[UUID],
        scores: Mapping[UUID, float],
        channels: Mapping[str, Sequence[UUID]],
    ) -> list[MemoryHit]:
        if not version_ids:
            return []
        scope, scope_params = self._record_scope("r")

        version_rows = {
            row["id"]: row
            for row in conn.execute(
                f"""
                SELECT v.*
                  FROM memory_versions v
                  JOIN memory_records r ON r.id = v.memory_id
                 WHERE v.id = ANY(%s) AND {scope}
                """,
                [list(version_ids), *scope_params],
            ).fetchall()
        }
        record_ids = [row["memory_id"] for row in version_rows.values()]
        if not record_ids:
            # 全部版本都被租户过滤掉了：不要拿空列表去构造 `= ANY(%s)`，
            # 空数组的元素类型推断在部分驱动版本上会直接报错。
            return []
        record_rows = {
            row["id"]: row
            for row in conn.execute(
                f"SELECT r.* FROM memory_records r WHERE r.id = ANY(%s) AND {scope}",
                [record_ids, *scope_params],
            ).fetchall()
        }

        channels_of: dict[UUID, list[str]] = {}
        for channel, ids in channels.items():
            for vid in ids:
                channels_of.setdefault(vid, []).append(channel)

        hits: list[MemoryHit] = []
        for vid in version_ids:
            version_row = version_rows.get(vid)
            if version_row is None:
                continue
            record_row = record_rows.get(version_row["memory_id"])
            if record_row is None:
                continue
            hit_channels = channels_of.get(vid, [])
            hits.append(
                MemoryHit(
                    record=_record_from_row(record_row),
                    version=_version_from_row(version_row),
                    score=round(float(scores.get(vid, 0.0)), 8),
                    reasons=tuple(hit_channels),
                    exact_match="exact" in hit_channels,
                )
            )
        return hits

    # -------------------------------------------------------- 内部 SQL 封装 --

    def _select_record(self, conn: Any, memory_id: UUID) -> Mapping[str, Any] | None:
        scope, scope_params = self._record_scope("r")
        return conn.execute(
            f"SELECT r.* FROM memory_records r WHERE r.id = %s AND {scope}",
            [memory_id, *scope_params],
        ).fetchone()

    def _select_version(self, conn: Any, version_id: UUID) -> Mapping[str, Any] | None:
        scope, scope_params = self._record_scope("r")
        return conn.execute(
            f"""
            SELECT v.*
              FROM memory_versions v
              JOIN memory_records r ON r.id = v.memory_id
             WHERE v.id = %s AND {scope}
            """,
            [version_id, *scope_params],
        ).fetchone()

    def _select_version_object(
        self, conn: Any, version_id: UUID | None
    ) -> MemoryVersion | None:
        """按 id 取**领域对象**（而不是行字典）。

        ``_select_version`` 返回的是驱动给的行；直接把它塞进 ``MemoryRecord
        .active_version`` 会得到一个"看起来像 MemoryVersion 其实是 dict"的对象，
        调用方在 ``.version`` 上才炸。统一从这里出口。
        """

        if not version_id:
            return None
        row = self._select_version(conn, version_id)
        return _version_from_row(row) if row is not None else None

    def _locked_record(self, conn: Any, memory_id: UUID) -> Mapping[str, Any]:
        """取记录并加行锁。不可见与不存在返回同一错误，不泄露存在性。"""

        scope, scope_params = self._record_scope("r")
        row = conn.execute(
            f"SELECT r.* FROM memory_records r WHERE r.id = %s AND {scope} FOR UPDATE",
            [memory_id, *scope_params],
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"记忆 {memory_id} 不存在或不属于当前租户")
        return row

    def _active_subject_conflict(
        self, conn: Any, exc: Exception, subject_key: str
    ) -> Exception:
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
        if constraint == "uq_memory_records_active_subject":
            scope, scope_params = self._record_scope("r")
            row = conn.execute(
                f"SELECT r.id FROM memory_records r "
                f"WHERE r.subject_key = %s AND r.status = 'active' AND {scope} LIMIT 1",
                [subject_key, *scope_params],
            ).fetchone()
            return ActiveSubjectConflictError(
                subject_key, str(row["id"]) if row else ""
            )
        return exc

    def _duplicate_content(
        self, conn: Any, memory_id: UUID, digest: str
    ) -> Exception:
        row = conn.execute(
            "SELECT id, version FROM memory_versions "
            "WHERE memory_id = %s AND content_hash = %s",
            (memory_id, digest),
        ).fetchone()
        if row is not None:
            return DuplicateVersionError(str(row["id"]), int(row["version"]))
        return MemoryRepositoryError("版本写入冲突，但无法定位已有版本")

    def _insert_sources(
        self, conn: Any, version_id: UUID, sources: Sequence[MemorySource]
    ) -> None:
        for source in sources:
            conn.execute(
                """
                INSERT INTO memory_sources
                    (memory_version_id, source_type, source_id,
                     source_excerpt, observed_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (memory_version_id, source_type, source_id) DO NOTHING
                """,
                (
                    version_id,
                    source.source_type.value,
                    source.source_id,
                    source.source_excerpt,
                    source.observed_at,
                ),
            )

    def _enqueue_outbox(
        self,
        conn: Any,
        version_id: UUID,
        profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID,
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_embedding_outbox (memory_version_id, embedding_profile_id)
            VALUES (%s, %s)
            ON CONFLICT (memory_version_id, embedding_profile_id) DO NOTHING
            """,
            (version_id, profile_id),
        )


# --------------------------------------------------------------------------- #
# 检索通道
# --------------------------------------------------------------------------- #


def _tsquery_literal(tokens: Sequence[str]) -> str:
    """把 token 列表拼成 tsquery 字面量。

    token 来自 jieba 分词，可能含有 ``'`` 之类字符；用单引号包住并转义，
    避免被当成 tsquery 语法。
    """

    parts = []
    for token in tokens:
        cleaned = str(token).strip().replace("'", "''")
        if cleaned:
            parts.append(f"'{cleaned}'")
    return " | ".join(parts)


def _lexical_candidates(
    conn: Any,
    query: MemoryQuery,
    filters: str,
    params: Sequence[Any],
    pool: int,
) -> list[UUID]:
    # 查询侧必须和写入侧用同一个分词器：lexical_text 里存的就是这套 token，
    # 两边口径不一致的话全文通道会永远召回为空，而且不报错。
    literal = _tsquery_literal(default_lexical_text(query.text).split())
    if not literal:
        return []

    # 用 CTE 让 tsquery 只出现一次。参数是按占位符在 SQL 文本里的**出现顺序**
    # 绑定的，而 {filters} 自带若干 %s；把 tsquery 写成 CTE 后它的占位符一定
    # 排在最前，参数顺序就是字面量 → filters → pool，不可能错位。
    sql = f"""
    WITH q AS (SELECT to_tsquery('simple', %s) AS tsq)
    SELECT v.id AS id
      FROM memory_versions v
      JOIN memory_records r ON r.id = v.memory_id
      CROSS JOIN q
     WHERE {filters}
       AND v.lexical_tsv @@ q.tsq
     ORDER BY ts_rank_cd(v.lexical_tsv, q.tsq) DESC, v.id
     LIMIT %s
    """
    rows = conn.execute(sql, [literal, *params, pool]).fetchall()
    return [row["id"] for row in rows]


def _semantic_candidates(
    conn: Any,
    vector_literal: str,
    filters: str,
    params: Sequence[Any],
    pool: int,
) -> list[UUID]:
    sql = f"""
    SELECT v.id AS id
      FROM memory_versions v
      JOIN memory_records r ON r.id = v.memory_id
     WHERE {filters}
       AND v.embedding IS NOT NULL
     ORDER BY v.embedding <=> %s::vector, v.id
     LIMIT %s
    """
    rows = conn.execute(sql, [*params, vector_literal, pool]).fetchall()
    return [row["id"] for row in rows]


def _exact_candidates(
    conn: Any,
    query: MemoryQuery,
    filters: str,
    params: Sequence[Any],
    pool: int,
) -> list[UUID]:
    """精确标识符通道：路径、版本号、点分/下划线标识符。

    这类 token 会被分词器切碎（``a.b.c``、``/api/v1/x``），走全文匹配会漏。
    比对同时看 ``content`` 和 ``lexical_text``：

    - ``content`` 是原文，路径/版本号在这里是**原样**出现的，命中率最高；
    - ``lexical_text`` 是分词结果（标点已被剥掉），可以兜住 ``/VEMU2/`` 与
      ``vemu2`` 这种写法差异。

    代价是 ``content`` 上的 ``position()`` 走不了索引。阶段 1 的数据量下可以接受；
    真到需要时再按计划 §5.x 加独立的精确匹配列和索引。
    """

    tokens = {match.group(0) for match in _EXACT_TOKEN_RE.finditer(query.text)}
    if not tokens:
        return []

    clauses = []
    exact_params: list[Any] = []
    for token in sorted(tokens):
        clauses.append(
            "(position(lower(%s) in lower(v.content)) > 0 "
            " OR position(lower(%s) in lower(v.lexical_text)) > 0)"
        )
        exact_params.extend([token, token])

    sql = f"""
    SELECT v.id AS id
      FROM memory_versions v
      JOIN memory_records r ON r.id = v.memory_id
     WHERE {filters}
       AND {" AND ".join(clauses)}
     LIMIT %s
    """
    rows = conn.execute(sql, [*params, *exact_params, pool]).fetchall()
    return [row["id"] for row in rows]


def _fuse(channels: Mapping[str, Sequence[UUID]]) -> dict[UUID, float]:
    """加权 RRF 融合，参数与 knowledge/multi_stage.py 一致。"""

    scores: dict[UUID, float] = {}
    for channel, ids in channels.items():
        weight = _CHANNEL_WEIGHTS.get(channel, 1.0)
        for rank, version_id in enumerate(ids):
            scores[version_id] = scores.get(version_id, 0.0) + (
                weight * _RRF_SCALE / (_RRF_K + rank)
            )
    return scores
