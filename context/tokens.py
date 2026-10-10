"""Token 估算工具。

策略：
- **默认**：纯 stdlib 字符启发式（ASCII ≈ 4 char/token，CJK ≈ 1.5 char/token），
  用于 CI / 离线环境 / 闭源模型（Gemini 等）。
- **真路径**：当调用方显式传入一个 ``tokenizer``（``tokenizers`` 库的 ``Tokenizer``
  或同协议对象，提供 ``encode(text)`` 返回 ``list[int]``），本模块走真实 BPE 计数。

为什么默认不走真路径：
- ``tokenizers`` 是可选依赖；CI 与部分部署环境不装它。
- 真路径需要按模型 profile 加载不同的 tokenizer，加载失败必须能降级。
- 启发式精度足以承担"软阈值触发压缩"这一非阻塞决策；
  ``assert_within_hard_limit`` 这条**发送前最后一道闸**才是真路径的真正用武之地。

公开 tokenizer（DeepSeek-V3 / Qwen / GLM / LLaMA / Mistral 等）的绑定见
``context/budget.py`` 的 ``ModelContextProfile.tokenizer_id``；闭源模型
（Google Gemini API 系）保持 ``tokenizer_id=None``，永远走启发式。

真实 token 仍以供应商 ``response.usage`` 为准；预算层继续用
``safety_margin_tokens`` 吸收估算误差，见 ``context/budget.py``。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol

# CJK 统一表意文字、扩展 A、兼容表意文字。
_CJK_RANGES = (
    (0x4E00, 0x9FFF),
    (0x3400, 0x4DBF),
    (0xF900, 0xFAFF),
)

_ASCII_CHARS_PER_TOKEN = 4.0
_CJK_CHARS_PER_TOKEN = 1.5

# tokenizers 是可选依赖。导入失败时，所有真路径调用降级为启发式。
try:  # pragma: no cover - 简单 try/except，无分支覆盖价值
    from tokenizers import Tokenizer as _HfTokenizer  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 同上
    _HfTokenizer = None  # type: ignore[assignment,misc]


class TokenizerUnavailable(RuntimeError):
    """tokenizer 不可用时抛出的本地错误，供调用方决定是否降级。"""


class SupportsEncode(Protocol):
    """可作为 tokenizer 传入 estimate_* 的最小协议。

    ``tokenizers.Tokenizer``、``tiktoken.Encoding``、自定义 mock 都满足此协议。
    """

    def encode(self, text: str) -> list[int]:
        ...


# 模块级懒加载缓存：tokenizer_id -> 加载好的 tokenizer（或 None 表示降级）。
# 同一进程内对同一 tokenizer_id 只下载/解析一次，线程安全。
_TOKENIZER_CACHE: dict[str, SupportsEncode | None] = {}
_TOKENIZER_LOCK = threading.Lock()


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


def _heuristic_estimate(text: str) -> int:
    """字符启发式估算（默认与降级路径）。"""

    if not text:
        return 0
    cjk = _count_cjk(text)
    ascii_chars = len(text) - cjk
    estimate = ascii_chars / _ASCII_CHARS_PER_TOKEN + cjk / _CJK_CHARS_PER_TOKEN
    # 至少 1 个 token，且向上取整保持保守。
    return max(1, int(estimate) + (1 if estimate % 1 else 0))


def _tokenizer_count(tokenizer: SupportsEncode, text: str) -> int:
    """调 tokenizer 算 token 数。任何失败都抛 TokenizerUnavailable 由调用方降级。"""

    if not text:
        return 0
    try:
        return len(tokenizer.encode(text))
    except Exception as exc:  # tokenizer.encode 行为差异大，统包避免漏判
        raise TokenizerUnavailable(str(exc)) from exc


def _try_real_path(text: str, tokenizer: SupportsEncode | None) -> int | None:
    """尝试走真路径；tokenizer 为 None 或抛异常时返回 None 触发启发式。"""

    if tokenizer is None:
        return None
    try:
        return _tokenizer_count(tokenizer, text)
    except TokenizerUnavailable:
        return None


def get_tokenizer(tokenizer_id: str | None) -> SupportsEncode | None:
    """按 HuggingFace ID 懒加载 tokenizer，缓存到模块级单例。

    返回 ``None`` 表示不可用（id 为空、依赖缺失、加载失败），调用方应降级到
    ``estimate_tokens`` 的启发式分支。捕获异常刻意静音：真路径的失败不应
    让上游预算计算崩溃。
    """

    if not tokenizer_id:
        return None
    if _HfTokenizer is None:
        return None
    cached = _TOKENIZER_CACHE.get(tokenizer_id)
    if cached is not None or tokenizer_id in _TOKENIZER_CACHE:
        # 命中（含显式 None 缓存 = 上次加载失败过）
        return cached
    with _TOKENIZER_LOCK:
        # 双检：另一线程可能已经塞好。
        if tokenizer_id in _TOKENIZER_CACHE:
            return _TOKENIZER_CACHE[tokenizer_id]
        try:
            tokenizer = _HfTokenizer.from_pretrained(tokenizer_id)
        except Exception:
            # 加载失败显式缓存 None，避免每次调用都重试下载。
            _TOKENIZER_CACHE[tokenizer_id] = None
            return None
        _TOKENIZER_CACHE[tokenizer_id] = tokenizer
        return tokenizer


def reset_tokenizer_cache() -> None:
    """测试钩子：清空模块级 tokenizer 缓存。生产代码不应调用。"""

    with _TOKENIZER_LOCK:
        _TOKENIZER_CACHE.clear()


def estimate_tokens(text: str, *, tokenizer: SupportsEncode | None = None) -> int:
    """估算一段文本的 token 数。

    ``tokenizer`` 为 None 时走启发式；非 None 时走 ``len(tokenizer.encode(text))``。
    """

    return _try_real_path(text, tokenizer) or _heuristic_estimate(text)


def estimate_value_tokens(value: Any, *, tokenizer: SupportsEncode | None = None) -> int:
    """估算任意 JSON 可序列化值的 token 数。"""

    if value is None:
        return 0
    if isinstance(value, str):
        return estimate_tokens(value, tokenizer=tokenizer)
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = str(value)
    return estimate_tokens(serialized, tokenizer=tokenizer)


def estimate_message_tokens(
    message: dict[str, Any], *, tokenizer: SupportsEncode | None = None
) -> int:
    """估算单条 OpenAI 消息的 token 数，含 role、tool_call 等结构开销。

    结构开销（4 个 token 用于 role + 消息分隔）继续按字符启发式估算；
    消息正文走 ``tokenizer``（如有）。结构开销按启发式处理即可，因为它
    与模型无关、不随文本内容显著变化。
    """

    total = 4  # role 与消息分隔的固定开销
    total += _heuristic_estimate(str(message.get("role", "")))
    total += estimate_value_tokens(message.get("content"), tokenizer=tokenizer)
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        total += estimate_value_tokens(function.get("name"), tokenizer=tokenizer)
        total += estimate_value_tokens(function.get("arguments"), tokenizer=tokenizer)
    if message.get("tool_call_id"):
        total += estimate_value_tokens(message["tool_call_id"], tokenizer=tokenizer)
    return total


def estimate_messages_tokens(
    messages: list[dict[str, Any]], *, tokenizer: SupportsEncode | None = None
) -> int:
    """估算一组消息的总 token 数。"""

    return sum(estimate_message_tokens(m, tokenizer=tokenizer) for m in messages)


def estimate_tool_tokens(
    tool_definitions: list[dict[str, Any]] | None,
    *,
    tokenizer: SupportsEncode | None = None,
) -> int:
    """估算工具 schema 注入请求时的 token 开销。"""

    if not tool_definitions:
        return 0
    try:
        serialized = json.dumps(tool_definitions, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = str(tool_definitions)
    return estimate_tokens(serialized, tokenizer=tokenizer)
