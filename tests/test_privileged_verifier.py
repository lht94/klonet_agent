from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class FakeLLM:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": messages, "tools": tools, "kwargs": kwargs}
        )
        content = self.contents.pop(0)
        if tools:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name=tools[0]["function"]["name"],
                            arguments=content,
                        )
                    )
                ],
            )
        else:
            message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _verified_step(return_code=0, timed_out=False, postconditions=None):
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence, PrivilegedStep

    return PrivilegedStep(
        step_id="restart-nginx",
        title="restart nginx",
        command="sudo systemctl restart nginx",
        risk="medium",
        status="executed",
        postconditions=postconditions
        if postconditions is not None
        else [{"checker": "exit_code_zero", "args": {}}],
        evidence=ExecutionEvidence(
            return_code=return_code,
            timed_out=timed_out,
            stdout="ok",
        ),
    )


def _plan_with_step(step):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan

    return PrivilegedPlan(
        plan_id="priv-123",
        goal="restart nginx",
        risk="medium",
        status="verifying",
        steps=[step],
    )


def _diagnostic_goal_state():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceClaim,
        EvidenceConclusion,
        EvidenceRecord,
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


def test_verifier_goal_requests_registered_evidence_for_discoverable_gap():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle, conclusion = _diagnostic_goal_state()
    llm = FakeLLM([json.dumps({
        "status": "need_evidence",
        "reason": "需要完整日志",
        "evidence_requests": [{
            "probe": "logs",
            "args": {"path": "/srv/app/logs/master.log"},
            "purpose": "读取底层异常",
        }],
    }, ensure_ascii=False)])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        bundle.goal, bundle, conclusion, goal_kind="causal_diagnosis",
    )

    assert outcome.status == "need_evidence"
    assert outcome.evidence_requests[0].probe == "logs"
    assert outcome.user_question == ""


def test_verifier_goal_requires_causal_evidence_before_achieved():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle, conclusion = _diagnostic_goal_state()
    llm = FakeLLM([
        json.dumps({"status": "achieved", "reason": "发现异常"}),
        json.dumps({
            "status": "need_evidence",
            "reason": "尚未确认根因",
            "evidence_requests": [{
                "probe": "process_logs",
                "args": {"pids": [1234], "project_root": "/srv/app"},
                "purpose": "获取完整异常",
            }],
        }, ensure_ascii=False),
    ])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        bundle.goal, bundle, conclusion, goal_kind="causal_diagnosis",
    )

    assert outcome.status == "need_evidence"
    assert len(llm.calls) == 2


def test_verifier_does_not_treat_unknown_root_cause_text_as_causal_evidence():
    from klonet_agent.ops.privileged.workflow.contracts import (
        DiagnosisAssessment, EvidenceClaim, EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle, _ = _diagnostic_goal_state()
    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim(
            "已经发现报错，但根因尚未获取",
            [bundle.records[0].evidence_id],
        )],
        diagnosis=DiagnosisAssessment(
            status="symptom_confirmed",
            symptom="gunicorn worker 初始化报错",
            evidence_refs=[bundle.records[0].evidence_id],
        ),
    )
    llm = FakeLLM([
        json.dumps({"status": "achieved", "reason": "已经发现报错"}),
        json.dumps({
            "status": "need_evidence",
            "reason": "根因仍未确认",
            "evidence_requests": [{
                "probe": "process_logs",
                "args": {"pids": [1234], "project_root": "/srv/app"},
                "purpose": "获取完整异常并定位根因",
            }],
        }, ensure_ascii=False),
    ])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        "v4e2e_m 现在是不是有报错？", bundle, conclusion,
        goal_kind="causal_diagnosis",
    )

    assert outcome.status == "need_evidence"
    assert len(llm.calls) == 2


def test_verifier_accepts_structured_causal_chain():
    from klonet_agent.ops.privileged.workflow.contracts import (
        DiagnosisAssessment, EvidenceClaim, EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle, _ = _diagnostic_goal_state()
    evidence_id = bundle.records[0].evidence_id
    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim(
            "日志目录不存在导致处理器初始化失败", [evidence_id],
        )],
        diagnosis=DiagnosisAssessment(
            status="cause_confirmed",
            symptom="worker 初始化报错",
            failure_point="ConcurrentRotatingFileHandler 创建锁文件",
            root_cause="配置解析到不存在的日志目录",
            evidence_refs=[evidence_id],
        ),
    )
    llm = FakeLLM([
        json.dumps({"status": "achieved", "reason": "根因已确认"}),
    ])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        "v4e2e_m 现在是不是有报错？", bundle, conclusion,
        goal_kind="causal_diagnosis",
    )

    assert outcome.status == "achieved"
    assert len(llm.calls) == 1


