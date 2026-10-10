"""有界记忆注入包（MemoryPack）。

对应计划 §6.7 与阶段 5 清单。三件事：

1. **固定渲染格式。** 每条记忆都渲染 memory_id / 类型与作用域 / 正文 /
   有效时间与置信度 / 来源引用。格式固定意味着模型与评测可以稳定断言，也意味着
   不会在某次改动里悄悄丢掉"这是线索不是事实"的说明。
2. **两级上限，token 优先。** 条数是候选上限（偏好 2 / 事实 4 / 经历 3），
   token 是最终硬约束。两者冲突时以 token 为准，**整条淘汰**最低分的记忆——
   先相似历史经历，再项目事实，用户偏好最后。截断单条结构会让模型看到残缺条目
   （只有 id 没有来源），比少给一条更糟。
3. **它只是证据。** MemoryPack 渲染成 evidence 消息，绝不进 system 区。检索到的
   记忆是与当前任务相关的历史信息，不拥有系统规则的优先级。

另外两个刻意的取舍：

- **来源只渲染标识，不渲染摘录。** ``source_excerpt`` 是整条记忆里唯一可能带敏感
  内容的部分（它直接引用了原始事件），而"是哪些证据支持这条"用 ``rows-12`` 这类
  标识就能说清。少渲染一段，就少一条绕开写入侧脱敏的旁路。
- **正文超长时截断并显式标注，结构永不截断。** 丢弃整条会损失最相关的记忆，
  保留完整正文又会让一条记忆吃掉整个预算。所以只有正文被截，且条目里
  ``正文已截断`` 是模型看得见的。
- **淘汰组合分 = 融合分 × 时间权重。** 同类型内竞争时，旧观察被打折：
  fact 半衰期 90 天、episode 180 天，preference 不衰减（偏好没有"变旧"的
  语义，衰减会误伤最有复用价值的记忆）。时间是折扣不是加分——"新"不能让
  不相关的记忆入选（score=0 乘任何权重仍是 0）。缺 observed_at 的条目按
  1.0 处理，不因数据缺失受罚。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from klonet_agent.context.tokens import estimate_tokens
from klonet_agent.memory.domain import MemoryHit, MemoryType

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注
    from klonet_agent.memory.retriever import MemoryRetrievalReport

__all__ = [
    "DEFAULT_TYPE_LIMITS",
    "MemoryPack",
    "MemoryPackBuilder",
    "MemoryPackDrop",
    "MemoryPackEntry",
    "_recency_weight_for",
]


# 计划 §6.7 的「候选上限」：相关用户偏好最多 2、项目事实 4、历史经历 3。
DEFAULT_TYPE_LIMITS: Mapping[str, int] = {
    MemoryType.PREFERENCE.value: 2,
    MemoryType.FACT.value: 4,
    MemoryType.EPISODE.value: 3,
}

# 渲染顺序（人读的顺序：先说"你是谁"，再说"项目是什么"，最后说"以前发生过什么"）。
_SECTION_ORDER = (
    MemoryType.PREFERENCE.value,
    MemoryType.FACT.value,
    MemoryType.EPISODE.value,
)

# 淘汰顺序：先丢最不具复用价值的相似经历，再丢项目事实，用户偏好最后丢。
# 偏好通常最短、最稳定，且直接影响回答风格，丢它性价比最低。
_EVICTION_ORDER = (
    MemoryType.EPISODE.value,
    MemoryType.FACT.value,
    MemoryType.PREFERENCE.value,
)

_SECTION_TITLES: Mapping[str, str] = {
    MemoryType.PREFERENCE.value: "相关用户偏好",
    MemoryType.FACT.value: "相关项目事实",
    MemoryType.EPISODE.value: "相似历史经历",
}

# Ops 模式的记忆里会有端口、进程、服务这类会变的东西，必须强制模型先用工具确认。
_RUNTIME_MODES = frozenset({"ops", "ops-privilege"})

# 时间衰减半衰期（天）：按类型区分。不在表里的类型（preference）不衰减。
# fact 会过时（版本号、IP、配置），episode 是经历衰减得慢，preference 稳定。
_RECENCY_HALF_LIFE_DAYS: Mapping[str, float] = {
    MemoryType.FACT.value: 90.0,
    MemoryType.EPISODE.value: 180.0,
}

_HEADER = (
    "【按问题检索到的相关记忆】\n"
    "以下是与当前问题相关的历史记忆，属于参考线索而非本轮事实；"
    "与本轮工具结果冲突时，一律以本轮工具结果为准。id 是短标识，完整 id 见 trace。"
)


@dataclass(frozen=True)
class MemoryPackEntry:
    """一条进入包里的记忆（渲染所需字段的完整快照）。"""

    memory_id: str
    memory_type: str
    scope: str
    content: str
    confidence: float
    score: float
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    sources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    content_truncated: bool = False
    observed_at: datetime | None = None


@dataclass(frozen=True)
class MemoryPackDrop:
    """一条没能进包的记忆及其原因（供观测，不渲染给模型）。

    ``score`` / ``recency_weight`` / ``final_score`` 只在"预算/条数竞争"类
    淘汰里填写，用于归因：分数低是 rerank 判它不相关，还是时间把它衰减死了——
    两者的修法完全不同。结构性淘汰（类型不可打包）不需要归因。
    """

    memory_id: str
    memory_type: str
    reason: str
    score: float | None = None
    recency_weight: float | None = None
    final_score: float | None = None


@dataclass(frozen=True)
class MemoryPack:
    """渲染好的记忆包。

    ``text`` 为空表示这一轮没有可注入的记忆——调用方必须把它当成"不注入"，
    而不是渲染一个空标题（那会白占 token 并暗示"查过了但没有"）。

    ``mode`` 保存下来是为了让 ``to_messages()`` 能重建"运行态约束"提示段，
    从而支持按条目拆分（阶段 7）。
    """

    text: str = ""
    entries: tuple[MemoryPackEntry, ...] = ()
    dropped: tuple[MemoryPackDrop, ...] = ()
    tokens: int = 0
    conflict_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    mode: str = "mentor"

    @property
    def empty(self) -> bool:
        return not self.entries

    @property
    def memory_ids(self) -> tuple[str, ...]:
        """进包记忆的**完整** id，用于 trace 与审计。"""

        return tuple(entry.memory_id for entry in self.entries)

    def to_message(self) -> dict[str, Any] | None:
        """包成一条可直接放进 ``evidence_messages`` 的消息。

        用 ``user`` 角色而不是 ``system``：记忆是证据，不能伪装成系统规则。
        ``_memory_pack`` 是本地标记，发送供应商前会被 ``to_provider_messages``
        按前缀剥掉。
        """

        if not self.text.strip():
            return None
        return {
            "role": "user",
            "content": self.text,
            "_memory_pack": True,
            # 完整 id 只在本地记账字段里，供 trace 与审计；发送前一并剥离。
            "_memory_pack_ids": list(self.memory_ids),
        }

    def to_messages(self) -> list[dict[str, Any]]:
        """把包拆成**按条目**的多条消息（阶段 7）。

        旧实现只提供 ``to_message()``（整包一条），compiler 因此只能
        "整包进或整包丢"——一条超大记忆就能让整个 MemoryPack 消失。
        拆条之后，compiler 在 hard eviction 阶段可以逐条移除最低优先级的
        内容，而不是一刀切。

        布局：
        - 每条记忆一条消息，按 ``entries`` 现有顺序（preference → fact →
          episode，即复用价值从高到低）；
        - header 与"冲突 / 运行态约束"等全局提示挂在**第一条**（最高优先级
          的那条）上，避免出现"只剩一个孤立 header"的消息。

        返回空列表表示这一轮不注入记忆。
        """

        if not self.text.strip() or not self.entries:
            return []

        global_notes = self._global_notes()
        messages: list[dict[str, Any]] = []
        last_index = len(self.entries) - 1
        for index, entry in enumerate(self.entries):
            body = (
                f"### {_SECTION_TITLES[entry.memory_type]}\n"
                f"- {entry.content}\n"
                f"  - {_metadata_line(entry)}"
            )
            content = (_HEADER + "\n\n" + body) if index == 0 else body
            if index == last_index and global_notes:
                content = content + "\n\n" + "\n".join(global_notes)
            messages.append(
                {
                    "role": "user",
                    "content": content,
                    "_memory_pack": True,
                    "_memory_pack_ids": [entry.memory_id],
                }
            )
        return messages

    def _global_notes(self) -> list[str]:
        """冲突提示与运行态约束——整包渲染与拆条渲染共用同一段文本。"""

        lines: list[str] = []
        if self.conflict_ids:
            lines.append("")
            lines.append("### 需要确认的冲突")
            lines.append(
                "- 这些记忆之间存在 contradicts 关系，不得静默合并："
                + "、".join(_short_id(memory_id) for memory_id in self.conflict_ids)
                + "。请在回答里说明不确定之处，或向用户确认。"
            )
        if _needs_runtime_constraint(self.mode):
            lines.append("")
            lines.append("### 运行态约束")
            lines.append(
                "- 以上记忆若涉及端口、进程、服务、容器等运行态信息，"
                "必须先由本轮工具结果确认后才能采用；记忆只是历史线索。"
            )
        if self.warnings:
            lines.append("")
            lines.append("### 召回状态")
            for warning in self.warnings:
                lines.append(f"- {warning}")
        return lines


class MemoryPackBuilder:
    """把召回结果裁成有界的、格式固定的注入包。"""

    def __init__(
        self,
        *,
        token_budget: int = 900,
        type_limits: Mapping[str, int] | None = None,
        max_content_chars: int = 400,
        max_sources: int = 3,
        clock: Callable[[], datetime] | None = None,
    ):
        if token_budget <= 0:
            raise ValueError("token_budget 必须为正整数")
        self._token_budget = int(token_budget)
        self._type_limits = dict(type_limits or DEFAULT_TYPE_LIMITS)
        self._max_content_chars = max(40, int(max_content_chars))
        self._max_sources = max(1, int(max_sources))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def token_budget(self) -> int:
        return self._token_budget

    def build(
        self,
        report: "MemoryRetrievalReport",
        *,
        mode: str = "mentor",
    ) -> MemoryPack:
        """把一次召回结果裁成注入包。

        刻意不抛异常：调用方在用户请求路径上，构包失败应当等价于"这轮不注入记忆"。
        """

        hits = list(getattr(report, "hits", ()) or ())
        conflict_ids = tuple(getattr(report, "conflict_ids", ()) or ())
        warnings = [
            "召回降级：" + "，".join(getattr(report, "degraded", ()) or ())
        ] if getattr(report, "degraded", None) else []

        selected, dropped = self._select(hits)
        if not selected:
            return MemoryPack(
                dropped=tuple(dropped),
                warnings=tuple(warnings),
            )

        entries = [self._to_entry(hit) for hit in selected]
        text = self._render(entries, conflict_ids, mode, warnings)
        tokens = estimate_tokens(text)

        # token 是最终硬约束：整条淘汰，绝不截断单条结构。
        while entries and tokens > self._token_budget:
            victim, attribution = self._pick_victim(entries)
            if victim is None:  # pragma: no cover - entries 非空时必然选得出
                break
            entries.remove(victim)
            score, weight, final_score = attribution
            dropped.append(
                MemoryPackDrop(
                    victim.memory_id,
                    victim.memory_type,
                    "over_token_budget",
                    score=score,
                    recency_weight=weight,
                    final_score=final_score,
                )
            )
            text = self._render(entries, conflict_ids, mode, warnings)
            tokens = estimate_tokens(text)

        if not entries:
            # 连一条都放不下：返回空包而不是"只有标题"的包。标题会白占 token
            # 并让模型以为"查过了但没有相关记忆"，那是两件不同的事。
            return MemoryPack(
                dropped=tuple(dropped),
                warnings=tuple(warnings),
            )

        kept = {entry.memory_id for entry in entries}
        return MemoryPack(
            text=text,
            entries=tuple(entries),
            dropped=tuple(dropped),
            tokens=tokens,
            conflict_ids=tuple(
                memory_id for memory_id in conflict_ids if memory_id in kept
            ),
            warnings=tuple(warnings),
            mode=mode,
        )

    # ------------------------------------------------------------- 选择 --

    def _select(
        self, hits: Sequence[MemoryHit]
    ) -> tuple[list[MemoryHit], list[MemoryPackDrop]]:
        """按类型分桶并施加条数上限，再按渲染顺序展开。

        召回结果本身已按融合分数降序，但条数上限的竞争用**组合分**
        （融合分 × 时间权重）裁决：同类型内旧观察给新观察让位。
        同分且同权重时保持召回顺序，不引入额外的不稳定排序。
        """

        now = self._clock()
        buckets: dict[str, list[MemoryHit]] = {key: [] for key in _SECTION_ORDER}
        dropped: list[MemoryPackDrop] = []
        for hit in hits:
            memory_type = _type_of(hit)
            limit = self._type_limits.get(memory_type, 0)
            if limit <= 0:
                dropped.append(
                    MemoryPackDrop(hit.record.id, memory_type, "type_not_packable")
                )
                continue
            bucket = buckets[memory_type]
            if len(bucket) < limit:
                bucket.append(hit)
                continue
            # 满桶：来者与桶内组合分最低者竞争，输的一方带归因出局。
            challenger_score = self._combined_score_of_hit(hit, now)
            incumbent = min(
                bucket, key=lambda item: self._combined_score_of_hit(item, now)
            )
            incumbent_score = self._combined_score_of_hit(incumbent, now)
            loser = hit if challenger_score <= incumbent_score else incumbent
            loser_score = min(challenger_score, incumbent_score)
            loser_weight = _recency_weight_for(
                memory_type, loser.version.observed_at, now
            )
            dropped.append(
                MemoryPackDrop(
                    loser.record.id,
                    memory_type,
                    "over_type_limit",
                    score=float(loser.score),
                    recency_weight=loser_weight,
                    final_score=loser_score,
                )
            )
            if loser is incumbent:
                bucket.remove(incumbent)
                bucket.append(hit)
        selected = [hit for key in _SECTION_ORDER for hit in buckets[key]]
        return selected, dropped

    def _combined_score_of_hit(self, hit: MemoryHit, now: datetime) -> float:
        """融合分 × 时间权重：同类型内竞争淘汰的裁决分。"""

        return float(hit.score) * _recency_weight_for(
            _type_of(hit), hit.version.observed_at, now
        )

    def _pick_victim(
        self, entries: list[MemoryPackEntry]
    ) -> tuple[MemoryPackEntry | None, tuple[float, float, float]]:
        """按类型顺序找第一个非空桶，桶内组合分最低者出局，附归因。"""

        now = self._clock()
        for memory_type in _EVICTION_ORDER:
            candidates = [
                entry for entry in entries if entry.memory_type == memory_type
            ]
            if not candidates:
                continue

            def combined(item: MemoryPackEntry) -> float:
                return item.score * _recency_weight_for(
                    item.memory_type, item.observed_at, now
                )

            victim = min(candidates, key=combined)
            weight = _recency_weight_for(victim.memory_type, victim.observed_at, now)
            return victim, (victim.score, weight, victim.score * weight)
        return None, (0.0, 1.0, 0.0)

    # ------------------------------------------------------------- 渲染 --

    def _to_entry(self, hit: MemoryHit) -> MemoryPackEntry:
        record = hit.record
        version = hit.version
        content = str(version.content or "").strip()
        truncated = False
        if len(content) > self._max_content_chars:
            content = content[: self._max_content_chars].rstrip() + "…"
            truncated = True
        # 只渲染来源标识，不渲染 source_excerpt：摘录是唯一可能带敏感内容的字段。
        sources = tuple(
            f"{source.source_type.value}:{source.source_id}"
            for source in (version.sources or ())[: self._max_sources]
        )
        return MemoryPackEntry(
            memory_id=record.id,
            memory_type=record.memory_type.value,
            scope=record.scope.value,
            content=content,
            confidence=float(record.confidence),
            score=float(hit.score),
            valid_from=version.valid_from,
            valid_to=version.valid_to,
            sources=sources,
            reasons=tuple(hit.reasons),
            content_truncated=truncated,
            observed_at=version.observed_at,
        )

    def _render(
        self,
        entries: Sequence[MemoryPackEntry],
        conflict_ids: Sequence[str],
        mode: str,
        warnings: Sequence[str],
    ) -> str:
        if not entries:
            return ""
        lines: list[str] = [_HEADER]
        by_type: dict[str, list[MemoryPackEntry]] = {}
        for entry in entries:
            by_type.setdefault(entry.memory_type, []).append(entry)

        for memory_type in _SECTION_ORDER:
            group = by_type.get(memory_type)
            if not group:
                continue
            lines.append("")
            lines.append(f"### {_SECTION_TITLES[memory_type]}")
            for entry in group:
                lines.append(f"- {entry.content}")
                lines.append(f"  - {_metadata_line(entry)}")

        if conflict_ids:
            lines.append("")
            lines.append("### 需要确认的冲突")
            lines.append(
                "- 这些记忆之间存在 contradicts 关系，不得静默合并："
                + "、".join(_short_id(memory_id) for memory_id in conflict_ids)
                + "。请在回答里说明不确定之处，或向用户确认。"
            )

        if _needs_runtime_constraint(mode):
            lines.append("")
            lines.append("### 运行态约束")
            lines.append(
                "- 以上记忆若涉及端口、进程、服务、容器等运行态信息，"
                "必须先由本轮工具结果确认后才能采用；记忆只是历史线索。"
            )

        if warnings:
            lines.append("")
            lines.append("### 召回状态")
            for warning in warnings:
                lines.append(f"- {warning}")

        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _type_of(hit: MemoryHit) -> str:
    memory_type = hit.record.memory_type
    return memory_type.value if isinstance(memory_type, MemoryType) else str(memory_type)


def _as_aware(moment: datetime) -> datetime:
    """naive datetime 按 UTC 处理（DB 返回 aware，测试/旧数据可能给 naive）。"""

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _recency_weight_for(
    memory_type: str, observed_at: datetime | None, now: datetime
) -> float:
    """时间权重 ∈ (0, 1]：越新越接近 1。

    - 不在半衰期表里的类型（preference）恒为 1.0：偏好没有"变旧"的语义；
    - 缺 observed_at 按 1.0 处理：数据缺失不惩罚，交给融合分裁决；
    - 未来时间戳（时钟偏斜）按 0 天龄处理，权重 1.0，不给负龄加成。
    """

    half_life = _RECENCY_HALF_LIFE_DAYS.get(memory_type)
    if half_life is None or observed_at is None:
        return 1.0
    age_seconds = (now - _as_aware(observed_at)).total_seconds()
    age_days = max(0.0, age_seconds / 86400.0)
    return math.exp(-age_days / half_life)


def _short_id(memory_id: str) -> str:
    text = str(memory_id or "")
    return text[:8] if text else "unknown"


def _window_text(entry: MemoryPackEntry) -> str:
    start = entry.valid_from
    if start is None:
        return "未知"
    if entry.valid_to is None:
        return f"{start:%Y-%m-%d} 起有效"
    return f"{start:%Y-%m-%d} ~ {entry.valid_to:%Y-%m-%d}"


def _metadata_line(entry: MemoryPackEntry) -> str:
    sources = "、".join(entry.sources) if entry.sources else "未记录"
    truncated = " ｜ 正文已截断" if entry.content_truncated else ""
    return (
        f"id={_short_id(entry.memory_id)} ｜ 类型={entry.memory_type} ｜ "
        f"作用域={entry.scope} ｜ 置信度={entry.confidence:.2f} ｜ "
        f"有效期={_window_text(entry)} ｜ 来源={sources}{truncated}"
    )


def _needs_runtime_constraint(mode: str) -> bool:
    return str(mode or "").strip().lower() in _RUNTIME_MODES
