"""结构化记忆候选的输出合同与提取器（阶段 3）。

计划 §6.4 的核心取舍是：**主模型不再拥有"写记忆"的权威能力**，它只能提出
结构化候选。这个文件定义"候选长什么样"以及"怎么把模型的输出安全地解析成候选"。

三条硬约束：

1. **宽进严出**。模型输出是不可信输入：缺字段、类型错、枚举值瞎写都可能出现。
   解析器对**每条候选**独立判定，坏的那条连同原因一起丢弃，绝不因为一条坏数据
   让整个回合失败（§7.2："候选失败不影响用户回答"）。
2. **不在这里做策略判断**。解析只回答"这个结构能不能读成候选"；
   "该不该写、写到哪个作用域、内容敏不敏感"全部归 ``memory/write_policy.py``。
   分开的理由是策略需要有唯一的审计出口，散落在解析里就没法逐条解释拒绝原因。
3. **来源必须可核对**。候选里的 ``sources`` 是模型引用的证据 id；解析层只做形状校验，
   存在性校验在策略层用回合上下文里的白名单核对——**模型不能自己编造 source_id**。

提示词只描述合同本身，不描述业务：提取什么内容由调用方（写入管线）通过
:class:`TurnDigest` 喂进来。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from klonet_agent.memory.domain import (
    ImportanceLevel,
    MemoryType,
    Scope,
)

__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "CandidateExtractor",
    "CandidateProposal",
    "ExtractionError",
    "RawSource",
    "TurnDigest",
    "build_extraction_messages",
    "parse_proposals",
]


CANDIDATE_SCHEMA_VERSION = 1

# 一次回合最多接受多少条候选。模型偶尔会把整段对话拆成一堆碎片，
# 这个上限是"防跑飞"的闸门，不是质量判断（质量由策略层与 consolidation 负责）。
MAX_PROPOSALS_PER_TURN = 12

# 单个字段的长度上限。超长不是"截断后照收"，而是直接判为形状不合法——
# 正文被截断会改变语义，宁可让模型重新提一次。
MAX_CONTENT_CHARS = 2000
MAX_SUMMARY_CHARS = 300
MAX_EXCERPT_CHARS = 1000
MAX_SUBJECT_CHARS = 120


class ExtractionError(RuntimeError):
    """提取阶段的硬错误（模型调用失败、返回完全不可解析）。

    注意与"某条候选不合法"区分：后者是正常的拒绝路径，不是异常。
    """


@dataclass(frozen=True)
class RawSource:
    """候选引用的一条证据。``source_id`` 必须能在回合上下文里被核对到。"""

    source_type: str
    source_id: str
    excerpt: str = ""


@dataclass(frozen=True)
class CandidateProposal:
    """模型提出的**未验证**候选。

    字段刻意保持宽松（字符串而非枚举）：这一层的职责是"忠实搬运模型说的话"，
    枚举校验、作用域判定和敏感过滤都在策略层，这样拒绝原因才有唯一出处。
    """

    memory_type: str
    scope: str
    content: str
    subject: str = ""
    attribute: str | None = None
    episode_id: str = ""
    summary: str = ""
    importance: str = ImportanceLevel.MEDIUM.value
    confidence: float = 0.5
    verified: bool = False
    proposed_decision: str | None = None
    sources: tuple[RawSource, ...] = ()
    # 在模型输出里的序号，用于把拒绝原因和原始响应对齐，便于排查。
    raw_index: int = 0
    # 解析期的形状警告（不致命，例如多给了字段）。会随决策一起进 trace。
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TurnDigest:
    """喂给提取器的一个回合摘要。

    ``events`` 与 ``source_ids`` 是**证据白名单**：策略层据此核对候选引用的来源
    是否真实存在。这里只放已脱敏、已限长的文本——提取器的输入本身就不能含秘密。
    """

    source_event_range: Mapping[str, Any]
    user_input: str
    assistant_reply: str
    events: tuple[Mapping[str, Any], ...] = ()
    source_ids: tuple[tuple[str, str], ...] = ()
    project_id: str | None = None
    extra_context: str = ""


# --------------------------------------------------------------------------- #
# 提示词与合同
# --------------------------------------------------------------------------- #

_SCHEMA_HINT = """{
  "candidates": [
    {
      "memory_type": "fact | preference | episode",
      "scope": "user | project",
      "entity": "稳定的实体名，如 klonet / python / user",
      "attribute": "属性名，如 runtime_version。episode 类型留空",
      "episode_id": "仅 episode 类型：事件标识（用来源行号，如 rows-12）",
      "content": "一句独立成立的话，不要写“上面提到的”这类指代",
      "summary": "可选，一句话摘要",
      "importance": "low | medium | high",
      "confidence": 0.0 到 1.0 的小数,
      "verified": true 或 false,
      "proposed_decision": "add | update | supersede | noop | reject",
      "sources": [
        {"source_type": "history_event | tool_result | user_statement | journal",
         "source_id": "必须是下面给出的证据 id 之一",
         "excerpt": "可选，不超过 200 字的脱敏摘录"}
      ]
    }
  ]
}"""

_EXTRACTION_RULES = """你必须遵守：
1. 只提取"以后还用得上"的内容：项目不变量、架构事实、用户的稳定偏好、具体经历。
   闲聊、一次性的寒暄、当前的临时要求都不要提。