def test_verifier_goal_rejects_offloading_logs_to_user():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle, conclusion = _diagnostic_goal_state()
    llm = FakeLLM([
        json.dumps({
            "status": "needs_user_decision",
            "user_question": "请提供完整日志和异常堆栈。",
        }, ensure_ascii=False),
        json.dumps({
            "status": "need_evidence",
            "evidence_requests": [{
                "probe": "process_logs",
                "args": {"pids": [1234], "project_root": "/srv/app"},
                "purpose": "自行获取完整日志",
            }],
        }, ensure_ascii=False),
    ])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        bundle.goal, bundle, conclusion, goal_kind="causal_diagnosis",
    )

    assert outcome.status == "need_evidence"
    assert len(llm.calls) == 2


def test_verifier_goal_allows_only_a_genuine_user_choice():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle, conclusion = _diagnostic_goal_state()
    llm = FakeLLM([json.dumps({
        "status": "needs_user_decision",
        "user_question": "检测到两个同名实例，请指定项目根目录。",
    }, ensure_ascii=False)])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        bundle.goal, bundle, conclusion, goal_kind="causal_diagnosis",
    )

    assert outcome.status == "needs_user_decision"
    assert "项目根目录" in outcome.user_question


def test_verifier_goal_does_not_repeat_attempted_evidence_request():
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle, conclusion = _diagnostic_goal_state()
    repeated = ProbeRequest(
        "logs", {"path": "/srv/app/logs/master.log"}, "读取底层异常",
    )
    llm = FakeLLM([
        json.dumps({
            "status": "need_evidence",
            "evidence_requests": [{
                "probe": repeated.probe,
                "args": repeated.args,
                "purpose": repeated.purpose,
            }],
        }, ensure_ascii=False),
        json.dumps({
            "status": "need_evidence",
            "evidence_requests": [{
                "probe": "ops_file",
                "args": {"path": "/srv/app/mains/gun.py", "view": "head"},
                "purpose": "检查失败点源码",
            }],
        }, ensure_ascii=False),
    ])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        bundle.goal,
        bundle,
        conclusion,
        attempted_keys={repeated.cache_key},
    )

    assert [item.probe for item in outcome.evidence_requests] == ["ops_file"]
    assert len(llm.calls) == 2


def test_verifier_goal_decides_replan_after_supported_execution_failure():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceClaim, EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle, _ = _diagnostic_goal_state()
    conclusion = EvidenceConclusion(confirmed_facts=[EvidenceClaim(
        "原计划未达到目标，根因是日志目录不存在",
        [bundle.records[0].evidence_id],
    )])
    llm = FakeLLM([json.dumps({
        "status": "need_replan",
        "reason": "根因已确认，需要修订未完成步骤",
    }, ensure_ascii=False)])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        "修复 v4e2e master",
        bundle,
        conclusion,
        phase="post_execution",
    )

    assert outcome.status == "need_replan"


def test_verifier_goal_prioritizes_referenced_evidence_with_a_bounded_payload():
    from klonet_agent.ops.privileged.workflow.contracts import (
        DiagnosisAssessment, EvidenceBundle, EvidenceClaim, EvidenceConclusion,
        EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    bundle = EvidenceBundle(goal="检查启动报错根因")
    for index in range(12):
        bundle.add(EvidenceRecord.from_probe(
            ProbeRequest("process", {"keywords": [str(index)]}, "noise"),
            "irrelevant-process-output-" + ("x" * 10000),
        ))
    causal = bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("screen_session", {"session": "target"}, "traceback"),
        "FileNotFoundError: /srv/app/logs/.__access.lock",
    ))
    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim(
            "根因是日志目录不存在，导致日志处理器无法创建锁文件",
            [causal.evidence_id],
        )],
        diagnosis=DiagnosisAssessment(
            status="cause_confirmed",
            symptom="日志处理器初始化报错",
            failure_point="创建日志锁文件",
            root_cause="日志目录不存在",
            evidence_refs=[causal.evidence_id],
        ),
    )
    llm = FakeLLM([json.dumps({"status": "achieved", "reason": "根因已确认"})])

    outcome = PrivilegedVerifierAgent(llm).verify_goal(
        bundle.goal, bundle, conclusion, goal_kind="causal_diagnosis",
    )

    payload = llm.calls[0]["messages"][1]["content"]
    assert outcome.status == "achieved"
    assert "FileNotFoundError" in payload
    assert len(payload) < 50000


