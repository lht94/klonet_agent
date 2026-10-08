"""Markdown 记忆一次性迁移到 PostgreSQL（计划 §8、阶段 6）。

核心是**四态状态机**：``legacy → shadow → compare → cutover``，任何时刻只有一个
权威，绝不双写（双写必然漂移，漂移之后没人知道该信哪一份）。

三个硬约束：

1. **只读扫描。** 迁移不修改、不删除任何源文件——`MEMORY.md` / `USER.md` /
   日记都是用户资产，导出能力一直保留（计划 §8 第 9 条）。
2. **幂等。** 幂等键是 ``migrate:<相对路径>#<sha256>:<序号>``，而 subject_key 由
   **条目的内容哈希**派生。所以"同一内容重复迁移"会撞上同一个 subject，
   consolidation 判 NOOP——重复执行不产生第二条记忆。
3. **不留半成品。** 解析全在内存里做，写入交给 ``MemoryWritePipeline``（它自己
   保证单条事务与区间幂等）。一个文件解析失败就整份跳过并记原因，不写半条。

迁入的条目仍然要过同一套 policy：敏感脱敏、作用域校验、来源白名单。
迁移**不是**特权通道——它只是另一个"候选提供者"。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple

from klonet_agent.memory.candidate_extractor import CandidateProposal, RawSource
from klonet_agent.memory.domain import (
    MemoryType,
    Scope,
    SourceType,
    Tenant,
    WriteDecision,
    build_subject_key,
)
from klonet_agent.memory.write_policy import TurnContext

__all__ = [
    "MarkdownMemoryScanner",
    "MemoryMigrator",
    "MigrationPhase",
    "MigrationReport",
    "MigrationState",
    "MigrationStateStore",
    "ParsedItem",
    "SourceDocument",
    "TransitionError",
    "allowed_next_phases",
    "split_markdown_items",
]


# --------------------------------------------------------------------------- #
# 四态状态机
# --------------------------------------------------------------------------- #


class MigrationPhase(str, Enum):
    """迁移的权威归属。

    - ``legacy``：Markdown 是权威（现状）。
    - ``shadow``：数据库**影子写入**，回答仍然只读 Markdown。这一步只验证
      "同一条内容在库里能不能正确落下来"，不影响任何回答。
    - ``compare``：双读对比（数据库 vs Markdown），回答仍以 Markdown 为准。
      差异要能解释，否则不能进 cutover。
    - ``cutover``：PostgreSQL 唯一权威。回滚只能回到最近一次只读快照，
      **不恢复双写**（计划 §8 第 8 条）。
    """

    LEGACY = "legacy"
    SHADOW = "shadow"
    COMPARE = "compare"
    CUTOVER = "cutover"


# 只允许相邻推进；每一步都可以退回去，唯一例外是 cutover 只退回 compare
# （回到 shadow/legacy 意味着"重新回到读 Markdown 回答"，那需要在 compare 里
# 先确认库里的数据完整，不能一步跳回去）。
_PHASE_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    MigrationPhase.LEGACY.value: (MigrationPhase.SHADOW.value,),
    MigrationPhase.SHADOW.value: (
        MigrationPhase.COMPARE.value,
        MigrationPhase.LEGACY.value,
    ),
    MigrationPhase.COMPARE.value: (
        MigrationPhase.CUTOVER.value,
        MigrationPhase.SHADOW.value,
    ),
    MigrationPhase.CUTOVER.value: (MigrationPhase.COMPARE.value,),
}


class TransitionError(RuntimeError):
    """非法的状态迁移。"""


def allowed_next_phases(phase: str) -> tuple[str, ...]:
    """当前阶段允许迁移到哪些阶段。"""

    return _PHASE_TRANSITIONS.get(str(phase or "").strip(), ())


def _validate_transition(current: str, target: str) -> None:
    if current == target:
        return
    if target not in allowed_next_phases(current):
        raise TransitionError(
            f"不允许从 {current!r} 直接切到 {target!r}；"
            f"允许的目标是 {allowed_next_phases(current)}"
        )


@dataclass(frozen=True)
class MigrationState:
    """迁移状态与"哪些源文件已经迁过"。"""

    phase: str = MigrationPhase.LEGACY.value
    updated_at: str = ""
    # rel_path -> sha256：源文件没变就跳过，省一次解析与写入。
    source_hashes: Mapping[str, str] = field(default_factory=dict)
    last_report: Mapping[str, Any] | None = None
    # cutover 回滚用的只读快照（导出文件路径）。没有快照就不允许 cutover。
    snapshot_export: str | None = None


class MigrationStateStore:
    """把迁移状态落在 JSON 文件里。

    刻意用文件而不是数据库：**迁移状态必须能在数据库不可用时读出来**，
    否则 rollback 检查会依赖于它正要保护的那个系统。
    """

    def __init__(self, path: Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> MigrationState:
        if not self._path.exists():
            return MigrationState()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 状态文件坏了就当作"没迁移过"：这会让下一次 run 重跑一遍迁移，
            # 而幂等键与 subject 推导保证重跑不会产生重复记忆。
            return MigrationState()
        if not isinstance(payload, dict):
            return MigrationState()
        hashes = payload.get("source_hashes")
        return MigrationState(
            phase=str(payload.get("phase") or MigrationPhase.LEGACY.value),
            updated_at=str(payload.get("updated_at") or ""),
            source_hashes=dict(hashes) if isinstance(hashes, Mapping) else {},
            last_report=(
                payload.get("last_report")
                if isinstance(payload.get("last_report"), Mapping)
                else None
            ),
            snapshot_export=(
                str(payload["snapshot_export"])
                if payload.get("snapshot_export")
                else None
            ),
        )

    def save(self, state: MigrationState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "phase": state.phase,
            "updated_at": state.updated_at or _now_iso(),
            "source_hashes": dict(state.source_hashes),
            "last_report": state.last_report,
            "snapshot_export": state.snapshot_export,
        }
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def transition(self, target: str, *, snapshot_export: str | None = None) -> MigrationState:
        """切到目标阶段，并在进入 cutover 前检查回滚手段是否就绪。"""

        current = self.load()
        _validate_transition(current.phase, target)
        if (
            target == MigrationPhase.CUTOVER.value
            and not (snapshot_export or current.snapshot_export)
        ):
            raise TransitionError(
                "进入 cutover 前必须提供只读快照（snapshot_export）："
                "cutover 的回滚只能回到快照，不恢复双写"
            )
        updated = MigrationState(
            phase=target,
            updated_at=_now_iso(),
            source_hashes=current.source_hashes,
            last_report=current.last_report,
            snapshot_export=snapshot_export or current.snapshot_export,
        )
        self.save(updated)
        return updated


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #


_SESSION_MEMORY_RE = re.compile(r"^sessions/(?P<user>[^/]+)/(?P<project>[^/]+)/MEMORY\.md$")
_SESSION_EPISODE_RE = re.compile(
    r"^sessions/(?P<user>[^/]+)/(?P<project>[^/]+)/(?P<date>\d{4}-\d{2}-\d{2})\.md$"
)
_USER_PROFILE_RE = re.compile(r"^users/(?P<user>[^/]+)/USER\.md$")
_SHARED_DAILY_RE = re.compile(r"^shared/ops/(?P<date>\d{4}-\d{2}-\d{2})\.md$")
_SHARED_BASELINE_RE = re.compile(r"^shared/ops/BASELINE\.md$")

# 这些文件是运行期产物，不是用户记忆。
_SKIP_NAMES = frozenset({"history.jsonl", ".migration_state.json"})


@dataclass(frozen=True)
class SourceDocument:
    """一份待迁移的 Markdown 源文件。"""

    rel_path: str
    kind: str
    text: str
    sha256: str
    user_id: str
    project_id: str | None
    observed_at: datetime


class MarkdownMemoryScanner:
    """只读扫描记忆目录，按路径规则给每份文件定性。"""

    def __init__(self, root: Path):
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def scan(self) -> list[SourceDocument]:
        if not self._root.exists():
            return []
        found: list[SourceDocument] = []
        for path in sorted(self._root.rglob("*.md")):
            if path.name in _SKIP_NAMES:
                continue
            rel = path.relative_to(self._root).as_posix()
            classified = _classify(rel)
            if classified is None:
                continue
            kind, user_id, project_id, observed_at = classified
            try:
                raw = path.read_bytes()
            except OSError:
                # 读不了的文件跳过：迁移不该因为一个坏文件整体失败。
                continue
            text = raw.decode("utf-8", errors="replace")
            if not text.strip():
                continue
            found.append(
                SourceDocument(
                    rel_path=rel,
                    kind=kind,
                    text=text,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    user_id=user_id,
                    project_id=project_id,
                    observed_at=observed_at,
                )
            )
        return found


def _classify(rel_path: str) -> tuple[str, str, str | None, datetime] | None:
    """按路径判定 (kind, user_id, project_id, observed_at)。不认识的路径返回 None。"""

    match = _SESSION_MEMORY_RE.match(rel_path)
    if match:
        return (
            "session_memory",
            match.group("user"),
            match.group("project"),
            _file_time_from_rel(rel_path),
        )
    match = _SESSION_EPISODE_RE.match(rel_path)
    if match:
        return (
            "session_episode",
            match.group("user"),
            match.group("project"),
            _date_from(match.group("date")),
        )
    match = _USER_PROFILE_RE.match(rel_path)
    if match:
        return ("user_profile", match.group("user"), None, _file_time_from_rel(rel_path))
    match = _SHARED_DAILY_RE.match(rel_path)
    if match:
        return (
            "shared_ops_daily",
            _SHARED_USER_ID,
            None,
            _date_from(match.group("date")),
        )
    if _SHARED_BASELINE_RE.match(rel_path):
        return ("shared_ops_baseline", _SHARED_USER_ID, None, _file_time_from_rel(rel_path))
    return None


# 共享 Ops 记忆挂在哪个 user_id 上：它们对所有租户可见是靠 RLS 的
# ``shared_ops`` 放行（策略挂在 klonet_ops 角色上），不靠 user_id 巧合。
_SHARED_USER_ID = "shared"


def _date_from(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:  # pragma: no cover - 正则已保证格式
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _file_time_from_rel(rel_path: str) -> datetime:
    """没有日期信息时，用路径里的日期段，再退回"现在"。"""

    match = re.search(r"(\d{4}-\d{2}-\d{2})", rel_path)
    if match:
        return _date_from(match.group(1))
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


_BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+(?P<body>.+)$")
_MIN_ITEM_CHARS = 8


def split_markdown_items(text: str) -> list[str]:
    """把一份 Markdown 记忆拆成可独立成立的条目。

    优先取列表项——Markdown 记忆里的原子事实通常就是这么写的（``- xxx``）；
    缩进的续行并入上一条。没有列表项时按"空行 / 标题"分段。
    标题行本身不进条目：它们只是分类，单独拿出来没有信息量。
    """

    items: list[str] = []
    current: list[str] = []
    in_list = False

    def flush() -> None:
        if current:
            joined = "\n".join(current).strip()
            if joined:
                items.append(joined)
            current.clear()

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            in_list = False
            continue
        if stripped.startswith("#"):
            flush()
            in_list = False
            continue
        bullet = _BULLET_RE.match(stripped)
        if bullet:
            flush()
            in_list = True
            current.append(bullet.group("body").strip())
            continue
        if in_list and raw[:1] in {" ", "\t"}:
            current.append(stripped)
            continue
        flush()
        in_list = False
        current.append(stripped)

    flush()
    return [item for item in items if len(item) >= _MIN_ITEM_CHARS]


@dataclass(frozen=True)
class ParsedItem:
    """一条解析出来的候选，连同它的来源与幂等键。"""

    proposal: CandidateProposal
    source_type: str
    source_id: str
    source_excerpt: str
    source_rel_path: str
    subject_key: str
    index: int

    @property
    def idempotency_key(self) -> str:
        return f"migrate:{self.source_rel_path}#{self.source_id.rsplit('#', 1)[-1]}:{self.index}"


_KIND_PLAN: Mapping[str, tuple[str, str]] = {
    # kind -> (memory_type, scope)
    "session_memory": (MemoryType.FACT.value, Scope.PROJECT.value),
    "user_profile": (MemoryType.PREFERENCE.value, Scope.USER.value),
    "session_episode": (MemoryType.EPISODE.value, Scope.PROJECT.value),
    "shared_ops_daily": (MemoryType.EPISODE.value, Scope.SHARED_OPS.value),
    "shared_ops_baseline": (MemoryType.FACT.value, Scope.SHARED_OPS.value),
}


def parse_document(document: SourceDocument) -> list[ParsedItem]:
    """把一份源文件解析成候选条目。"""

    plan = _KIND_PLAN.get(document.kind)
    if plan is None:  # pragma: no cover - 扫描阶段已过滤
        return []
    memory_type, scope = plan
    items = split_markdown_items(document.text)
    parsed: list[ParsedItem] = []
    source_id = f"md:{document.rel_path}#{document.sha256[:12]}"
    for index, content in enumerate(items):
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
        fields = _subject_fields(memory_type, scope, document, digest, index)
        parsed.append(
            ParsedItem(
                proposal=CandidateProposal(
                    memory_type=memory_type,
                    scope=scope,
                    content=content,
                    subject=fields.entity,
                    attribute=fields.attribute,
                    episode_id=fields.episode_id,
                    importance="medium",
                    confidence=_confidence_for(document.kind),
                    verified=False,
                    proposed_decision="add",
                    sources=(
                        RawSource(
                            source_type=SourceType.JOURNAL.value,
                            source_id=source_id,
                            excerpt=content[:200],
                        ),
                    ),
                    raw_index=index,
                ),
                source_type=SourceType.JOURNAL.value,
                source_id=source_id,
                source_excerpt=content[:200],
                source_rel_path=document.rel_path,
                subject_key=fields.subject_key,
                index=index,
            )
        )
    return parsed


class _SubjectFields(NamedTuple):
    """一条迁移条目的 subject 三要素。

    ``subject_key`` 直接由 ``build_subject_key`` 算，而不是在这里手拼——策略层用的
    就是它，两处口径必须完全一致，否则迁移写进去的记录会在后续 consolidation 里
    被当成"另一个 subject"，幂等就失效了。
    """

    subject_key: str
    entity: str
    attribute: str | None
    episode_id: str


def _subject_fields(
    memory_type: str, scope: str, document: SourceDocument, digest: str, index: int
) -> _SubjectFields:
    """由**内容哈希**派生 subject。

    这是迁移幂等的关键：同一条内容再次迁移会落到同一个 subject 上，
    consolidation 一看内容相同就判 NOOP，而不是插第二条记忆。
    情景记忆例外——它按事件身份去重，同一段文字在不同日记文件里是两次经历。
    """

    memory_type_enum = MemoryType(memory_type)
    scope_enum = Scope(scope)
    if memory_type_enum is MemoryType.EPISODE:
        episode_id = (
            f"md-shared-{digest}-{index}"
            if scope_enum is Scope.SHARED_OPS
            else f"md-{document.project_id or 'unknown'}-{digest}-{index}"
        )
        return _SubjectFields(
            subject_key=build_subject_key(
                memory_type=memory_type_enum, episode_id=episode_id
            ),
            entity="",
            attribute=None,
            episode_id=episode_id,
        )

    entity = document.project_id or document.user_id
    attribute = f"migrated_{digest}"
    return _SubjectFields(
        subject_key=build_subject_key(
            memory_type=memory_type_enum,
            scope=scope_enum,
            entity=entity,
            attribute=attribute,
        ),
        entity=entity,
        attribute=attribute,
        episode_id="",
    )


def _confidence_for(kind: str) -> float:
    """迁移内容的置信度。

    统一低于"用户当轮明确陈述"：文件里的话可能已经过时，而且没有当轮工具验证。
    情景记忆再低一档——它本来就是"某次经历"，不是结论。
    """

    if kind in {"session_episode", "shared_ops_daily"}:
        return 0.45
    if kind == "shared_ops_baseline":
        return 0.55
    return 0.6


# --------------------------------------------------------------------------- #
# 迁移编排
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MigrationReport:
    """一次迁移（或预览）的结果，也是"迁移前后数量和内容抽样核对报告"的载体。"""

    dry_run: bool
    documents: int = 0
    skipped_unchanged: int = 0
    items: int = 0
    accepted: int = 0
    rejected: int = 0
    noop: int = 0
    failures: tuple[str, ...] = ()
    rejection_reasons: Mapping[str, int] = field(default_factory=dict)
    by_kind: Mapping[str, int] = field(default_factory=dict)
    samples: tuple[Mapping[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_payload(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "documents": self.documents,
            "skipped_unchanged": self.skipped_unchanged,
            "items": self.items,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "noop": self.noop,
            "failures": list(self.failures),
            "rejection_reasons": dict(self.rejection_reasons),
            "by_kind": dict(self.by_kind),
            "samples": [dict(item) for item in self.samples],
        }


class MemoryMigrator:
    """扫描 → 解析 → 经 write pipeline 幂等写入。

    ``pipeline_factory`` 而不是单个 pipeline：仓库在构造时就绑定了租户
    （``PostgresMemoryRepository`` 的设计），而一份记忆目录里同时存在多个
    ``(user, project)``。所以按租户惰性建、并缓存 pipeline——不这么做就只能
    迁移一个租户。
    """

    def __init__(
        self,
        pipeline_factory: Any,
        *,
        root: Path,
        state_store: MigrationStateStore,
        shared_tenant: Tenant | None = None,
        sample_limit: int = 5,
    ):
        self._pipeline_factory = pipeline_factory
        self._pipelines: dict[Tenant, Any] = {}
        self._scanner = MarkdownMemoryScanner(root)
        self._state_store = state_store
        self._shared_tenant = shared_tenant or Tenant(user_id=_SHARED_USER_ID)
        self._sample_limit = max(0, int(sample_limit))

    def _tenant_for(self, document: SourceDocument) -> Tenant:
        if _KIND_PLAN[document.kind][1] == Scope.SHARED_OPS.value:
            return self._shared_tenant
        return Tenant(user_id=document.user_id, project_id=document.project_id)

    def _pipeline_for(self, tenant: Tenant) -> Any:
        pipeline = self._pipelines.get(tenant)
        if pipeline is None:
            pipeline = self._pipeline_factory(tenant)
            self._pipelines[tenant] = pipeline
        return pipeline

    # ------------------------------------------------------------- 只读 --

    def preview(self) -> MigrationReport:
        """只读扫描并生成预览报告（不写库、不改状态）。"""

        documents = self._scanner.scan()
        items = [item for document in documents for item in parse_document(document)]
        return MigrationReport(
            dry_run=True,
            documents=len(documents),
            items=len(items),
            by_kind=dict(Counter(document.kind for document in documents)),
            samples=tuple(
                {
                    "source": item.source_rel_path,
                    "type": item.proposal.memory_type,
                    "scope": item.proposal.scope,
                    "subject_key": item.subject_key,
                    "content": item.proposal.content[:120],
                }
                for item in items[: self._sample_limit]
            ),
        )

    # ------------------------------------------------------------- 写入 --

    def run(self, *, apply: bool = False) -> MigrationReport:
        """执行迁移。``apply=False`` 等价于 preview（但会走完整解析）。"""

        state = self._state_store.load()
        documents = self._scanner.scan()
        by_kind: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        failures: list[str] = []
        samples: list[dict[str, Any]] = []
        accepted = rejected = noop = items_total = 0
        skipped = 0

        for document in documents:
            by_kind[document.kind] += 1
            if apply and state.source_hashes.get(document.rel_path) == document.sha256:
                # 源文件没变：跳过。这是幂等的第一层（第二层是 subject/幂等键）。
                skipped += 1
                continue
            try:
                parsed = parse_document(document)
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不拖垮整批
                failures.append(f"{document.rel_path}: {type(exc).__name__}: {exc}")
                continue

            items_total += len(parsed)
            tenant = self._tenant_for(document)
            for item in parsed:
                if not apply:
                    accepted += 1
                    continue
                try:
                    outcome = self._pipeline_for(tenant).process_proposal(
                        item.proposal,
                        self._context_for(document, item, tenant),
                        source_event_range={
                            "start": item.source_id,
                            "end": item.source_id,
                            "origin": "migration",
                        },
                        idempotency_key=item.idempotency_key,
                    )
                except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                    failures.append(
                        f"{document.rel_path}#{item.index}: {type(exc).__name__}: {exc}"
                    )
                    continue
                if outcome.wrote:
                    accepted += 1
                elif outcome.decision is WriteDecision.NOOP:
                    noop += 1
                else:
                    rejected += 1
                    # 拒绝原因优先取策略层的 reason_code（它比决策名更具体）。
                    code = getattr(
                        getattr(outcome, "verdict", None), "reason_code", None
                    )
                    reasons[str(getattr(code, "value", None) or outcome.decision)] += 1
                if len(samples) < self._sample_limit:
                    decision = outcome.decision or WriteDecision.REJECT
                    samples.append(
                        {
                            "source": document.rel_path,
                            "subject_key": item.subject_key,
                            "decision": decision.value,
                            "reason": str(outcome.reason or "")[:160],
                        }
                    )

        if apply and not failures:
            # 只有整批没有失败才推进源文件台账：有失败时下次重跑会重试这些文件，
            # 而不是把它们误标成"已迁移"。
            new_hashes = dict(state.source_hashes)
            for document in documents:
                new_hashes[document.rel_path] = document.sha256
            self._state_store.save(
                MigrationState(
                    phase=state.phase,
                    updated_at=_now_iso(),
                    source_hashes=new_hashes,
                    last_report={
                        "items": items_total,
                        "accepted": accepted,
                        "rejected": rejected,
                        "noop": noop,
                    },
                    snapshot_export=state.snapshot_export,
                )
            )

        return MigrationReport(
            dry_run=not apply,
            documents=len(documents),
            skipped_unchanged=skipped,
            items=items_total,
            accepted=accepted,
            rejected=rejected,
            noop=noop,
            failures=tuple(failures),
            rejection_reasons=dict(reasons),
            by_kind=dict(by_kind),
            samples=tuple(samples),
        )

    # ------------------------------------------------------------- 内部 --

    def _context_for(
        self, document: SourceDocument, item: ParsedItem, tenant: Tenant
    ) -> TurnContext:
        """迁移的写入上下文。

        ``allowed_sources`` 只放**这一份文件**声明的来源：迁移不能用"我在迁移"
        当借口引用别的来源。这条白名单与运行时管线是同一个机制。
        """

        return TurnContext(
            tenant=tenant,
            observed_at=document.observed_at,
            allowed_sources=frozenset({(item.source_type, item.source_id)}),
            project_id=document.project_id,
            allow_shared_ops=tenant == self._shared_tenant,
        )


# --------------------------------------------------------------------------- #
# 核对报告
# --------------------------------------------------------------------------- #


def render_report(report: MigrationReport, *, phase: str) -> str:
    """把报告渲染成 Markdown，便于人核对迁移前后的数量与抽样。"""

    lines = [
        f"# Markdown 记忆迁移报告（阶段 {phase}）",
        "",
        f"- 模式：{'预览（未写库）' if report.dry_run else '实际写入'}",
        f"- 扫描文件：{report.documents}（跳过未变化：{report.skipped_unchanged}）",
        f"- 解析条目：{report.items}",
        f"- 接受：{report.accepted}｜NOOP：{report.noop}｜拒绝：{report.rejected}",
        f"- 失败：{len(report.failures)}",
    ]
    if report.by_kind:
        lines.append("")
        lines.append("## 按来源类型")
        for kind, count in sorted(report.by_kind.items()):
            lines.append(f"- {kind}: {count}")
    if report.rejection_reasons:
        lines.append("")
        lines.append("## 拒绝原因")
        for reason, count in sorted(report.rejection_reasons.items()):
            lines.append(f"- {reason}: {count}")
    if report.samples:
        lines.append("")
        lines.append("## 抽样")
        for sample in report.samples:
            rendered = "｜".join(f"{key}={value}" for key, value in sample.items())
            lines.append(f"- {rendered}")
    if report.failures:
        lines.append("")
        lines.append("## 失败明细")
        for failure in report.failures:
            lines.append(f"- {failure}")
    lines.append("")
    return "\n".join(lines)


def iter_source_documents(root: Path) -> Iterable[SourceDocument]:
    """便捷入口：只扫描不解析。"""

    return MarkdownMemoryScanner(root).scan()