2. 每条候选都必须带 sources，且 source_id 只能取下面"可引用的证据"里列出的值。
   编造 source_id 会被直接拒绝。
3. 绝对不要把口令、API Key、Token、Cookie、私钥、连接串、.env 正文放进候选。
   需要表达"配置在某处"就用描述，不要抄值。
4. 事实与经历才可能有 verified=true；偏好只有在用户**明确表述**时才算。
5. 同一 subject 的取值发生变化时用 supersede，只补充描述/来源时用 update，
   内容与已知记忆完全等价时用 noop。
6. 拿不准就不要提。宁可少写，也不要写错。
7. 只输出 JSON，不要额外的解释文字。"""


def build_extraction_messages(
    digest: TurnDigest, *, max_event_chars: int = 4000
) -> list[dict[str, str]]:
    """构造提取用的 messages。

    证据列表显式列在提示里，模型才知道哪些 ``source_id`` 是合法的——
    这比"让模型自己记行号"可靠得多，也是后续白名单校验能成立的前提。
    """

    lines: list[str] = []
    for event in digest.events:
        role = str(event.get("role") or event.get("name") or "event")
        content = str(event.get("content") or event.get("result") or "")
        if len(content) > max_event_chars:
            content = content[:max_event_chars] + "…(截断)"
        event_id = str(event.get("event_id") or "")
        lines.append(f"[{role}] {content}" + (f"  <event_id={event_id}>" if event_id else ""))
    transcript = "\n".join(lines) if lines else "(无额外事件)"

    evidence = "\n".join(f"- {kind}:{sid}" for kind, sid in digest.source_ids) or "(无)"

    user_block = f"""## 本轮用户输入
{digest.user_input}

## 本轮的助手回复
{digest.assistant_reply}

## 本轮事件
{transcript}

## 可引用的证据（source_type:source_id）
{evidence}

## 其它上下文
{digest.extra_context or "(无)"}

## 输出合同
{_SCHEMA_HINT}

