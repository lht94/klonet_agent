"""Token 估算工具。

本项目使用多个 OpenAI-compatible 供应商（gemini、GLM 等），没有公开 tokenizer，
无法精确计算 token。这里提供保守的字符启发式估算：

- ASCII/拉丁字符约 4 个字符 1 个 token；
- CJK（中日韩）字符约 1.5 个字符 1 个 token（中文实际约 1.5~2 token/字）。

估算结果只用于预算控制，真实 token 以 response.usage 为准；预算层已经
通过 safety margin 吸收估算误差，见 context/budget.py。
"""

from __future__ import annotations

import json
from typing import Any

# CJK 统一表意文字、扩展 A、兼容表意文字。
_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0xF900, 0xFAFF),
)

_ASCII_CHARS_PER_TOKEN = 4.0
_CJK_CHARS_PER_TOKEN = 1.5


def _count_cjk(text: str) -> int:
    """统计文本中的 CJK 字符数。"""

    count = 0
    for char in text:
        code = ord(char)
        for start, end in _CJK_RANGES:
            if start <= code <= end:
                count += 1
                break
    return count


def estimate_tokens(text: str) -> int:
    """按字符启发式估算一段文本的 token 数。"""

    if not text:
        return 0
    cjk = _count_cjk(text)
    ascii_chars = len(text) - cjk
    estimate = ascii_chars / _ASCII_CHARS_PER_TOKEN + cjk / _CJK_CHARS_PER_TOKEN
    # 至少 1 个 token，且向上取整保持保守。
    return max(1, int(estimate) + (1 if estimate % 1 else 0))


def estimate_value_tokens(value: Any) -> int:
    """估算任意 JSON 可序列化值的 token 数。"""

    if value is None:
        return 0
    if isinstance(value, str):
        return estimate_tokens(value)
    try:
        return estimate_tokens(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return estimate_tokens(str(value))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """估算单条 OpenAI 消息的 token 数，含 role、tool_call 等结构开销。"""

    total = 4  # role 与消息分隔的固定开销
    total += estimate_tokens(str(message.get("role", "")))
    total += estimate_value_tokens(message.get("content"))
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        total += estimate_value_tokens(function.get("name"))
        total += estimate_value_tokens(function.get("arguments"))
    if message.get("tool_call_id"):
        total += estimate_value_tokens(message["tool_call_id"])
    return total


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算一组消息的总 token 数。"""

    return sum(estimate_message_tokens(message) for message in messages)


def estimate_tool_tokens(tool_definitions: list[dict[str, Any]] | None) -> int:
    """估算工具 schema 注入请求时的 token 开销。"""

    if not tool_definitions:
        return 0
    try:
        return estimate_tokens(json.dumps(tool_definitions, ensure_ascii=False))
    except (TypeError, ValueError):
        return estimate_tokens(str(tool_definitions))
