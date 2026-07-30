from __future__ import annotations

import json
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": messages, "tools": tools, "kwargs": kwargs}
        )
        content = self.contents.pop(0)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _planner_payload(**overrides):
    payload = {
        "goal": "restart nginx",
        "risk": "low",
        "steps": [
            {
                "step_id": "reload-nginx",
                "title": "reload nginx",
                "action": "reload_nginx",
                "args": {},
                "timeout": 30,
                "expected_changes": ["nginx restarts"],
                "preconditions": [],
                "postconditions": [
                    {"checker": "nginx_config_valid", "args": {}},
                    {"checker": "service_active", "args": {"service": "nginx"}},
                ],
                "rollback": "restore the previous reviewed configuration",
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_planner_uses_toolless_isolated_prompt_and_applies_deterministic_risk():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM([_planner_payload()])

    plan = PrivilegedPlannerAgent(llm).plan(
        "restart nginx",
        environment_context="systemd available",
    )

    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"] is None
    assert llm.calls[0]["kwargs"]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    assert llm.calls[0]["kwargs"]["reasoning_effort"] == "low"
    prompt = llm.calls[0]["messages"]
    assert prompt[0]["role"] == "system"
    assert "Planner" in prompt[0]["content"]
    assert "Verifier" not in prompt[0]["content"]
    assert plan.risk == "medium"
    assert plan.status == "awaiting_confirmation"
    assert plan.is_authorized is False
    assert plan.steps[0].postconditions == [
        {"checker": "nginx_config_valid", "args": {}},
        {"checker": "service_active", "args": {"service": "nginx"}},
    ]


def test_planner_allows_model_to_raise_but_not_lower_registry_risk():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM([_planner_payload(risk="high")])

    plan = PrivilegedPlannerAgent(llm).plan("restart nginx")

    assert plan.risk == "high"
    assert plan.status == "awaiting_confirmation"
    assert plan.steps[0].risk == "medium"
    assert plan.steps[0].approval_scope == "plan"


def test_planner_repairs_invalid_json_once():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(["not json", _planner_payload()])

    plan = PrivilegedPlannerAgent(llm).plan("restart nginx")

    assert plan.goal == "restart nginx"
    assert len(llm.calls) == 2
    assert "repair" in llm.calls[1]["messages"][-1]["content"].lower()


def test_planner_fails_safe_after_invalid_repair():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(["not json", "still not json"])

    try:
        PrivilegedPlannerAgent(llm).plan("restart nginx")
    except ValueError as exc:
        assert "valid privileged plan" in str(exc)
    else:
        raise AssertionError("planner must fail safe")


def test_planner_rejects_oversized_internal_plan_and_requests_compaction():
    oversized = {
        "goal": "deploy platform",
        "risk": "medium",
        "steps": [
            {
                "step_id": "step-%s" % index,
                "title": "检查 %s" % index,
                "command": "echo %s" % index,
                "risk": "readonly",
            }
            for index in range(9)
        ],
    }
    llm = FakeLLM([json.dumps(oversized), _planner_payload()])

    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(llm).plan("deploy platform")

    assert len(llm.calls) == 2
    assert "maximum of 8" in llm.calls[1]["messages"][-1]["content"]
    assert len(plan.steps) == 1


def test_planner_rejects_hard_denied_command_even_if_model_calls_it_low_risk():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    denied = _planner_payload(
                goal="wipe host",
                steps=[
                    {
                        "step_id": "wipe",
                        "title": "wipe",
                        "command": "sudo rm -rf /",
                        "risk": "low",
                        "postconditions": [],
                    }
                ],
            )
    llm = FakeLLM([denied, denied])

    try:
        PrivilegedPlannerAgent(llm).plan("wipe host")
    except ValueError as exc:
        assert "action + args" in str(exc)
    else:
        raise AssertionError("catastrophic command must be denied")


def test_complex_nginx_plan_waits_for_confirmation_and_gets_config_health_checks():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM([_planner_payload(goal="deploy nginx config")])

    plan = PrivilegedPlannerAgent(llm).plan("deploy nginx config")

    assert plan.status == "awaiting_confirmation"
    assert plan.authorized_hash == ""
    reload_checks = plan.steps[0].postconditions
    assert {"checker": "nginx_config_valid", "args": {}} in reload_checks
    assert {"checker": "service_active", "args": {"service": "nginx"}} in reload_checks


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


def test_verifier_accepts_deterministic_success_without_calling_llm():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "goal_achieved": True,
                    "reason": "evidence proves service is healthy",
                    "next_action": "",
                }
            )
        ]
    )
    step = _verified_step()

    decision = PrivilegedVerifierAgent(llm).verify_step(_plan_with_step(step), step)

    assert decision.status == "passed"
    assert decision.goal_achieved is True
    assert llm.calls == []


def test_verifier_does_not_send_intermediate_or_final_goal_to_llm():
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence, PrivilegedPlan
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "goal_achieved": True,
                    "reason": "current user and home directory were identified",
                    "next_action": "",
                }
            )
        ]
    )
    step = _verified_step()
    step.title = "识别当前用户和主目录"
    step.command = "whoami && echo $HOME"
    step.evidence = ExecutionEvidence(
        return_code=0,
        stdout="klonet-agent\n/home/klonet-agent\n",
    )
    plan = PrivilegedPlan(
        plan_id="priv-deploy",
        goal="部署 Klonet 平台",
        risk="medium",
        status="verifying",
        steps=[step],
    )

    decision = PrivilegedVerifierAgent(llm).verify_step(plan, step)

    assert decision.status == "passed"
    assert llm.calls == []


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


def test_verifier_does_not_allow_llm_to_veto_configured_success():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "inconclusive",
                    "goal_achieved": False,
                    "reason": "exit code alone does not prove the service restarted",
                    "next_action": "check service state",
                }
            )
        ]
    )
    step = _verified_step()

    decision = PrivilegedVerifierAgent(llm).verify_step(
        _plan_with_step(step),
        step,
    )

    assert decision.status == "passed"
    assert decision.goal_achieved is True
    assert llm.calls == []


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
