"""Compatibility tests for the OpenAI-compatible LLM client."""


def test_all_llm_clients_have_a_finite_default_timeout_and_no_hidden_sdk_retry(
    monkeypatch,
):
    import sys
    from types import SimpleNamespace

    from klonet_agent.config import (
        DEFAULT_LLM_MAX_RETRIES,
        DEFAULT_LLM_TIMEOUT_SECONDS,
    )
    from klonet_agent.llm.client import LLMClient

    captured = {}

    def openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai))

    LLMClient(api_key="secret", base_url="https://llm.invalid/v1")

    assert captured["timeout"] == DEFAULT_LLM_TIMEOUT_SECONDS
    assert captured["max_retries"] == DEFAULT_LLM_MAX_RETRIES == 0


def test_explicit_llm_timeout_and_retry_policy_override_global_defaults(monkeypatch):
    import sys
    from types import SimpleNamespace

    from klonet_agent.llm.client import LLMClient

    captured = {}
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=lambda **kwargs: captured.update(kwargs) or object()),
    )

    LLMClient(
        api_key="secret", base_url="https://llm.invalid/v1",
        timeout=9, max_retries=1,
    )

    assert captured["timeout"] == 9
    assert captured["max_retries"] == 1


def test_complete_omits_reasoning_effort_when_sdk_does_not_accept_it():
    from klonet_agent.llm.client import LLMClient

    captured = {}

    class LegacyCompletions:
        @staticmethod
        def create(*, model, messages, stream, tools=None):
            captured.update(
                model=model,
                messages=messages,
                stream=stream,
                tools=tools,
            )
            return "response"

    llm = object.__new__(LLMClient)
    llm.model = "compatible-model"
    llm.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": LegacyCompletions()})()},
    )()

    result = llm.complete(
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        reasoning_effort="medium",
    )

    assert result == "response"
    assert captured["model"] == "compatible-model"


def test_complete_forwards_named_tool_choice_and_can_disable_reasoning():
    from klonet_agent.llm.client import LLMClient

    captured = {}

    class Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return "response"

    llm = object.__new__(LLMClient)
    llm.model = "compatible-model"
    llm.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    tool = {
        "type": "function",
        "function": {
            "name": "bind_action",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    choice = {
        "type": "function",
        "function": {"name": "bind_action"},
    }

    result = llm.complete(
        messages=[{"role": "user", "content": "bind"}],
        tools=[tool],
        tool_choice=choice,
        reasoning_effort=None,
        extra_body={"thinking": {"type": "disabled"}},
    )

    assert result == "response"
    assert captured["tool_choice"] == choice
    assert captured["tools"] == [tool]
    assert "reasoning_effort" not in captured
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_complete_forwards_max_tokens_bound():
    from klonet_agent.llm.client import LLMClient

    captured = {}

    class Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return "response"

    llm = object.__new__(LLMClient)
    llm.model = "compatible-model"
    llm.client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()

    assert llm.complete(
        messages=[{"role": "user", "content": "bounded"}],
        max_tokens=8000,
    ) == "response"
    assert captured["max_tokens"] == 8000
