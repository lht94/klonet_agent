from __future__ import annotations

import json
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append((messages, tools, kwargs))
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.outputs.pop(0)),
            )],
        )


def _bundle_and_conclusion():
    from klonet_agent.ops.privileged.v4.contracts import (
        EvidenceBundle, EvidenceClaim, EvidenceConclusion, EvidenceRecord,
        ProbeRequest,
    )

    bundle = EvidenceBundle(goal="检查 v4e2e 启动报错")
    record = bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("screen_session", {"session": "v4e2e_m"}, "traceback"),
        "Traceback truncated",
    ))
    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim("发现截断堆栈", [record.evidence_id])],
        uncertainties=[EvidenceClaim("底层异常未知", [record.evidence_id])],
    )
    return bundle, conclusion


def test_diagnostic_planner_compiles_discoverable_gap_into_registered_probe():
    from klonet_agent.ops.privileged.v4.diagnosis import V4DiagnosticPlannerAgent

    bundle, conclusion = _bundle_and_conclusion()
    llm = FakeLLM([json.dumps({
        "status": "continue",
        "reason": "需要完整日志",
        "probe_requests": [{
            "probe": "logs",
            "args": {"path": "/srv/v4/logs/master.log"},
            "purpose": "读取底层异常",
        }],
    }, ensure_ascii=False)])

    decision = V4DiagnosticPlannerAgent(llm).assess(
        bundle.goal, bundle, conclusion,
    )

    assert decision.status == "continue"
    assert decision.probe_requests[0].probe == "logs"
    assert decision.user_question == ""


def test_complete_non_diagnostic_query_does_not_spend_another_model_call():
    from klonet_agent.ops.privileged.v4.contracts import EvidenceConclusion
    from klonet_agent.ops.privileged.v4.diagnosis import V4DiagnosticPlannerAgent

    bundle, _ = _bundle_and_conclusion()
    llm = FakeLLM([])

    decision = V4DiagnosticPlannerAgent(llm).assess(
        "当前有多少健康平台", bundle, EvidenceConclusion(),
    )

    assert decision.status == "achieved"
    assert llm.calls == []


def test_diagnostic_planner_rejects_repeated_probe_and_repairs_contract():
    from klonet_agent.ops.privileged.v4.contracts import ProbeRequest
    from klonet_agent.ops.privileged.v4.diagnosis import V4DiagnosticPlannerAgent

    bundle, conclusion = _bundle_and_conclusion()
    repeated = ProbeRequest(
        "logs", {"path": "/srv/v4/logs/master.log"}, "读取底层异常",
    )
    llm = FakeLLM([
        json.dumps({
            "status": "continue",
            "probe_requests": [{
                "probe": repeated.probe,
                "args": repeated.args,
                "purpose": repeated.purpose,
            }],
        }, ensure_ascii=False),
        json.dumps({
            "status": "continue",
            "probe_requests": [{
                "probe": "ops_file",
                "args": {"path": "/srv/v4/mains/gun.py", "view": "head"},
                "purpose": "检查失败点代码",
            }],
        }, ensure_ascii=False),
    ])

    decision = V4DiagnosticPlannerAgent(llm).assess(
        bundle.goal,
        bundle,
        conclusion,
        attempted_keys={repeated.cache_key},
    )

    assert decision.status == "continue"
    assert [item.probe for item in decision.probe_requests] == ["ops_file"]
    assert len(llm.calls) == 2


def test_diagnostic_planner_allows_user_pause_only_for_target_choice():
    from klonet_agent.ops.privileged.v4.diagnosis import V4DiagnosticPlannerAgent

    bundle, conclusion = _bundle_and_conclusion()
    llm = FakeLLM([json.dumps({
        "status": "needs_user_decision",
        "user_question": "检测到两个同名实例，请指定项目根目录。",
        "probe_requests": [],
    }, ensure_ascii=False)])

    decision = V4DiagnosticPlannerAgent(llm).assess(
        bundle.goal, bundle, conclusion,
    )

    assert decision.status == "needs_user_decision"
    assert "项目根目录" in decision.user_question


def test_diagnostic_planner_repairs_attempt_to_ask_user_for_logs():
    from klonet_agent.ops.privileged.v4.diagnosis import V4DiagnosticPlannerAgent

    bundle, conclusion = _bundle_and_conclusion()
    llm = FakeLLM([
        json.dumps({
            "status": "needs_user_decision",
            "user_question": "请提供完整日志和异常堆栈。",
            "probe_requests": [],
        }, ensure_ascii=False),
        json.dumps({
            "status": "continue",
            "probe_requests": [{
                "probe": "process_logs",
                "args": {"pids": [1234], "project_root": "/srv/v4"},
                "purpose": "自行获取完整日志和堆栈",
            }],
        }, ensure_ascii=False),
    ])

    decision = V4DiagnosticPlannerAgent(llm).assess(
        bundle.goal, bundle, conclusion,
    )

    assert decision.status == "continue"
    assert decision.probe_requests[0].probe == "process_logs"
    assert len(llm.calls) == 2
