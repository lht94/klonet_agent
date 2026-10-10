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

from klonet_agent.context import tokens as _tokens_module
from klonet_agent.context.tokens import SupportsEncode


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
                self,
                "estimated_tokens",
                _tokens_module.estimate_messages_tokens(list(self.messages)),
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
    *,
    tokenizer: SupportsEncode | None = None,
) -> tuple[list[MessageGroup], list[MessageGroup]]:
    """从新到旧选择完整组，直到预算用尽。

    返回 (included, omitted)。保证：
    - 组的相对顺序不变；
    - 任何组都不会被部分纳入；
    - **整组放不下就整组 omit，绝不拆链**（阶段 7）。旧实现曾在"最新组
      过大"时降级保留其中的 user/assistant 文本、丢弃中间工具交换，
      那会产生"有 tool_call 没有 tool result"的协议残片；宁可让这一轮
      历史区为空，也不能发送残缺的工具链。若确实需要一个巨大的历史
      工具交换，应由调用方先行注入结构化工具摘要，而不是在这里拆。

    ``tokenizer`` 为 None 时按启发式 token 数选（与 MessageGroup 初始化时
    的估算口径一致）；非 None 时按真路径 BPE 重新估算每组 token，
    保证预算精度与上游 compile() 内的 estimate_messages_tokens 对齐。
    """

    if token_budget <= 0:
        return [], list(groups)

    non_system = [group for group in groups if group.group_type != "system"]
    included: list[MessageGroup] = []
    omitted: list[MessageGroup] = []
    used = 0

    for group in reversed(non_system):
        # select 阶段重新按 tokenizer 算 token，避免 group.estimated_tokens
        # （构造时启发式）与当前调用方选择的 tokenizer 口径不一致。
        # 模块属性查找而非 import 绑定，是为了 monkeypatch 能在测试里 spy。
        cost = _tokens_module.estimate_messages_tokens(
            list(group.messages), tokenizer=tokenizer
        )
        if cost <= token_budget - used:
            included.append(group)
            used += cost
            continue
        omitted.append(group)

    included.reverse()
    omitted.reverse()
    return included, omitted
