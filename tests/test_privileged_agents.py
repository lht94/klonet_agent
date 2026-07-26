from __future__ import annotations

import json
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def complete(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        content = self.contents.pop(0)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _planner_payload(**overrides):
    payload = {
        "goal": "restart nginx",
        "risk": "low",
        "steps": [
            {
                "step_id": "restart-nginx",
                "title": "restart nginx",
                "command": "sudo systemctl restart nginx",
                "risk": "low",
                "timeout": 30,
                "expected_changes": ["nginx restarts"],
                "preconditions": [],
                "postconditions": [],
                "rollback": "sudo systemctl restart nginx",
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
    prompt = llm.calls[0]["messages"]
    assert prompt[0]["role"] == "system"
    assert "Planner" in prompt[0]["content"]
    assert "Verifier" not in prompt[0]["content"]
    assert plan.risk == "medium"
    assert plan.status == "approved"
    assert plan.is_authorized is True
    assert plan.steps[0].postconditions == [
        {"checker": "service_active", "args": {"service": "nginx"}}
    ]


def test_planner_top_level_risk_is_also_a_non_overridable_lower_bound():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM([_planner_payload(risk="high")])

    plan = PrivilegedPlannerAgent(llm).plan("restart nginx")

    assert plan.risk == "high"
    assert plan.status == "awaiting_confirmation"
    assert plan.steps[0].risk == "high"
    assert plan.steps[0].approval_scope == "step"


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


def test_planner_rejects_hard_denied_command_even_if_model_calls_it_low_risk():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(
        [
            _planner_payload(
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
        ]
    )

    try:
        PrivilegedPlannerAgent(llm).plan("wipe host")
    except PermissionError as exc:
        assert "hard-denied" in str(exc)
    else:
        raise AssertionError("catastrophic command must be denied")


def test_complex_nginx_plan_waits_for_confirmation_and_gets_config_health_checks():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(
        [
            _planner_payload(
                goal="deploy nginx config",
                steps=[
                    {
                        "step_id": "write-config",
                        "title": "write config",
                        "command": "sudo tee /etc/nginx/conf.d/klonet.conf",
                        "risk": "medium",
                        "rollback": "sudo rm /etc/nginx/conf.d/klonet.conf",
                    },
                    {
                        "step_id": "reload-nginx",
                        "title": "reload nginx",
                        "command": "sudo systemctl reload nginx",
                        "risk": "medium",
                        "rollback": "sudo systemctl restart nginx",
                    },
                ],
            )
        ]
    )

    plan = PrivilegedPlannerAgent(llm).plan("deploy nginx config")

    assert plan.status == "awaiting_confirmation"
    assert plan.authorized_hash == ""
    reload_checks = plan.steps[1].postconditions
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


def test_verifier_is_toolless_and_accepts_deterministic_success():
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
    assert llm.calls[0]["tools"] is None
    assert "Verifier" in llm.calls[0]["messages"][0]["content"]
    assert "Planner" not in llm.calls[0]["messages"][0]["content"]


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
