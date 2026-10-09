"""ConsolidationJob —— 发现重复 / 近似重复 / 冲突并**只产提案**（04 计划 §6.4）。

三条不可谈判的约束：

1. **只产提案，不改正式记忆。** Job 全程不调用任何 repository 写入方法；
   它唯一的出口是 ``ProposalStore.create``。正式记忆只能由运维
   ``proposals approve``（经 ``versioning.apply_plan``）改变。
2. **候选生成按固定顺序缩小范围**（计划 §6.4），不是"两两全比"：
   同 tenant+scope+memory_type → subject 兼容 → active 且当前有效 →
   相似度门槛 → 来源/时态/内容哈希交叉校验。每一步都在减少比较量，
   最后一对一对比较只发生在小桶里。
3. **exact duplicate 是确定性的**，不经过任何模型。同一内容哈希就是同一内容，
   没有判断题可做——把它交给模型只会引入不确定性。

模型（``suggestion_provider``）的唯一作用是把"近似重复"合并成一句更好的
``merged_content``。它拿不到 repository，产出物也只能落进
``ProposalStore.suggested_action``（见 ``proposals.build_merge_candidate``：
其中只有 ``merged_content`` / ``subject_key`` 会被采纳）。**没有 provider 也能跑**：
退化为"取置信度更高、更长、更早的那条正文"，因为一条无法 apply 的提案等于没产。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from klonet_agent.config import MaintenanceConfig
from klonet_agent.memory.database import MemoryDatabase
from klonet_agent.memory.domain import MemoryRecord, MemoryType, Scope, Tenant, parse_subject_key
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.proposals import (
    ProposalStore,
    ProposalType,
    capture_source_fingerprints,
    ensure_same_tenant,
)
from klonet_agent.memory.maintenance.tenants import (
    default_tenants_provider, normalise_tenants, sweep_tenants,
)

__all__ = [
    "CandidatePair",
    "ConsolidationJob",
    "SuggestionRequest",
    "build_job",
    "candidate_pairs",
    "classify_pair",
    "token_similarity",
]


# --------------------------------------------------------------------------- #
# 相似度（确定性、可重放）
# --------------------------------------------------------------------------- #


def _tokens(record: MemoryRecord) -> set[str]:
    """正文 token 集合 + 元数据里的概念标记。

    concepts 是写记忆时就落库的确定性概念标记（见 ``evals/memory_cases.jsonl``
    的用法），用来在**没有向量**的情况下模拟"词面不同、语义相同"。
    只读 metadata，不调任何模型——评测必须能离线重放。
    """

    version = record.active_version
    if version is None:
        return set()
    tokens: set[str] = set()
    lexical = getattr(version, "lexical_text", None)
    if lexical:
        tokens.update(str(lexical).split())
    else:
        tokens.update(str(version.content or "").split())
    metadata = version.metadata or {}
    concepts = metadata.get("concepts") if isinstance(metadata, Mapping) else None
    if isinstance(concepts, (list, tuple)):
        tokens.update(f"#concept:{item}" for item in concepts if str(item).strip())
    return {token for token in tokens if token}


def token_similarity(left: MemoryRecord, right: MemoryRecord) -> float:
    """Jaccard 相似度。空集与空集视为 0（无内容可比较 ≠ 完全一致）。"""

    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _content_hash(record: MemoryRecord) -> str | None:
    version = record.active_version
    return getattr(version, "content_hash", None) if version is not None else None


def _record_time(
    record: MemoryRecord, source_lookup: "SourceLookup | None" = None
) -> datetime | None:
    """记录的"观察时间"：优先来源的 ``observed_at``，退化到版本时间。

    **``MemoryRecord`` 上没有 ``observed_at``**——那个字段长在 ``MemorySource``
    上，而 ``MemoryVersion`` 不 hydrate 来源。想拿真实观察时间就必须显式查
    来源（与 ``_source_keys`` 同一个坑）；查不到时退化 ``valid_from``，再退化
    记录的 ``created_at``。三条路都拿不到才返回 ``None``。
    """

    version = record.active_version
    if version is None:
        return None
    if source_lookup is not None:
        version_id = getattr(version, "id", None)
        if version_id:
            try:
                sources = source_lookup(str(version_id)) or ()
            except Exception:  # noqa: BLE001 - 来源查不到不该让分类失败
                sources = ()
            stamps = [
                source.observed_at
                for source in sources
                if getattr(source, "observed_at", None) is not None
            ]
            if stamps:
                return min(stamps)
    for attr in ("valid_from", "created_at"):
        value = getattr(version, attr, None)
        if isinstance(value, datetime):
            return value
    value = getattr(record, "created_at", None)
    return value if isinstance(value, datetime) else None


#: ``(version_id) -> Sequence[MemorySource]``。
#:
#: **必须显式注入**：``list_records`` / ``get_record`` 都不会 hydrate
#: ``MemoryVersion.sources``（按行构造时它是空的）。想用"共享来源"做交叉校验
#: 就必须由调用方给出查询函数，否则这一步会静默变成"永远没有共享来源"。
#: 阶段 5a 已经在 ``build_merge_candidate`` 上踩过同一个坑。
SourceLookup = Callable[[str], Sequence[Any]]


def _source_keys(
    record: MemoryRecord, source_lookup: "SourceLookup | None" = None
) -> frozenset[tuple[str, str]]:
    version = record.active_version
    if version is None:
        return frozenset()
    if source_lookup is None:
        return frozenset()
    version_id = getattr(version, "id", None)
    if not version_id:
        return frozenset()
    try:
        sources = source_lookup(str(version_id)) or ()
    except Exception:  # noqa: BLE001 - 来源查不到不该让分类失败
        return frozenset()
    return frozenset(
        (source.source_type.value, source.source_id) for source in sources
    )


def _entity(record: MemoryRecord) -> tuple[str, str]:
    """``(entity, attribute)``；解析不出来时退回整串（不让一个坏 key 炸掉整轮）。"""

    try:
        parsed = parse_subject_key(record.subject_key)
    except Exception:  # noqa: BLE001 - 历史遗留 key 不该让 Job 失败
        return (record.subject_key, "")
    return (parsed.entity or "", parsed.attribute or "")


# --------------------------------------------------------------------------- #
# 候选对与分类
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CandidatePair:
    """一对"值得看一眼"的记忆，以及确定性分类结论。"""

    left: MemoryRecord
    right: MemoryRecord
    proposal_type: ProposalType
    similarity: float
    reason_codes: tuple[str, ...] = ()

    @property
    def memory_ids(self) -> tuple[str, str]:
        return (str(self.left.id), str(self.right.id))


def classify_pair(
    left: MemoryRecord,
    right: MemoryRecord,
    *,
    similarity_threshold: float = 0.75,
    now: datetime | None = None,
    source_lookup: "SourceLookup | None" = None,
) -> CandidatePair | None:
    """确定性分类。返回 ``None`` 表示"这一对不值得提"。

    判定顺序（严格照计划 §6.4 的缩小顺序）：

    0. **entity 不同 → 直接 ``None``**。这是硬过滤，必须排在内容比较**之前**：
       同一句正文挂在两个 entity 下是合法的（alpha 与 beta 都用同一个版本号），
       按内容哈希判成"重复"是错的。
    1. 内容哈希相同 → ``NOOP``（同一内容，不需要任何动作）；
    2. ``subject_key`` 完全相同但内容不同 → ``CONFLICT``（同一事实两个说法）；
    3. 相似度 ≥ 门槛 → ``MERGE``（近似重复）。

    第 5 步"来源/时态交叉校验"不改变类型，只**加证据**：共享同一
    ``source_id``、``observed_at`` 接近都会写进 ``reason_codes``，让审批的人
    知道这条提案为什么被提出来。
    """

    moment = now or datetime.now(timezone.utc)

    # 步骤 2 是**硬过滤，必须排在内容比较之前**：subject 不兼容意味着"两句话说的
    # 不是同一件事"。同一句正文挂在两个 entity 下是合法的（例如 alpha 与 beta
    # 都用同一个版本号），此时按内容哈希判成重复是完全错误的。
    left_entity = _entity(left)[0]
    right_entity = _entity(right)[0]
    if not left_entity or left_entity != right_entity:
        return None

    left_hash, right_hash = _content_hash(left), _content_hash(right)
    similarity = token_similarity(left, right)

    reasons: list[str] = []
    shared = _source_keys(left, source_lookup) & _source_keys(right, source_lookup)
    if shared:
        reasons.append("shared_source_evidence")
    left_at = _record_time(left, source_lookup)
    right_at = _record_time(right, source_lookup)
    if left_at is not None and right_at is not None:
        if abs((left_at - right_at).total_seconds()) <= 3600:
            reasons.append("observed_close_in_time")

    if left_hash is not None and left_hash == right_hash:
        return CandidatePair(
            left=left,
            right=right,
            proposal_type=ProposalType.NOOP,
            similarity=1.0,
            reason_codes=("exact_duplicate", *reasons),
        )

    if left.subject_key == right.subject_key:
        return CandidatePair(
            left=left,
            right=right,
            proposal_type=ProposalType.CONFLICT,
            similarity=similarity,
            reason_codes=("same_subject_different_content", *reasons),
        )

    if similarity >= similarity_threshold:
        return CandidatePair(
            left=left,
            right=right,
            proposal_type=ProposalType.MERGE,
            similarity=similarity,
            reason_codes=("near_duplicate", *reasons),
        )

    # 相似但 entity 不同：不是同一事实，不提。反过来"entity 相同但不够像"
    # 也不提——相似度门槛就是用来挡这个的。
    return None


def candidate_pairs(
    records: Sequence[MemoryRecord],
    *,
    similarity_threshold: float = 0.75,
    bucket_limit: int = 200,
    now: datetime | None = None,
    source_lookup: "SourceLookup | None" = None,
) -> list[CandidatePair]:
    """按 §6.4 的顺序缩小范围后，只在桶内两两比较。

    桶键 = ``(scope, memory_type, entity)``：这正是"相同 tenant+scope+type"
    之后"相同或兼容 subject_key"的可执行形式——subject_key 兼容意味着
    entity 相同，只差 attribute。

    ``bucket_limit`` 是对最坏情况 ``O(n²)`` 的兜底：一个桶超过这个数量说明
    记忆已经严重重复，先处理完这一批再让下一轮继续（cursor 会推进）。
    """

    buckets: dict[tuple[Scope, MemoryType, str], list[MemoryRecord]] = {}
    for record in records:
        if record.status.value != "active":
            continue
        version = record.active_version
        if version is None:
            continue
        if version.valid_to is not None and version.valid_to <= (now or datetime.now(timezone.utc)):
            # 已经失效的版本不进候选：整理它们没有意义（过期归档负责）。
            continue
        key = (record.scope, record.memory_type, _entity(record)[0])
        buckets.setdefault(key, []).append(record)

    pairs: list[CandidatePair] = []
    for key in sorted(buckets, key=lambda item: (item[0].value, item[1].value, item[2])):
        bucket = sorted(buckets[key], key=lambda record: str(record.id))[:bucket_limit]
        for index, left in enumerate(bucket):
            for right in bucket[index + 1 :]:
                pair = classify_pair(
                    left,
                    right,
                    similarity_threshold=similarity_threshold,
                    now=now,
                    source_lookup=source_lookup,
                )
                if pair is not None:
                    pairs.append(pair)
    return pairs


# --------------------------------------------------------------------------- #
# 模型接口（结构化建议，无仓库权限）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SuggestionRequest:
    """交给模型看的**只读**快照。

    刻意只给正文与 subject_key：给更多（租户、来源 id、置信度）没有收益，
    只会扩大注入面。模型返回的 ``merged_content`` 会被
    ``proposals.build_merge_candidate`` 采纳，其余字段一律忽略。
    """

    proposal_type: ProposalType
    subject_key: str
    contents: tuple[str, ...]


#: 返回 ``{"merged_content": str, "subject_key": str | None}`` 或 ``None``。
SuggestionProvider = Callable[[SuggestionRequest], Mapping[str, Any] | None]


def _fallback_merge_content(
    pair: CandidatePair, source_lookup: "SourceLookup | None" = None
) -> str:
    """没有模型时的确定性合并：置信度高者优先，其次更长，其次更早。

    这三条排序都是刻意的：置信度高说明证据更足；等置信度下更长的描述通常
    信息更全；再等长时更早的版本才是"原始记录"。全部可复现，不掷骰子。
    """

    def _rank(record: MemoryRecord) -> tuple[float, int, float, str]:
        version = record.active_version
        length = len(str(version.content or "")) if version is not None else 0
        observed = _record_time(record, source_lookup)
        # 越早的观察时间排越前 → 取负的时间戳。
        stamp = -observed.timestamp() if observed is not None else 0.0
        return (-float(record.confidence), -length, stamp, str(record.id))

    ordered = sorted((pair.left, pair.right), key=_rank)
    winner = ordered[0]
    version = winner.active_version
    return str(version.content if version is not None else "")


# --------------------------------------------------------------------------- #
# Job
# --------------------------------------------------------------------------- #


class ConsolidationJob:
    """生成整理提案；实现 ``MaintenanceJob``。"""

    name = "consolidation"

    def __init__(
        self,
        *,
        database: MemoryDatabase,
        tenants_provider: Callable[[], Sequence[Tenant]] | None = None,
        store_factory: Callable[[Tenant], ProposalStore] | None = None,
        repository_factory: Callable[[Tenant], Any] | None = None,
        suggestion_provider: SuggestionProvider | None = None,
        similarity_threshold: float = 0.75,
        max_records_per_tenant: int = 500,
        bucket_limit: int = 200,
        max_pairs_per_tenant: int = 200,
        max_tenants_per_run: int = 50,
        max_seconds_per_run: float = 60.0,
        include_shared_ops: bool = True,
        clock: Callable[[], datetime] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ):
        if not 0.0 < similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold 必须在 (0, 1] 区间")
        if max_records_per_tenant <= 0:
            raise ValueError("max_records_per_tenant 必须为正整数")
        if bucket_limit <= 0:
            raise ValueError("bucket_limit 必须为正整数")
        if max_pairs_per_tenant <= 0:
            raise ValueError("max_pairs_per_tenant 必须为正整数")
        if max_tenants_per_run <= 0:
            raise ValueError("max_tenants_per_run 必须为正整数")
        if max_seconds_per_run <= 0:
            raise ValueError("max_seconds_per_run 必须为正数")
        self._database = database
        self._store_factory = store_factory
        self._repository_factory = repository_factory
        self._suggestion_provider = suggestion_provider
        self._similarity_threshold = float(similarity_threshold)
        self._max_records = int(max_records_per_tenant)
        self._bucket_limit = int(bucket_limit)
        self._max_pairs = int(max_pairs_per_tenant)
        self._max_tenants = int(max_tenants_per_run)
        self._max_seconds = float(max_seconds_per_run)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._should_stop = should_stop
        self._tenants_provider = tenants_provider or default_tenants_provider(
            database, include_shared_ops=include_shared_ops
        )

    # ------------------------------------------------------------- 依赖 --

    def _list_tenants(self) -> list[Tenant]:
        return normalise_tenants(self._tenants_provider())

    def _repository_for(self, tenant: Tenant) -> Any:
        if self._repository_factory is not None:
            return self._repository_factory(tenant)
        from klonet_agent.memory.postgres import PostgresMemoryRepository

        return PostgresMemoryRepository(self._database, tenant)

    def _store_for(self, tenant: Tenant) -> ProposalStore:
        if self._store_factory is not None:
            return self._store_factory(tenant)
        return ProposalStore(self._database, tenant)

    # ------------------------------------------------------------- 运行 --

    def run(self, context: JobContext, cursor: str | None) -> JobResult:
        tenants = self._list_tenants()
        if not tenants:
            return JobResult(details=("no_tenants：没有可调度的租户",))

        budget_end = self._clock().timestamp() + self._max_seconds
        if context.deadline is not None:
            budget_end = min(budget_end, context.deadline.timestamp())
        batch_end = datetime.fromtimestamp(budget_end, tz=timezone.utc)

        def _handler(tenant: Tenant) -> tuple[int, int, int, str | None] | None:
            return self._sweep_tenant(tenant, context, batch_end)

        outcome = sweep_tenants(
            tenants,
            cursor,
            handler=_handler,
            clock=self._clock,
            batch_deadline=batch_end,
            should_stop=self._should_stop,
            max_tenants=self._max_tenants,
        )
        summary = (
            f"tenants={outcome.tenants_done} scanned={outcome.scanned} "
            f"proposals={outcome.changed}"
        )
        return JobResult(
            scanned=outcome.scanned,
            changed=0,
            proposed=outcome.changed,
            failed=outcome.failed,
            next_cursor=outcome.next_cursor,
            details=(summary, *outcome.notes[:8]),
        )

    def _sweep_tenant(
        self, tenant: Tenant, context: JobContext, batch_end: datetime
    ) -> tuple[int, int, int, str | None]:
        repository = self._repository_for(tenant)
        store = self._store_for(tenant)
        now = self._clock()
        records = repository.list_records(limit=self._max_records, status="active")
        if context.dry_run:
            pairs = candidate_pairs(
                records,
                similarity_threshold=self._similarity_threshold,
                bucket_limit=self._bucket_limit,
                now=now,
                source_lookup=repository.list_sources,
            )
            counts: dict[str, int] = {}
            for pair in pairs[: self._max_pairs]:
                counts[pair.proposal_type.value] = counts.get(pair.proposal_type.value, 0) + 1
            return (
                len(records),
                0,
                0,
                f"{_label(tenant)}: dry_run candidates={len(pairs)} "
                f"by_type={','.join(f'{k}={v}' for k, v in sorted(counts.items())) or '-'}",
            )

        pairs = candidate_pairs(
            records,
            similarity_threshold=self._similarity_threshold,
            bucket_limit=self._bucket_limit,
            now=now,
            source_lookup=repository.list_sources,
        )

        created = 0
        deduped = 0
        by_type: dict[str, int] = {}
        for pair in pairs[: self._max_pairs]:
            ids = pair.memory_ids
            if not self._same_scope(ids, records):
                # 双保险：跨 scope 组合在这里就被拒（数据库层还有租户谓词）。
                continue
            proposed = self._create_proposal(store, repository, pair, tenant)
            if proposed is None:
                continue
            if proposed.created:
                created += 1
                by_type[pair.proposal_type.value] = by_type.get(pair.proposal_type.value, 0) + 1
            else:
                deduped += 1
        note = None
        if created or deduped:
            note = (
                f"{_label(tenant)}: created={created} deduped={deduped} "
                f"by_type={','.join(f'{k}={v}' for k, v in sorted(by_type.items())) or '-'}"
            )
        return (len(records), created, 0, note)

    def _same_scope(self, ids: Sequence[str], records: Sequence[MemoryRecord]) -> bool:
        by_id = {str(record.id): record for record in records}
        sample = [by_id[item] for item in ids if item in by_id]
        if len(sample) < 2:
            return False
        try:
            ensure_same_tenant([(record.user_id, record.project_id) for record in sample])
        except Exception:  # noqa: BLE001 - ProposalInvalidError
            return False
        return True

    def _create_proposal(
        self,
        store: ProposalStore,
        repository: Any,
        pair: CandidatePair,
        tenant: Tenant,
    ) -> Any:
        source_fingerprints = capture_source_fingerprints(repository, list(pair.memory_ids))
        suggested: dict[str, Any] = {}
        reasons = list(pair.reason_codes)
        if pair.proposal_type in (ProposalType.MERGE, ProposalType.SUPERSEDE):
            suggestion = self._suggest(pair)
            if suggestion is not None:
                suggested.update(
                    {key: value for key, value in suggestion.items() if key in ("merged_content", "subject_key")}
                )
                reasons.append("model_suggestion")
            if not str(suggested.get("merged_content") or "").strip():
                # 没有模型也必须给出一个能落地的正文，否则这条提案永远批不动。
                suggested["merged_content"] = _fallback_merge_content(
                    pair, source_lookup=getattr(repository, "list_sources", None)
                )
                reasons.append("deterministic_merge")
        return store.create(
            proposal_type=pair.proposal_type,
            source_memory_ids=list(pair.memory_ids),
            scope=pair.left.scope,
            suggested_action=suggested,
            reason_codes=reasons,
            evidence_refs=[
                {"similarity": round(pair.similarity, 4)},
                {"subject_keys": [pair.left.subject_key, pair.right.subject_key]},
            ],
            policy_version="consolidation-v1",
            source_fingerprints=source_fingerprints,
        )

    def _suggest(self, pair: CandidatePair) -> Mapping[str, Any] | None:
        if self._suggestion_provider is None:
            return None
        request = SuggestionRequest(
            proposal_type=pair.proposal_type,
            subject_key=pair.left.subject_key,
            contents=tuple(
                str(record.active_version.content if record.active_version else "")
                for record in (pair.left, pair.right)
            ),
        )
        try:
            return self._suggestion_provider(request)
        except Exception:  # noqa: BLE001 - 建议失败不影响提案本身
            return None


def _label(tenant: Tenant) -> str:
    return f"{tenant.user_id}/{tenant.project_id or '-'}"


def build_job(
    *, database: MemoryDatabase, config: MaintenanceConfig
) -> ConsolidationJob:
    return ConsolidationJob(database=database)
