"""Compatibility tests for the OpenAI-compatible LLM client."""


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
