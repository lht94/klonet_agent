from __future__ import annotations

from types import SimpleNamespace

from klonet_agent.ops.privileged.v4.contracts import EvidenceConclusion
from klonet_agent.ops.privileged.v4.response import V4ResponseAgent


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                )
            ]
        )


def test_readonly_response_preserves_model_layout_with_one_call():
    llm = FakeLLM("\n结论：已发现平台。\n\n1. 平台 A\n2. 平台 B\n")

    result = V4ResponseAgent(llm).render_readonly(
        "检查平台",
        EvidenceConclusion(),
    )

    assert result == "结论：已发现平台。\n\n1. 平台 A\n2. 平台 B"
    assert len(llm.calls) == 1
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "合理分段" in system_prompt
    assert "列表项分别换行" in system_prompt
