from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": messages, "tools": tools, "kwargs": kwargs}
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.responses.pop(0))
                )
            ]
        )


def _semantic_payload(*, objective="inspect the current platform state"):
    return json.dumps(
        {
            "status": "ready",
            "goal": "inspect platform",
            "assumptions": [],
            "steps": [
                {
                    "step_id": "inspect",
                    "title": "检查平台",
                    "objective": objective,
                    "reason": "the current state is required before further work",
                    "evidence_refs": ["server facts"],
                    "depends_on": [],
                    "expected_effects": [],
                    "success_criteria": ["the current state is observed"],
                    "risk_suggestion": "readonly",
                }
            ],
        }
    )


def test_planner_can_probe_then_returns_semantic_plan_without_actions():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    probes = []
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "ports",
                            "args": {"ports": [8080]},
                            "purpose": "identify the current listener",
                        }
                    ],
                }
            ),
            _semantic_payload(),
        ]
    )
    planner = PrivilegedPlannerAgent(
        llm,
        probe_runner=lambda requests: (
            probes.extend(requests) or "port 8080 is not listening"
        ),
    )

    plan = planner.plan("inspect platform")

    assert probes[0]["probe"] == "ports"
    assert plan.schema_version == 3
    assert plan.steps[0].execution_binding is None
    assert plan.steps[0].action == ""
    assert plan.probe_history[0]["round"] == 1
    assert "action=" not in llm.calls[0]["messages"][0]["content"]


def test_failure_packet_context_routes_rag_to_troubleshooting():
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    seen = []
    builder = PrivilegedPlanContextBuilder(
        knowledge_search=lambda query, **kwargs: (
            seen.append((query, kwargs)) or "recovery runbook"
        ),
        environment_inspector=lambda args: "environment",
    )

    builder.build(
        "上一执行失败，请根据 Failure Packet 诊断并修复",
        supplemental_environment_context="Connection refused on Redis port 9368",
    )

    assert seen[0][1]["task_type"] == "troubleshooting"
    assert "Redis port 9368" in seen[0][0]


def test_execution_agent_maps_semantic_step_to_registered_action():
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload()])
    ).plan("inspect platform")
    binder = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "manual_checkpoint",
                    "args": {"reason": "record observed platform state"},
                    "binding_reason": "the registered checkpoint covers the objective",
                    "resolved_from_evidence": ["server facts"],
                    "postconditions": [
                        {"checker": "exit_code_zero", "args": {}}
                    ],
                }
            )
        ]
    )

    bound = PrivilegedExecutionAgent(binder).prepare_plan(
        plan,
        grounded_context=None,
    )

    binding = bound.steps[0].execution_binding
    assert binding.kind == "registered_action"
    assert binding.action == "manual_checkpoint"
    assert binding.approval_scope == "plan"
    assert bound.status == "awaiting_confirmation"
    assert "manual_checkpoint" in binder.calls[0]["messages"][1]["content"]


def test_unregistered_shell_requires_plan_then_exact_step_confirmation(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    target = tmp_path / "shell-result"
    planner = PrivilegedPlannerAgent(
        FakeLLM(
            [
                _semantic_payload(
                    objective="create the one-off deployment marker"
                )
            ]
        )
    )
    binder = PrivilegedExecutionAgent(
        FakeLLM(
            [
                json.dumps(
                    {
                        "status": "shell_artifact",
                        "script": "touch %s" % target,
                        "cwd": str(tmp_path),
                        "run_as": "",
                        "timeout": 10,
                        "declared_changes": [str(target)],
                        "rollback": "remove the marker file",
                        "binding_reason": "no registered action covers this marker",
                        "postconditions": [
                            {
                                "checker": "file_exists",
                                "args": {"path": str(target)},
                            }
                        ],
                    }
                )
            ]
        )
    )
    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        execution_agent=binder,
        executor=PrivilegedCommandExecutor(),
        verifier=PrivilegedVerifierAgent(None),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
    )

    waiting = workflow.submit("create marker")
    plan_id = waiting.plan.plan_id
    step_id = waiting.plan.steps[0].step_id
    after_plan_confirmation = workflow.handle_command(
        "confirm-priv %s" % plan_id
    )
    details = workflow.handle_command("show-priv %s" % plan_id)
    completed = workflow.handle_command(
        "confirm-priv-step %s %s" % (plan_id, step_id)
    )

    assert waiting.kind == "awaiting_confirmation"
    assert after_plan_confirmation.kind == "awaiting_step_confirmation"
    assert "固定脚本如下" in details.message
    assert "脚本 SHA-256" in details.message
    assert completed.kind == "completed"
    assert target.is_file()
    assert completed.plan.is_authorized is True
    assert (
        completed.plan.steps[0].execution_binding.shell_artifact.status
        == "executed"
    )


