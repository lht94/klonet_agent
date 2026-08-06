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
    assert decision.goal_achieved is True
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
    assert decision.goal_achieved is True
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
    assert decision.goal_achieved is False
    assert len(llm.calls) == 1


def test_verifier_never_allows_llm_passed_to_override_nonzero_exit():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "goal_achieved": True,
                    "reason": "looks fine",
                    "next_action": "",
                }
            )
        ]
    )
    step = _verified_step(return_code=9)

    decision = PrivilegedVerifierAgent(llm).verify_step(_plan_with_step(step), step)

    assert decision.status == "failed"
    assert decision.goal_achieved is False
    assert "return_code=9" in decision.failures


def test_verifier_never_allows_zero_exit_to_hide_failed_state_check(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "goal_achieved": True,
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
    assert decision.goal_achieved is False
    assert decision.failures == ["file_exists"]


def test_verifier_treats_required_unavailable_checker_as_inconclusive():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = _verified_step(
        postconditions=[{"checker": "unknown-required-checker", "args": {}}]
    )

    decision = PrivilegedVerifierAgent(None).verify_step(_plan_with_step(step), step)

    assert decision.status == "inconclusive"
    assert decision.goal_achieved is False
    assert decision.missing_evidence == ["unknown-required-checker"]


def test_verifier_marks_timeout_blocked_and_does_not_reexecute():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = _verified_step(return_code=None, timed_out=True)

    decision = PrivilegedVerifierAgent(None).verify_step(_plan_with_step(step), step)

    assert decision.status == "blocked"
    assert decision.next_action == "inspect current state; do not auto-reexecute"