def test_verifier_uses_agent_when_exit_code_is_the_only_evidence():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "summary": "evidence proves service is healthy",
                    "confirmed_facts": ["exit code is zero"],
                    "failed_criteria": [],
                    "missing_evidence": [],
                    "reflection": "",
                    "recommended_next_focus": "",
                }
            )
        ]
    )
    step = _verified_step()

    decision = PrivilegedVerifierAgent(llm).verify_step(_plan_with_step(step), step)

    assert decision.status == "passed"
    assert decision.step_achieved is True
    assert len(llm.calls) == 1


def test_verifier_receives_goal_and_semantic_step_context():
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence, PrivilegedPlan
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "summary": "current user and home directory were identified",
                    "confirmed_facts": ["current user observed"],
                    "failed_criteria": [],
                    "missing_evidence": [],
                    "reflection": "",
                    "recommended_next_focus": "",
                }
            )
        ]
    )
    step = _verified_step()
    step.title = "璇嗗埆褰撳墠鐢ㄦ埛鍜屼富鐩綍"
    step.command = "whoami && echo $HOME"
    step.evidence = ExecutionEvidence(
        return_code=0,
        stdout="klonet-agent\n/home/klonet-agent\n",
    )
    plan = PrivilegedPlan(
        plan_id="priv-deploy",
        goal="閮ㄧ讲 Klonet 骞冲彴",
        risk="medium",
        status="verifying",
        steps=[step],
    )

    decision = PrivilegedVerifierAgent(llm).verify_step(plan, step)

    assert decision.status == "passed"
    assert len(llm.calls) == 1
    assert "閮ㄧ讲 Klonet 骞冲彴" in llm.calls[0]["messages"][1]["content"]


def test_verifier_trusts_passed_state_checker_without_calling_llm(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    target = tmp_path / "ready"
    target.write_text("ready", encoding="utf-8")
    step = _verified_step(
        postconditions=[
            {"checker": "file_exists", "args": {"path": str(target)}},
        ],
    )
    llm = FakeLLM([])

    decision = PrivilegedVerifierAgent(llm).verify_step(
        _plan_with_step(step),
        step,
    )

    assert decision.status == "passed"
    assert decision.step_achieved is True
    assert decision.reason == "all deterministic state checks passed"
    assert llm.calls == []


def test_verifier_can_mark_exit_code_only_evidence_inconclusive():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "inconclusive",
                    "summary": "exit code alone does not prove the service restarted",
                    "confirmed_facts": ["exit code is zero"],
                    "failed_criteria": [],
                    "missing_evidence": ["service state"],
                    "reflection": "execution success is not state success",
                    "recommended_next_focus": "check service state",
                }
            )
        ]
    )
    step = _verified_step()

    decision = PrivilegedVerifierAgent(llm).verify_step(
        _plan_with_step(step),
        step,
    )

    assert decision.status == "inconclusive"
    assert decision.step_achieved is False
    assert len(llm.calls) == 1


def test_verifier_never_allows_llm_passed_to_override_nonzero_exit():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "step_achieved": True,
                    "reason": "looks fine",
                    "next_action": "",
                }
            )
        ]
    )
    step = _verified_step(return_code=9)

    decision = PrivilegedVerifierAgent(llm).verify_step(_plan_with_step(step), step)

    assert decision.status == "failed"
    assert decision.step_achieved is False
    assert "return_code=9" in decision.failures


def test_verifier_never_allows_zero_exit_to_hide_failed_state_check(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "step_achieved": True,
                    "reason": "command returned zero",
                    "next_action": "",
                }
            )
        ]
    )
    step = _verified_step(
        return_code=0,
        postconditions=[
            {"checker": "file_exists", "args": {"path": str(tmp_path / "missing")}}
        ],
    )

    decision = PrivilegedVerifierAgent(llm).verify_step(_plan_with_step(step), step)

    assert decision.status == "failed"
    assert decision.step_achieved is False
    assert decision.failures == ["file_exists"]


def test_verifier_treats_required_unavailable_checker_as_inconclusive():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = _verified_step(
        postconditions=[{"checker": "unknown-required-checker", "args": {}}]
    )

    decision = PrivilegedVerifierAgent(None).verify_step(_plan_with_step(step), step)

    assert decision.status == "inconclusive"
    assert decision.step_achieved is False
    assert decision.missing_evidence == ["unknown-required-checker"]


def test_verifier_marks_timeout_blocked_and_does_not_reexecute():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = _verified_step(return_code=None, timed_out=True)

    decision = PrivilegedVerifierAgent(None).verify_step(_plan_with_step(step), step)

    assert decision.status == "blocked"
    assert decision.next_action == "inspect current state; do not auto-reexecute"
