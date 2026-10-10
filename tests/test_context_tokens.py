"""context/tokens.py 的真路径与降级路径测试。

设计要点：
- 不依赖真实的 ``tokenizers`` 包或 HuggingFace 模型仓库；
  所有 tokenizer 都是测试内定义的最小 Protocol 对象。
- 离线场景（包缺失 / 加载失败）通过 monkeypatch 模拟，不删/装真实依赖。
- 启发式与真路径两路都得验证，确保改动不会让默认行为悄悄漂移。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


class FakeTokenizer:
    """最小可用的 tokenizer 替身：encode 返回固定 token 数。

    ``chars_per_token`` 控制每个 ``char`` 算几个 token——
    测试用它模拟"英文 4 char/token""中文 1.5 char/token"的差异。
    """

    def __init__(self, chars_per_token: float = 1.0):
        self._chars_per_token = chars_per_token

    def encode(self, text: str) -> list[int]:
        if not text:
            return []
        n = max(1, int(len(text) / self._chars_per_token + 0.999))
        return [0] * n


def _tokens():
    """延迟导入，确保 sys.path 已就位。"""

    from klonet_agent.context import tokens as tokens_module

    return tokens_module


def test_estimate_tokens_heuristic_baseline():
    """默认（无 tokenizer）走字符启发式，且对纯 ASCII 与中文都有合理结果。"""

    module = _tokens()
    module.reset_tokenizer_cache()
    # 纯 ASCII：12 字符 → 12/4 = 3 token
    assert module.estimate_tokens("Hello world!") == 3
    # 纯中文：6 字符 → 6/1.5 = 4 token
    assert module.estimate_tokens("你好世界你好") == 4


def test_estimate_tokens_with_mock_tokenizer():
    """传入 tokenizer 时按 len(encode) 算，不走启发式。"""

    module = _tokens()
    # chars_per_token=2 → 任意 8 字符都返回 4 token。
    fake = FakeTokenizer(chars_per_token=2.0)
    assert module.estimate_tokens("12345678", tokenizer=fake) == 4
    # 同样输入如果不传 tokenizer → 启发式 8/4 = 2 token，对比证明走了真路径。
    assert module.estimate_tokens("12345678") == 2


def test_estimate_tokens_empty_input_is_zero():
    """空串走任何路径都返回 0；真路径不调 encode。"""

    module = _tokens()
    assert module.estimate_tokens("") == 0
    assert module.estimate_tokens("", tokenizer=FakeTokenizer()) == 0


def test_estimate_value_tokens_with_tokenizer():
    """非字符串值走 json.dumps 后再 encode。"""

    import json

    module = _tokens()
    fake = FakeTokenizer(chars_per_token=1.0)  # 每个字符 1 token
    value = {"key": "abc"}
    expected = len(json.dumps(value, ensure_ascii=False))
    assert module.estimate_value_tokens(value, tokenizer=fake) == expected


def test_estimate_message_tokens_uses_tokenizer_for_content():
    """消息正文走 tokenizer，但 role 与 tool_call_id 仍按启发式。"""

    module = _tokens()
    fake = FakeTokenizer(chars_per_token=1.0)
    message = {
        "role": "user",
        "content": "12345",  # 5 chars → 5 token
        "tool_call_id": "X",  # 启发式: 1/4 → 1 token
    }
    # 固定开销 4 + role 启发式 1(4/4) + content 真路径 5 + tool_call_id 启发式 1 = 11
    assert module.estimate_message_tokens(message, tokenizer=fake) == 11


def test_estimate_message_tokens_ignores_non_dict_tool_calls():
    """tool_calls 里混入非字典项时不应崩溃。"""

    module = _tokens()
    message = {
        "role": "assistant",
        "content": "ok",
        "tool_calls": ["not-a-dict", {"function": {"name": "do", "arguments": "{}"}}],
    }
    # 启发式只把 dict 那条算进去；不会因为字符串工具调用而抛异常。
    assert module.estimate_message_tokens(message) > 0


def test_estimate_messages_tokens_with_tokenizer():
    """一组消息的总 token 累加正确，且每条都走真路径。"""

    module = _tokens()
    fake = FakeTokenizer(chars_per_token=2.0)
    messages = [
        {"role": "user", "content": "1234"},  # 2 token
        {"role": "assistant", "content": "5678"},  # 2 token
    ]
    # 不算 role/分隔固定开销，只看 content 真路径。
    assert module.estimate_messages_tokens(messages, tokenizer=fake) >= 4


def test_estimate_tool_tokens_with_tokenizer():
    """tool schema 序列化后按真路径算。"""

    import json

    module = _tokens()
    fake = FakeTokenizer(chars_per_token=1.0)
    tools = [{"type": "function", "function": {"name": "f"}}]
    serialized = json.dumps(tools, ensure_ascii=False)
    assert module.estimate_tool_tokens(tools, tokenizer=fake) == len(serialized)
    # 空列表 → 0
    assert module.estimate_tool_tokens(None, tokenizer=fake) == 0
    assert module.estimate_tool_tokens([], tokenizer=fake) == 0


def test_get_tokenizer_returns_none_when_id_empty():
    """tokenizer_id 为 None 或空串时直接返回 None，不尝试加载。"""

    module = _tokens()
    module.reset_tokenizer_cache()
    assert module.get_tokenizer(None) is None
    assert module.get_tokenizer("") is None


def test_get_tokenizer_returns_none_when_tokenizers_missing(monkeypatch):
    """tokenizers 包未装时（_HfTokenizer 为 None），返回 None 不报错。"""

    module = _tokens()
    module.reset_tokenizer_cache()
    monkeypatch.setattr(module, "_HfTokenizer", None, raising=False)
    assert module.get_tokenizer("deepseek-ai/DeepSeek-V3") is None


def test_get_tokenizer_returns_none_when_load_fails(monkeypatch):
    """from_pretrained 抛异常时缓存 None，下次直接走降级不再重试。"""

    module = _tokens()

    class _Boom:
        @staticmethod
        def from_pretrained(_id):
            raise RuntimeError("network down")

    monkeypatch.setattr(module, "_HfTokenizer", _Boom)
    module.reset_tokenizer_cache()
    assert module.get_tokenizer("deepseek-ai/DeepSeek-V3") is None
    # 第二次仍返回 None（命中显式 None 缓存）。
    assert module.get_tokenizer("deepseek-ai/DeepSeek-V3") is None


def test_get_tokenizer_caches_success(monkeypatch):
    """加载成功后同一 id 第二次直接返回缓存，不重复调 from_pretrained。"""

    module = _tokens()
    fake_instance = FakeTokenizer()

    calls = {"n": 0}

    class _Stub:
        @staticmethod
        def from_pretrained(_id):
            calls["n"] += 1
            return fake_instance

    monkeypatch.setattr(module, "_HfTokenizer", _Stub)
    module.reset_tokenizer_cache()
    first = module.get_tokenizer("deepseek-ai/DeepSeek-V3")
    second = module.get_tokenizer("deepseek-ai/DeepSeek-V3")
    assert first is fake_instance
    assert second is fake_instance
    assert calls["n"] == 1


def test_reset_tokenizer_cache_clears(monkeypatch):
    """reset_tokenizer_cache 之后，重新走完整加载路径。"""

    module = _tokens()

    class _Stub:
        @staticmethod
        def from_pretrained(_id):
            return FakeTokenizer()

    monkeypatch.setattr(module, "_HfTokenizer", _Stub)
    module.reset_tokenizer_cache()
    module.get_tokenizer("x")
    module.reset_tokenizer_cache()
    # 没有"已加载过"的副作用，重复调用仍应工作。
    assert module.get_tokenizer("x") is not None