def test_shell_policy_hard_denies_dynamic_egress_secrets_and_agent_changes(
    tmp_path,
):
    from klonet_agent.ops.privileged.shell_artifact import (
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    policy = ShellArtifactPolicy()
    scripts = (
        "echo $(id)",
        "curl https://example.test/upload",
        "cat /etc/shadow",
        "sed -i 's/x/y/' ops/privileged/policy.py",
        "PASSWORD=plain-text command true",
        "python3 -c 'print(1)'",
    )
    for index, script in enumerate(scripts):
        artifact = create_shell_artifact(
            artifact_id="shell-%s" % index,
            script=script,
            cwd=str(tmp_path),
            run_as="",
            timeout=10,
            environment_fingerprint="",
            declared_changes=[],
            rollback="",
            nonce="nonce-%s" % index,
        )
        assert policy.validate(artifact), script


def test_executor_refuses_changed_expired_drifted_or_reused_shell(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.shell_artifact import create_shell_artifact

    artifact = create_shell_artifact(
        artifact_id="shell-once",
        script="touch %s" % (tmp_path / "once"),
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env-a",
        declared_changes=[str(tmp_path / "once")],
        rollback="remove once",
        nonce="nonce",
    )
    artifact.status = "approved"
    artifact.approved_contract_hash = artifact.contract_hash
    step = PrivilegedStep(
        step_id="once",
        title="one time",
        objective="create marker once",
        reason="test",
        success_criteria=["marker exists"],
        risk="high",
        approval_scope="step",
        execution_binding=ExecutionBinding(
            kind="shell_artifact",
            risk="high",
            approval_scope="step",
            shell_artifact=artifact,
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        ),
    )
    drifted = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-b"
    ).execute(step)
    assert drifted.stderr == "shell_artifact_environment_fingerprint_changed"

    artifact.environment_fingerprint = "env-a"
    completed = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    reused = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert completed.return_code == 0
    assert reused.stderr == "shell_artifact_not_exactly_approved"

    artifact.status = "approved"
    artifact.expires_at = "2000-01-01T00:00:00+00:00"
    artifact.approved_contract_hash = artifact.contract_hash
    expired = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert expired.stderr == "shell_artifact_expired"

    artifact.status = "approved"
    artifact.expires_at = "2999-01-01T00:00:00+00:00"
    artifact.approved_contract_hash = artifact.contract_hash
    artifact.script += "echo changed\n"
    changed = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert changed.stderr == "shell_artifact_contract_not_exactly_approved"


def test_verifier_probe_evidence_is_persisted_and_cannot_override_failure():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = PrivilegedStep(
        step_id="verify",
        title="verify",
        objective="verify service",
        reason="test",
        success_criteria=["service is active"],
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
        evidence=ExecutionEvidence(return_code=9, stderr="failed"),
    )
    plan = PrivilegedPlan(
        plan_id="priv-v3",
        goal="verify service",
        risk="medium",
        steps=[step],
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "service",
                            "args": {"services": ["nginx"]},
                            "purpose": "observe service state",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "passed",
                    "summary": "service looks active",
                    "confirmed_facts": ["nginx active"],
                    "failed_criteria": [],
                    "missing_evidence": [],
                    "reflection": "the command itself still failed",
                    "recommended_next_focus": "inspect command failure",
                }
            ),
        ]
    )
    verifier = PrivilegedVerifierAgent(
        llm,
        probe_runner=lambda requests: "nginx active",
    )

    decision = verifier.verify_step(plan, step)

    assert decision.status == "failed"
    assert decision.probe_history[0]["requests"][0]["probe"] == "service"
    assert "return_code=9" in decision.failures


def test_replan_rejects_same_failed_remaining_route(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
        VerificationDecision,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    def step(step_id):
        return PrivilegedStep(
            step_id=step_id,
            title="启动平台",
            objective="start the Klonet platform",
            reason="the user requested deployment",
            success_criteria=["platform is healthy"],
            risk="medium",
            status="approved",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                action="manual_checkpoint",
                args={"reason": "same route"},
                risk="medium",
                postconditions=[{"checker": "exit_code_zero", "args": {}}],
            ),
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        )

    original = PrivilegedPlan(
        plan_id="priv-loop",
        goal="deploy platform",
        risk="medium",
        status="approved",
        steps=[step("start")],
    )
    original.authorize()
    replacement = PrivilegedPlan(
        plan_id="priv-replacement",
        goal="deploy platform",
        risk="medium",
        steps=[step("start-again")],
    )

    class Planner:
        def plan(self, goal, **kwargs):
            del goal, kwargs
            return replacement

    class PreboundExecutionAgent:
        @staticmethod
        def prepare_plan(plan, *, grounded_context):
            del grounded_context
            plan.status = "awaiting_confirmation"
            return plan

    class ContextBuilder:
        @staticmethod
        def current_environment_fingerprint():
            return "env"

        @staticmethod
        def build(goal, **kwargs):
            del goal, kwargs
            return GroundedPlanContext(
                knowledge_evidence="recovery runbook",
                environment_evidence="same failure evidence",
                action_catalog="capability summary",
            )

    class Executor:
        @staticmethod
        def execute(current_step):
            del current_step
            return ExecutionEvidence(return_code=1, stderr="same failure")

    class Verifier:
        @staticmethod
        def verify_step(plan, current_step):
            del plan, current_step
            return VerificationDecision(
                status="failed",
                reason="same failure",
                reflection="the route did not change the prerequisite",
            )

    workflow = PrivilegedOpsWorkflow(
        planner=Planner(),
        execution_agent=PreboundExecutionAgent(),
        executor=Executor(),
        verifier=Verifier(),
        context_builder=ContextBuilder(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
    )
    workflow.store.save(original)

    result = workflow.execute("priv-loop")

    assert result.kind == "paused"
    assert "没有实质差异" in result.message
    assert result.plan.replan_attempts == 1


def test_schema_v2_raw_command_migrates_to_non_executable_audit_record():
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan

    raw = {
        "schema_version": 2,
        "plan_id": "priv-old",
        "goal": "deploy",
        "risk": "medium",
        "status": "approved",
        "authorized_hash": hashlib.sha256(b"old").hexdigest(),
        "steps": [
            {
                "step_id": "legacy",
                "title": "legacy",
                "command": "klonet deploy",
                "risk": "medium",
                "status": "approved",
            }
        ],
    }

    plan = PrivilegedPlan.from_dict(raw)

    assert plan.schema_version == 3
    assert plan.authorized_hash == ""
    assert plan.steps[0].execution_binding.kind == "legacy_command"
    assert plan.steps[0].status == "blocked"
