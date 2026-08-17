from __future__ import annotations

from types import SimpleNamespace

from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion
from klonet_agent.ops.privileged.workflow.response import ResponseAgent


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

    result = ResponseAgent(llm).render_readonly(
        "检查平台",
        EvidenceConclusion(),
    )

    assert result == "结论：已发现平台。\n\n1. 平台 A\n2. 平台 B"
    assert len(llm.calls) == 1
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "合理分段" in system_prompt
    assert "列表项分别换行" in system_prompt


def test_response_agent_receives_frozen_knowledge_evidence():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    llm = FakeLLM("已回答")
    bundle = EvidenceBundle(goal="检查平台")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("klonet_knowledge", {"query": "检查平台"}, "知识"),
        "source=startup_shutdown.md\n后端健康以 /server_health/ 为准",
    ))

    ResponseAgent(llm).render_readonly(
        "检查平台", EvidenceConclusion(), evidence_bundle=bundle,
    )

    prompt = llm.calls[0]["messages"][1]["content"]
    assert "startup_shutdown.md" in prompt
    assert "/server_health/" in prompt


def test_runtime_inventory_response_preserves_project_root_identity():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceClaim

    conclusion = EvidenceConclusion(confirmed_facts=[
        EvidenceClaim(
            "runtime_inventory_counts runtime_candidate_count=2 healthy_count=1 abnormal_count=1 code_only_count=1",
            ["ev-1"],
        ),
        EvidenceClaim(
            "runtime_instance platform=vemu project_root=/srv/formal backend_status=healthy master_port=45551 master_endpoint=healthy worker_port=45552 worker_endpoint=healthy",
            ["ev-1"],
        ),
        EvidenceClaim(
            "runtime_instance platform=vemu project_root=/srv/test backend_status=abnormal master_port=45554 master_endpoint=healthy worker_port=45555 worker_endpoint=unreachable",
            ["ev-1"],
        ),
        EvidenceClaim("runtime_code_only code_only_root=/srv/code-only", ["ev-1"]),
    ])

    result = ResponseAgent(FakeLLM("should not be used")).render_readonly(
        "检查有多少正常运行的平台", conclusion,
    )

    assert "正常运行实例（1）" in result
    assert "project_root=/srv/formal" in result
    assert "后端异常的运行候选（1）" in result
    assert "project_root=/srv/test" in result
    assert "只有代码、没有后端运行证据的目录（1）" in result
    assert "/srv/code-only" in result
