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
