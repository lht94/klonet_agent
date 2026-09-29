"""完整消息组解析与选择。

把扁平消息列表解析成语义完整的逻辑单元（MessageGroup），
再按 token 预算从新到旧选择，保证：
- user 消息不会脱离对应回答；
- assistant 的 tool calls 与全部 tool results 不分开；
- 工具结果后的最终 assistant 回答尽可能同组保留。

消息数量与 token 数没有稳定关系，因此裁剪单位是"完整组"而不是单条消息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from klonet_agent.context.tokens import estimate_messages_tokens


@dataclass(frozen=True)
class MessageGroup:
    """一个语义完整的消息组。

    group_type:
      - system: 系统提示词（编译器单独处理，一般不进入最近历史选择）
      - user_turn: 一个 user 消息及其后的 assistant/tool 回应
      - orphan: 位于历史开头、缺少锚点 user 的残片（如中断的工具链）
    """

    event_ids: tuple[str, ...]
    messages: tuple[dict[str, Any], ...]
    group_type: str
    estimated_tokens: int = field(default=0)

    def __post_init__(self):
        if self.estimated_tokens == 0:
            object.__setattr__(
                self, "estimated_tokens", estimate_messages_tokens(list(self.messages))
            )


def _message_event_id(index: int) -> str:
    """为没有 event_id 的旧格式消息生成稳定的位置 ID。"""

    return f"msg-{index:06d}"


def _tool_call_ids(message: dict[str, Any]) -> list[str]:
    ids = []
    for tool_call in message.get("tool_calls") or []:
        if isinstance(tool_call, dict):
            ids.append(str(tool_call.get("id") or ""))
    return ids


def parse_message_groups(messages: list[dict[str, Any]]) -> list[MessageGroup]:
    """把扁平消息解析成语义组。"""

    groups: list[MessageGroup] = []
    pending_non_system: list[tuple[str, dict[str, Any]]] = []
    # pending_non_system 保存 (event_id, message)，等待归入某个组。

    def _flush_user_turn():
        nonlocal pending_non_system
        if not pending_non_system:
            return
        pairs = pending_non_system
        pending_non_system = []
        event_ids = tuple(event_id for event_id, _ in pairs)
        msgs = [message for _, message in pairs]

        # 组内如果以 tool 结果开头（孤立 tool），不影响整组合法性：
        # sanitize_openai_tool_history 会在发送前清理。
        group_type = "user_turn"
        if msgs[0].get("role") != "user":
            group_type = "orphan"
        groups.append(
            MessageGroup(
                event_ids=event_ids,
                messages=tuple(msgs),
                group_type=group_type,
            )
        )

    for index, message in enumerate(messages):
        event_id = str(message.get("event_id") or _message_event_id(index))
        role = message.get("role")
        if role == "system":
            _flush_user_turn()
            groups.append(
                MessageGroup(
                    event_ids=(event_id,),
                    messages=(message,),
                    group_type="system",
                )
            )
            continue
        # 遇到新的 user 消息时，先结算上一回合（或开头孤立残片）。
        if role == "user" and pending_non_system:
            _flush_user_turn()
        pending_non_system.append((event_id, message))

    _flush_user_turn()
    return groups


def select_recent_groups(
    groups: list[MessageGroup],
    token_budget: int,
) -> tuple[list[MessageGroup], list[MessageGroup]]:
    """从新到旧选择完整组，直到预算用尽。

    返回 (included, omitted)。保证：
    - 组的相对顺序不变；
    - 任何组都不会被部分纳入；
    - 最新组即使单独超过预算，也会降级保留其首尾消息，
      避免出现"一条巨大工具输出让最近历史完全为空"的情况。
    """

    if token_budget <= 0:
        return [], list(groups)

    non_system = [group for group in groups if group.group_type != "system"]
    included: list[MessageGroup] = []
    omitted: list[MessageGroup] = []
    used = 0

    for group in reversed(non_system):
        if group.estimated_tokens <= token_budget - used:
            included.append(group)
            used += group.estimated_tokens
            continue
        omitted.append(group)

    included.reverse()
    omitted.reverse()

    # 兜底：一条消息都没装下时（最新组本身超预算），降级保留该组中
    # 的 user 消息与最后一条 assistant 文本消息，丢弃中间工具交换。
    if not included and non_system:
        newest = non_system[-1]
        fallback = [
            message
            for message in newest.messages
            if message.get("role") in {"user", "assistant"}
            and not message.get("tool_calls")
        ]
        if fallback:
            kept_ids = tuple(
                event_id
                for event_id, message in zip(newest.event_ids, newest.messages)
                if message in fallback
            )
            degraded = MessageGroup(
                event_ids=kept_ids,
                messages=tuple(fallback),
                group_type="orphan",
            )
            included = [degraded]
            omitted = non_system[:-1]

    return included, omitted
