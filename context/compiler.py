"""调用前上下文编译器。

ContextCompiler 根据当前模型的上下文预算，在每次模型调用前构造
合法、完整且留有输出空间的消息列表：

1. 必需区：系统规则、活动 checkpoint、本轮临时控制消息、当前用户输入；
2. 证据区：RAG/日志/工具摘要等证据消息；
3. 最近历史区：token 预算内的最近完整消息组。

编译结果保证估算 token 不超过 hard input limit；超过软阈值时通过
compression_required 请求压缩，而不是请求失败后补救。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from klonet_agent.context.budget import ContextBudget, build_context_budget
from klonet_agent.context.message_groups import (
    MessageGroup,
    parse_message_groups,
    select_recent_groups,
)
from klonet_agent.context import tokens as _tokens_module
from klonet_agent.context.tokens import SupportsEncode


class ContextOverflowError(Exception):
    """必需内容本身超过硬预算时的本地确定性错误。"""

    def __init__(self, message: str, areas: dict[str, int] | None = None):
        super().__init__(message)
        self.areas = areas or {}


# 只剥离本地记账字段，其余字段原样保留：部分供应商（如 DeepSeek 推理模型）
# 要求把 reasoning_content 原样回传，白名单会误删这类供应商字段。
_LOCAL_MESSAGE_KEYS = frozenset({"event_id", "ts"})


def to_provider_messages(messages: list[dict]) -> list[dict]:
    """剥离本地事件身份字段，返回可直接发送的消息列表。"""

    cleaned: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        cleaned.append(
            {
                key: value
                for key, value in message.items()
                if key not in _LOCAL_MESSAGE_KEYS and not key.startswith("_")
            }
        )
    return cleaned


def assert_within_hard_limit(
    messages: list[dict],
    model: str,
    tool_definitions: list[dict] | None = None,
) -> int:
    """在调用供应商前做最后一次硬预算断言。

    返回估算输入 token。超过 hard input limit 时抛出 ContextOverflowError，
    调用方必须拒绝发送请求，而不是回退到未编译的完整历史。

    注意 hard_input_limit 已经扣掉了输出预留、tool schema 和安全余量，
    因此这里只比较消息本身的 token，不重复计入 tool schema。

    真路径：profile 绑了 tokenizer_id 时按 BPE 精确算 token；
    降级：profile 未绑或 tokenizer 加载失败时按字符启发式算。
    """

    budget = build_context_budget(model, tool_definitions)
    tokenizer = _tokens_module.get_tokenizer(budget.tokenizer_id)
    estimated = _tokens_module.estimate_messages_tokens(messages, tokenizer=tokenizer)
    if estimated > budget.hard_input_limit:
        raise ContextOverflowError(
            "发送前硬预算断言失败：估算输入 token 超过模型 hard limit，拒绝请求供应商。",
            areas={
                "estimated_input": estimated,
                "hard_input_limit": budget.hard_input_limit,
                "reserved_output": budget.reserved_output_tokens,
                "reserved_tools": budget.reserved_tool_tokens,
            },
        )
    return estimated


@dataclass(frozen=True)
class ContextRequest:
    """一次模型调用需要的全部上下文材料。"""

    model: str
    system_messages: list[dict] = field(default_factory=list)
    checkpoint_message: dict | None = None
    history_messages: list[dict] = field(default_factory=list)
    transient_messages: list[dict] = field(default_factory=list)
    current_user_message: dict | None = None
    tool_definitions: list[dict] | None = None
    # evidence_messages：RAG/日志/工具摘要等证据消息，独立预算。
    evidence_messages: list[dict] = field(default_factory=list)
    # memory_pack_message：按问题检索到的记忆包，**独立**预算且先于普通证据。
    # 它必须留在证据区：检索到的记忆是与当前任务相关的历史信息，
    # 不具备系统规则的优先级，不能伪装成 system policy（计划 §6.7）。
    memory_pack_message: dict | None = None


@dataclass(frozen=True)
class CompiledContext:
    """编译后的最终消息列表与编译元数据。"""

    messages: list[dict]
    estimated_input_tokens: int
    hard_input_limit: int
    soft_input_limit: int
    included_event_ids: tuple[str, ...]
    omitted_event_ids: tuple[str, ...]
    checkpoint_id: str | None
    compression_required: bool
    areas: dict[str, int] = field(default_factory=dict)
    profile_source: str = "builtin"
    # 实际进入上下文的记忆 id（来自 MemoryPack 的本地记账字段），供 trace 断言
    # "相关记忆确实进了证据区、无关记忆一条都没进"。
    memory_pack_ids: tuple[str, ...] = ()


class ContextCompiler:
    """把多路上下文材料编译为一次 LLM 请求的消息列表。"""

    def compile(self, request: ContextRequest) -> CompiledContext:
        """按预算组装最终消息列表。"""

        budget = build_context_budget(request.model, request.tool_definitions)
        # 一次预算解析 → 一份 tokenizer；tokenizer 加载失败时为 None，
        # 所有 estimate_messages_tokens 调用自动降级到字符启发式。
        # 用模块属性查找而非 ``from ... import get_tokenizer``，是为了让测试
        # 里 ``monkeypatch.setattr(tokens_module, "get_tokenizer", ...)``
        # 在本模块也能生效——import 绑定会冻结引用。
        tokenizer = _tokens_module.get_tokenizer(budget.tokenizer_id)
        areas: dict[str, int] = {
            "system": 0,
            "checkpoint": 0,
            "transient": 0,
            "current_input": 0,
            "memory_pack": 0,
            "evidence": 0,
            "recent_history": 0,
            "tools": budget.reserved_tool_tokens,
        }

        # ---- 必需区：完整保留，不允许裁剪 ----
        # 顺序：system -> checkpoint -> transient -> [evidence] -> [recent] -> current
        # 当前用户输入始终排在最后，最近历史插入在其之前。
        system_messages = list(request.system_messages)
        areas["system"] = _tokens_module.estimate_messages_tokens(system_messages, tokenizer=tokenizer)

        checkpoint_messages: list[dict] = []
        checkpoint_id = None
        if request.checkpoint_message is not None:
            checkpoint_messages = [request.checkpoint_message]
            checkpoint_id = str(request.checkpoint_message.get("checkpoint_id") or "")
        areas["checkpoint"] = _tokens_module.estimate_messages_tokens(
            checkpoint_messages, tokenizer=tokenizer
        )

        transient = list(request.transient_messages)
        areas["transient"] = _tokens_module.estimate_messages_tokens(transient, tokenizer=tokenizer)

        current_user = request.current_user_message
        areas["current_input"] = (
            _tokens_module.estimate_messages_tokens([current_user], tokenizer=tokenizer)
            if current_user is not None
            else 0
        )

        required_tokens = (
            areas["system"]
            + areas["checkpoint"]
            + areas["transient"]
            + areas["current_input"]
        )

        # 必需区自身就超过硬预算：本地确定性拒绝，不发请求。
        if required_tokens > budget.hard_input_limit:
            raise ContextOverflowError(
                "必需上下文（系统规则 + checkpoint + 当前输入）已超过硬预算，"
                "拒绝发送请求。",
                areas=areas,
            )

        # ---- 记忆区：独立预算，先于普通证据 ----
        # Memory Pack 在构建时已经按自己的条数与 token 上限裁过一遍，这里只是
        # 最后一道"整条放不下就不放"。_fit_messages 按整条保留/整条丢弃处理，
        # 所以不会把一条记忆切成半条——残缺条目比少一条更糟。
        memory_pack: list[dict] = []
        pack_omitted: list[dict] = []
        remaining = budget.hard_input_limit - required_tokens
        if request.memory_pack_message is not None:
            memory_pack, pack_omitted = _fit_messages(
                [request.memory_pack_message],
                int(remaining * 0.15),
                tokenizer=tokenizer,
            )
            areas["memory_pack"] = _tokens_module.estimate_messages_tokens(
                memory_pack, tokenizer=tokenizer
            )
            required_tokens += areas["memory_pack"]

        # ---- 证据区：独立预算（可用空间的一部分） ----
        remaining = budget.hard_input_limit - required_tokens
        evidence_budget = int(remaining * 0.25)
        evidence_messages, evidence_omitted = _fit_messages(
            request.evidence_messages, evidence_budget, tokenizer=tokenizer
        )
        areas["evidence"] = _tokens_module.estimate_messages_tokens(
            evidence_messages, tokenizer=tokenizer
        )
        required_tokens += areas["evidence"]

        # ---- 最近历史区：剩余预算内从新到旧选择完整组 ----
        history_budget = budget.hard_input_limit - required_tokens
        history_messages = request.history_messages
        included: list[MessageGroup] = []
        omitted: list[MessageGroup] = []
        recent: list[dict] = []
        if history_messages:
            groups = parse_message_groups(history_messages)
            included, omitted = select_recent_groups(
                groups, history_budget, tokenizer=tokenizer
            )
            recent = [dict(message) for group in included for message in group.messages]
            areas["recent_history"] = _tokens_module.estimate_messages_tokens(
                recent, tokenizer=tokenizer
            )

        included_ids = tuple(
            event_id for group in included for event_id in group.event_ids
        )
        omitted_ids = tuple(
            event_id for group in omitted for event_id in group.event_ids
        )
        if pack_omitted:
            omitted_ids = omitted_ids + ("memory_pack",)
        if evidence_omitted:
            omitted_ids = omitted_ids + tuple(
                f"evidence-{index}" for index in range(len(evidence_omitted))
            )

        final_messages: list[dict] = []
        final_messages.extend(system_messages)
        final_messages.extend(checkpoint_messages)
        final_messages.extend(transient)
        final_messages.extend(memory_pack)
        final_messages.extend(evidence_messages)
        final_messages.extend(recent)
        if current_user is not None:
            final_messages.append(current_user)

        estimated = required_tokens + areas["recent_history"]
        compression_required = estimated > budget.soft_input_limit

        memory_pack_ids: tuple[str, ...] = ()
        if memory_pack:
            raw_ids = memory_pack[0].get("_memory_pack_ids") or ()
            memory_pack_ids = tuple(str(item) for item in raw_ids)

        return CompiledContext(
            messages=final_messages,
            estimated_input_tokens=estimated,
            hard_input_limit=budget.hard_input_limit,
            soft_input_limit=budget.soft_input_limit,
            included_event_ids=included_ids,
            omitted_event_ids=omitted_ids,
            checkpoint_id=checkpoint_id or None,
            compression_required=compression_required,
            areas=areas,
            profile_source=budget.profile_source,
            memory_pack_ids=memory_pack_ids,
        )

    def compile_history(
        self,
        history: list[dict],
        model: str,
        tool_definitions: list[dict] | None = None,
        checkpoint_message: dict | None = None,
        evidence_messages: list[dict] | None = None,
        memory_pack_message: dict | None = None,
    ) -> CompiledContext:
        """从扁平 history 编译（编排器当前主路径的便捷入口）。

        扁平历史拆分规则：
        - role=system 的消息全部归入系统区（保持相对顺序，置于最前）；
        - 最后一条 user 消息视为当前用户输入（必需区）；
        - 其余消息按完整组参与最近历史选择。
        """

        system_messages = [m for m in history if m.get("role") == "system"]
        non_system = [m for m in history if m.get("role") != "system"]

        current_user = None
        if non_system and non_system[-1].get("role") == "user":
            current_user = non_system[-1]
            history_messages = non_system[:-1]
        else:
            history_messages = non_system

        request = ContextRequest(
            model=model,
            system_messages=system_messages,
            checkpoint_message=checkpoint_message,
            history_messages=history_messages,
            current_user_message=current_user,
            tool_definitions=tool_definitions,
            evidence_messages=evidence_messages or [],
            memory_pack_message=memory_pack_message,
        )
        return self.compile(request)


def _fit_messages(
    messages: list[dict],
    token_budget: int,
    *,
    tokenizer: SupportsEncode | None = None,
) -> tuple[list[dict], list[dict]]:
    """按预算从新到旧保留证据消息，返回 (included, omitted)。

    ``tokenizer`` 透传到 ``estimate_messages_tokens``，与 compile() 主体
    的口径保持一致。
    """

    included: list[dict] = []
    omitted: list[dict] = []
    used = 0
    for message in reversed(messages):
        cost = _tokens_module.estimate_messages_tokens([message], tokenizer=tokenizer)
        if used + cost <= token_budget:
            included.insert(0, message)
            used += cost
        else:
            omitted.insert(0, message)
    return included, omitted
