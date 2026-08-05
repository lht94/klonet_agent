from __future__ import annotations

import json
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output))]
        )


def test_discovery_runs_registered_probes_and_reuses_duplicate_request():
    from klonet_agent.ops.privileged.v4.discovery import V4DiscoveryAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "platform_instances",
                            "args": {},
                            "purpose": "discover candidates",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "platform_instances",
                            "args": {},
                            "purpose": "repeat must use cache",
                        },
                        {
                            "probe": "screen",
                            "args": {},
                            "purpose": "corroborate runtime",
                        },
                    ],
                }
            ),
            json.dumps({"status": "ready"}),
        ]
    )
    probe_calls = []

    def run_probes(requests):
        probe_calls.append(requests)
        return "evidence for %s" % requests[0]["probe"]

    bundle = V4DiscoveryAgent(llm, probe_runner=run_probes).collect(
        "检查有哪些平台"
    )

    assert [item.request.probe for item in bundle.records] == [
        "platform_instances",
        "screen",
    ]
    assert [item[0]["probe"] for item in probe_calls] == [
        "platform_instances",
        "screen",
    ]
    assert len(llm.calls) == 3


def test_discovery_marks_budget_exhaustion_instead_of_replanning_forever():
    from klonet_agent.ops.privileged.v4.contracts import DiscoveryBudget
    from klonet_agent.ops.privileged.v4.discovery import V4DiscoveryAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {"probe": "screen", "args": {}, "purpose": "screen"}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {"probe": "ports", "args": {}, "purpose": "ports"}
                    ],
                }
            ),
        ]
    )
    bundle = V4DiscoveryAgent(
        llm,
        probe_runner=lambda requests: "ok",
        budget_factory=lambda: DiscoveryBudget(max_rounds=1),
    ).collect("inspect")

    assert bundle.budget_exhausted is True
    assert [item.request.probe for item in bundle.records] == ["screen"]
    assert len(llm.calls) == 2


def test_discovery_collect_requests_adds_only_fresh_registered_evidence():
    from klonet_agent.ops.privileged.v4.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.v4.discovery import V4DiscoveryAgent

    existing = ProbeRequest("screen", {}, "existing")
    bundle = EvidenceBundle(goal="deploy")
    bundle.add(EvidenceRecord.from_probe(existing, "screen evidence"))
    calls = []
    agent = V4DiscoveryAgent(
        FakeLLM([]),
        probe_runner=lambda requests: calls.append(requests) or "port evidence",
    )

    returned = agent.collect_requests(
        [existing, ProbeRequest("ports", {"ports": [47001]}, "freeze port")],
        bundle,
    )

    assert returned is bundle
    assert [item.request.probe for item in bundle.records] == ["screen", "ports"]
    assert len(calls) == 1
    assert calls[0][0]["probe"] == "ports"


def test_synthesis_repairs_unknown_evidence_reference_once():
    from klonet_agent.ops.privileged.v4.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.v4.synthesis import V4EvidenceSynthesizer

    bundle = EvidenceBundle(goal="inspect")
    record = bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("screen", {}, "screen"),
            "vemu_uestc_m",
        )
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "confirmed_facts": [
                        {"text": "one platform", "evidence_refs": ["ev-invented"]}
                    ]
                }
            ),
            json.dumps(
                {
                    "confirmed_facts": [
                        {"text": "one platform", "evidence_refs": [record.evidence_id]}
                    ],
                    "uncertainties": [],
                    "missing_decisions": [],
                }
            ),
        ]
    )

    conclusion = V4EvidenceSynthesizer(llm).synthesize("inspect", bundle)

    assert conclusion.confirmed_facts[0].text == "one platform"
    assert len(llm.calls) == 2


def test_synthesis_promotes_user_selected_screen_git_mapping_deterministically():
    from klonet_agent.ops.privileged.v4.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.v4.synthesis import V4EvidenceSynthesizer

    bundle = EvidenceBundle(goal="use Screen prefix vemu_uestc as source")
    record = bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("screen", {}, "source"),
            (
                "screen_runtime\n"
                "session=vemu_uestc_m screen_pid=100 "
                "runtime_cwds=/home/lzl/vemu_uestc/mains "
                "git_roots=/home/lzl/vemu_uestc\n"
                "screen_git_repositories\ninspect_git_repository\n"
                "path=/home/lzl/vemu_uestc inside_work_tree=true "
                "revision=af418698\nstatus=## develop...origin/develop\n"
                "remotes=origin\tgitee:uestc-minenet/vemu_uestc.git (fetch)"
            ),
        )
    )
    llm = FakeLLM(
        [json.dumps({"confirmed_facts": [], "uncertainties": [], "missing_decisions": []})]
    )

    conclusion = V4EvidenceSynthesizer(llm).synthesize(bundle.goal, bundle)

    fact = conclusion.confirmed_facts[0]
    assert "/home/lzl/vemu_uestc" in fact.text
    assert "gitee:uestc-minenet/vemu_uestc.git" in fact.text
    assert "develop" in fact.text
    assert fact.evidence_refs == [record.evidence_id]


def test_response_fallback_reports_facts_and_uncertainty_without_llm():
    from klonet_agent.ops.privileged.v4.contracts import (
        EvidenceClaim,
        EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.v4.response import V4ResponseAgent

    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim("确认 vemu_uestc 正在运行", ["ev-1"])],
        uncertainties=[EvidenceClaim("agent102 证据不足", ["ev-2"])],
    )

    message = V4ResponseAgent(None).render_readonly("检查平台", conclusion)

    assert "确认 vemu_uestc 正在运行" in message
    assert "agent102 证据不足" in message
    assert "不确定" in message