{_EXTRACTION_RULES}"""

    return [
        {
            "role": "system",
            "content": "你是一个记忆提取器。你只负责提出结构化候选，不负责决定它们是否被写入。",
        },
        {"role": "user", "content": user_block},
    ]


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)


def extract_json_payload(raw: str) -> Mapping[str, Any]:
    """从模型回复里取出 JSON 对象。

    模型经常把 JSON 包在 ```json 代码块里，或者在前后加一句解释。这里做两级容错：
    先去代码块，再退化成"取第一个 { 到最后一个 }"。两者都失败才报
    :class:`ExtractionError`——那是"模型完全没按合同输出"，属于硬错误。
    """

    text = str(raw or "").strip()
    if not text:
        raise ExtractionError("提取器返回了空响应")

    block = _JSON_BLOCK_RE.search(text)
    if block:
        text = block.group("body").strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ExtractionError("提取器响应里找不到 JSON 对象") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"提取器响应不是合法 JSON：{exc}") from exc

    if not isinstance(parsed, Mapping):
        raise ExtractionError("提取器响应的顶层不是 JSON 对象")
    return parsed


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _as_float(value: Any, default: float) -> tuple[float, str | None]:
    if isinstance(value, bool) or value is None:
        return default, ("confidence 不是数字，已用默认值" if value is not None else None)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default, "confidence 不是数字，已用默认值"
    if not 0.0 <= number <= 1.0:
        return default, "confidence 超出 [0,1]，已夹到边界"
    return number, None


def _canonical_memory_type(value: Any) -> str:
    text = _as_text(value).strip().lower()
    alias = {"facts": "fact", "preferences": "preference", "episodes": "episode",
             "事件": "episode", "事实": "fact", "偏好": "preference"}
    return alias.get(text, text)


def _canonical_scope(value: Any) -> str:
    text = _as_text(value).strip().lower()
    alias = {"global": "user", "用户": "user", "项目": "project", "shared": "shared_ops",
             "shared_ops": "shared_ops"}
    return alias.get(text, text)


def _canonical_decision(value: Any) -> str | None:
    text = _as_text(value).strip().lower()
    if not text:
        return None
    alias = {"create": "add", "new": "add", "replace": "supersede", "skip": "noop",
             "ignore": "reject"}
    return alias.get(text, text)


def _parse_source(item: Any, index: int) -> tuple[RawSource | None, str | None]:
    if not isinstance(item, Mapping):
        return None, f"sources[{index}] 不是对象"
    kind = _as_text(item.get("source_type")).strip().lower()
    sid = _as_text(item.get("source_id")).strip()
    if not kind or not sid:
        return None, f"sources[{index}] 缺 source_type 或 source_id"
    excerpt = _as_text(item.get("excerpt"))
    warning = None
    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_EXCERPT_CHARS]
        warning = f"sources[{index}].excerpt 超长已截断"
    return RawSource(source_type=kind, source_id=sid, excerpt=excerpt), warning


def parse_proposals(
    payload: Any,
) -> tuple[tuple[CandidateProposal, ...], tuple[str, ...]]:
    """把模型输出解析成候选列表。

    返回 ``(候选, 拒绝原因)``。**不抛异常**（除了顶层结构完全不可用时由调用方
    决定）：单条候选的任何问题只影响它自己。
    """

    if isinstance(payload, str):
        payload = extract_json_payload(payload)
    if not isinstance(payload, Mapping):
        return (), ("顶层不是 JSON 对象",)

    raw_items = payload.get("candidates")
    if raw_items is None:
        raw_items = payload.get("memories")
    if raw_items is None:
        return (), ("缺少 candidates 字段",)
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return (), ("candidates 不是数组",)

    proposals: list[CandidateProposal] = []
    rejected: list[str] = []

    for index, item in enumerate(raw_items):
        if len(proposals) >= MAX_PROPOSALS_PER_TURN:
            rejected.append(
                f"候选数超过上限 {MAX_PROPOSALS_PER_TURN}，其余被丢弃"
            )
            break
        if not isinstance(item, Mapping):
            rejected.append(f"第 {index} 条候选不是对象")
            continue

        warnings: list[str] = []

        memory_type = _canonical_memory_type(item.get("memory_type") or item.get("type"))
        if memory_type not in {member.value for member in MemoryType}:
            rejected.append(f"第 {index} 条候选的 memory_type 非法：{memory_type!r}")
            continue

        scope = _canonical_scope(item.get("scope"))
        if scope not in {member.value for member in Scope}:
            rejected.append(f"第 {index} 条候选的 scope 非法：{scope!r}")
            continue

        content = _as_text(item.get("content")).strip()
        if not content:
            rejected.append(f"第 {index} 条候选没有 content")
            continue
        if len(content) > MAX_CONTENT_CHARS:
            rejected.append(
                f"第 {index} 条候选 content 超过 {MAX_CONTENT_CHARS} 字，"
                "拒绝收下（截断会改变语义，请重新提取）"
            )
            continue

        subject = _as_text(item.get("entity") or item.get("subject")).strip()
        attribute = _as_text(item.get("attribute")).strip() or None
        episode_id = _as_text(item.get("episode_id") or item.get("event_id")).strip()
        if len(subject) > MAX_SUBJECT_CHARS or (attribute and len(attribute) > MAX_SUBJECT_CHARS):
            rejected.append(f"第 {index} 条候选的 subject/attribute 过长")
            continue

        summary = _as_text(item.get("summary")).strip()
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS]
            warnings.append("summary 超长已截断")

        importance = _as_text(item.get("importance")).strip().lower()
        if importance not in {member.value for member in ImportanceLevel}:
            if importance:
                warnings.append(f"importance={importance!r} 非法，按 medium 处理")
            importance = ImportanceLevel.MEDIUM.value

        confidence, confidence_warning = _as_float(item.get("confidence"), 0.5)
        if confidence_warning:
            warnings.append(confidence_warning)

        raw_sources = item.get("sources") or ()
        if isinstance(raw_sources, Mapping) or isinstance(raw_sources, (str, bytes)):
            rejected.append(f"第 {index} 条候选的 sources 不是数组")
            continue
        sources: list[RawSource] = []
        for source_index, raw_source in enumerate(raw_sources):
            parsed_source, source_warning = _parse_source(raw_source, source_index)
            if source_warning:
                if parsed_source is None:
                    rejected.append(f"第 {index} 条候选：{source_warning}")
                else:
                    warnings.append(source_warning)
            if parsed_source is not None:
                sources.append(parsed_source)

        proposals.append(
            CandidateProposal(
                memory_type=memory_type,
                scope=scope,
                content=content,
                subject=subject,
                attribute=attribute,
                episode_id=episode_id,
                summary=summary,
                importance=importance,
                confidence=confidence,
                verified=bool(item.get("verified")),
                proposed_decision=_canonical_decision(item.get("proposed_decision")),
                sources=tuple(sources),
                raw_index=index,
                warnings=tuple(warnings),
            )
        )

    return tuple(proposals), tuple(rejected)


# --------------------------------------------------------------------------- #
# 提取器
# --------------------------------------------------------------------------- #

#: 调用模型的最小签名：给定 messages，返回一段文本（应当是 JSON）。
CompleteFn = Callable[[Sequence[Mapping[str, str]]], str]


class CandidateExtractor:
    """把回合摘要变成候选。

    ``complete`` 由调用方注入（运行时是 ``LLMClient.complete`` 的薄封装，
    测试里是返回固定 JSON 的假函数）。**刻意不在这里建 LLM 客户端**：
    提取用什么模型是部署决策，而解析与策略必须能在没有网络的测试里跑。
    """

    def __init__(self, complete: CompleteFn | None = None) -> None:
        self._complete = complete

    @property
    def is_available(self) -> bool:
        return self._complete is not None

    def extract(
        self, digest: TurnDigest
    ) -> tuple[tuple[CandidateProposal, ...], tuple[str, ...]]:
        """返回 ``(候选, 拒绝原因)``。

        模型调用失败会抛 :class:`ExtractionError`——由管线决定怎么降级；
        解析出坏数据则走正常拒绝路径。
        """

        if self._complete is None:
            raise ExtractionError("没有配置提取模型，无法提取候选")
        messages = build_extraction_messages(digest)
        try:
            raw = self._complete(messages)
        except Exception as exc:  # noqa: BLE001 - 提取失败必须是可降级的
            raise ExtractionError(f"提取模型调用失败：{exc}") from exc
        return parse_proposals(raw)
