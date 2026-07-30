from __future__ import annotations

import pytest


def _step(
    step_id="step-1",
    command="echo ok",
    risk="low",
    approval_scope="plan",
    action="manual_checkpoint",
):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )

    binding = (
        ExecutionBinding(
            kind="registered_action",
            action=action,
            args={"reason": "workflow test"},
            risk=risk,
            approval_scope=approval_scope,
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        )
        if action
        else None
    )
    return PrivilegedStep(
        step_id=step_id,
        title=step_id,
        objective="complete %s" % step_id,
        reason="workflow test route",
        success_criteria=["exit code is zero"],
        command=command,
        action=action,
        args={"reason": "workflow test"},
        execution_binding=binding,
        risk=risk,
        approval_scope=approval_scope,
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
    )


def _plan(status="approved", steps=None, risk="low"):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan

    plan = PrivilegedPlan(
        plan_id="priv-test",
        goal="test goal",
        risk=risk,
        status=status,
        steps=steps or [_step()],
    )
    if status == "approved":
        plan.authorize()
        for step in plan.steps:
            if step.approval_scope != "step":
                step.status = "approved"
    return plan


class StubPlanner:
    def __init__(self, plan):
        self.result = plan
        self.calls = []

    def plan(self, goal, environment_context=""):
        self.calls.append((goal, environment_context))
        return self.result


class FailingPlanner:
    def plan(self, goal, environment_context=""):
        raise TimeoutError("planner timed out")


class StubExecutor:
    def __init__(self, return_code=0, timed_out=False):
        self.return_code = return_code
        self.timed_out = timed_out
        self.calls = []

    def execute(self, step):
        from klonet_agent.ops.privileged.contracts import ExecutionEvidence

        self.calls.append(step.step_id)
        return ExecutionEvidence(
            return_code=self.return_code,
            timed_out=self.timed_out,
            stdout="ok",
            started_at="start",
            finished_at="finish",
            environment_changed=True,
        )

    def execute_readonly(self, step, argv):
        assert argv
        return self.execute(step)


class StubVerifier:
    def __init__(self, status="passed"):
        self.status = status
        self.calls = []

    def verify_step(self, plan, step):
        from klonet_agent.ops.privileged.contracts import VerificationDecision

        self.calls.append(step.step_id)
        return VerificationDecision(
            status=self.status,
            goal_achieved=self.status == "passed",
            reason="stub",
        )


class PreboundExecutionAgent:
    @staticmethod
    def prepare_plan(current_plan, *, grounded_context):
        del grounded_context
        return current_plan


def _workflow(tmp_path, plan, executor=None, verifier=None, events=None):
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    return PrivilegedOpsWorkflow(
        planner=StubPlanner(plan),
        executor=executor or StubExecutor(),
        verifier=verifier or StubVerifier(),
        execution_agent=PreboundExecutionAgent(),
        store=PrivilegedPlanStore(tmp_path, user_id="alice", project_id="p1"),
        event_sink=(lambda name, payload: events.append((name, payload)))
        if events is not None
        else None,
    )


def test_auto_authorized_microplan_executes_and_completes(tmp_path):
    plan = _plan()
    executor = StubExecutor()
    workflow = _workflow(tmp_path, plan, executor=executor)

    result = workflow.submit("test goal")

    assert result.plan.status == "completed"
    assert result.plan.steps[0].status == "completed"
    assert executor.calls == ["step-1"]
    persisted = workflow.store.load("priv-test")
    assert persisted.status == "completed"


def test_multi_step_plan_waits_for_one_confirmation_then_executes_all(tmp_path):
    plan = _plan(
        status="awaiting_confirmation",
        steps=[_step("one"), _step("two")],
        risk="medium",
    )
    executor = StubExecutor()
    workflow = _workflow(tmp_path, plan, executor=executor)

    waiting = workflow.submit("test goal")
    completed = workflow.handle_command("confirm-priv priv-test")

    assert waiting.kind == "awaiting_confirmation"
    assert executor.calls == ["one", "two"]
    assert completed.plan.status == "completed"


def test_readonly_step_in_mutating_plan_uses_deterministic_verifier(tmp_path):
    from klonet_agent.ops.privileged.contracts import VerificationDecision

    first = _step("validate", risk="readonly")
    second = _step("start", risk="medium")
    plan = _plan(steps=[first, second], risk="medium")

    class RoutedVerifier:
        def __init__(self):
            self.deterministic_calls = []
            self.model_calls = []

        def verify_deterministic_step(self, current_plan, step):
            del current_plan
            self.deterministic_calls.append(step.step_id)
            return VerificationDecision(status="passed", goal_achieved=True)

        def verify_step(self, current_plan, step):
            del current_plan
            self.model_calls.append(step.step_id)
            return VerificationDecision(status="passed", goal_achieved=True)

    verifier = RoutedVerifier()
    workflow = _workflow(tmp_path, plan, verifier=verifier)

    result = workflow.submit("deploy")

    assert result.kind == "completed"
    assert verifier.deterministic_calls == ["validate"]
    assert verifier.model_calls == ["start"]


def test_high_risk_plan_requires_plan_and_exact_step_confirmation(tmp_path):
    import hashlib

    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ShellArtifact,
    )

    step = _step(
        "delete-old",
        command="sudo rm -r /var/log/myapp-old",
        risk="high",
        approval_scope="step",
    )
    script = "set -euo pipefail\nprintf 'reviewed\\n'\n"
    step.execution_binding = ExecutionBinding(
        kind="shell_artifact",
        risk="high",
        approval_scope="step",
        shell_artifact=ShellArtifact(
            artifact_id="shell-test",
            script=script,
            sha256=hashlib.sha256(script.encode()).hexdigest(),
            cwd=str(tmp_path),
            single_use_nonce="nonce",
            expires_at="2999-01-01T00:00:00+00:00",
        ),
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
    )
    step.status = "awaiting_confirmation"
    plan = _plan(status="awaiting_confirmation", steps=[step], risk="high")
    executor = StubExecutor()
    workflow = _workflow(tmp_path, plan, executor=executor)

    workflow.submit("delete old logs")
    still_waiting = workflow.handle_command("confirm-priv priv-test")
    completed = workflow.handle_command("confirm-priv-step priv-test delete-old")

    assert still_waiting.kind == "awaiting_step_confirmation"
    assert executor.calls == ["delete-old"]
    assert completed.plan.status == "completed"


def test_wrong_or_stale_authorization_never_executes(tmp_path):
    plan = _plan(status="awaiting_confirmation", risk="medium")
    executor = StubExecutor()
    workflow = _workflow(tmp_path, plan, executor=executor)
    workflow.submit("test goal")
    stored = workflow.store.load("priv-test")
    stored.steps[0].execution_binding.args["reason"] = "changed"
    stored.authorized_hash = "stale"
    stored.status = "approved"
    workflow.store.save(stored)

    result = workflow.execute("priv-test")

    assert result.kind == "blocked"
    assert executor.calls == []


def test_failure_pauses_and_waits_for_user_decision(tmp_path):
    plan = _plan(steps=[_step("one"), _step("two")])

    class SecondFailsVerifier(StubVerifier):
        def verify_step(self, plan, step):
            self.status = "passed" if step.step_id == "one" else "failed"
            return super().verify_step(plan, step)

    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        plan,
        executor=executor,
        verifier=SecondFailsVerifier(),
    )

    result = workflow.submit("test goal")

    assert result.plan.status == "paused"
    assert result.plan.steps[0].status == "completed"
    assert result.plan.steps[1].status == "paused"
    assert "continue-priv priv-test" in result.message
    assert "retry-priv priv-test" in result.message


def test_user_can_skip_paused_step_and_continue_confirmed_plan(tmp_path):
    plan = _plan(steps=[_step("one"), _step("two")], risk="medium")

    class FirstFailsVerifier(StubVerifier):
        def verify_step(self, current_plan, step):
            self.status = "failed" if step.step_id == "one" else "passed"
            return super().verify_step(current_plan, step)

    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        plan,
        executor=executor,
        verifier=FirstFailsVerifier(),
    )

    paused = workflow.submit("test goal")
    completed = workflow.handle_command("continue-priv priv-test")

    assert paused.kind == "paused"
    assert completed.kind == "completed"
    assert executor.calls == ["one", "two"]
    assert completed.plan.steps[0].status == "skipped"


def test_user_can_retry_paused_step(tmp_path):
    plan = _plan(steps=[_step("one")], risk="medium")

    class PassesOnRetryVerifier(StubVerifier):
        def verify_step(self, current_plan, step):
            self.status = "failed" if not self.calls else "passed"
            return super().verify_step(current_plan, step)

    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        plan,
        executor=executor,
        verifier=PassesOnRetryVerifier(),
    )

    paused = workflow.submit("test goal")
    completed = workflow.handle_command("retry-priv priv-test")

    assert paused.kind == "paused"
    assert completed.kind == "completed"
    assert executor.calls == ["one", "one"]


def test_resume_interrupted_step_checks_state_but_never_reexecutes(tmp_path):
    plan = _plan()
    plan.status = "executing"
    plan.steps[0].status = "running"
    executor = StubExecutor()
    workflow = _workflow(tmp_path, plan, executor=executor)
    workflow.store.save(plan)

    result = workflow.handle_command("resume-priv priv-test")

    assert result.plan.status == "paused"
    assert result.plan.steps[0].status == "execution_unknown"
    assert executor.calls == []


def test_resume_can_complete_unknown_step_from_current_state_evidence(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    target = tmp_path / "created.txt"
    target.write_text("ready", encoding="utf-8")
    step = _step()
    step.postconditions = [{"checker": "file_exists", "args": {"path": str(target)}}]
    plan = _plan(steps=[step])
    plan.status = "executing"
    plan.steps[0].status = "running"
    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        plan,
        executor=executor,
        verifier=PrivilegedVerifierAgent(None),
    )
    workflow.store.save(plan)

    result = workflow.handle_command("resume-priv priv-test")

    assert result.plan.status == "completed"
    assert result.plan.steps[0].status == "completed"
    assert executor.calls == []


def test_resume_recovers_false_blocked_readonly_step_without_reexecution(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionEvidence,
        VerificationDecision,
    )

    first = _step("validate", risk="readonly")
    second = _step("start", risk="medium")
    plan = _plan(steps=[first, second], risk="medium")
    first.status = "blocked"
    first.evidence = ExecutionEvidence(return_code=0, timed_out=False)
    second.status = "approved"
    plan.status = "blocked"

    class RecoveryVerifier:
        def verify_deterministic_step(self, current_plan, step):
            del current_plan
            assert step.step_id == "validate"
            return VerificationDecision(status="passed", goal_achieved=True)

        def verify_step(self, current_plan, step):
            del current_plan
            assert step.step_id == "start"
            return VerificationDecision(status="passed", goal_achieved=True)

    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        plan,
        executor=executor,
        verifier=RecoveryVerifier(),
    )
    workflow.store.save(plan)

    result = workflow.handle_command("resume-priv priv-test")

    assert result.kind == "completed"
    assert executor.calls == ["start"]
    assert result.plan.steps[0].status == "completed"


def test_failed_precondition_pauses_before_executor_runs(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = _step()
    step.preconditions = [
        {"checker": "file_exists", "args": {"path": str(tmp_path / "missing")}}
    ]
    plan = _plan(steps=[step])
    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        plan,
        executor=executor,
        verifier=PrivilegedVerifierAgent(None),
    )

    result = workflow.submit("test goal")

    assert result.kind == "paused"
    assert result.plan.steps[0].status == "paused"
    assert "precondition" in result.plan.steps[0].observation
    assert executor.calls == []


def test_readonly_plan_is_not_persisted_or_confirmed(tmp_path):
    step = _step(command="systemctl status nginx", risk="readonly")
    plan = _plan(steps=[step], risk="readonly")
    executor = StubExecutor()
    workflow = _workflow(tmp_path, plan, executor=executor)

    result = workflow.submit("show nginx status")

    assert result.kind == "completed"
    assert workflow.store.list() == []
    assert executor.calls == ["step-1"]


def test_planner_readonly_plan_cannot_bypass_argv_validation(tmp_path):
    plan = _plan(
        risk="readonly",
        steps=[
            _step(
                command="ls\ntouch /tmp/ops-priv-bypass",
                risk="readonly",
                action="",
            )
        ],
    )
    executor = StubExecutor()
    workflow = _workflow(tmp_path, plan, executor=executor)

    result = workflow.submit("inspect safely")

    assert result.kind == "blocked"
    assert "read-only validation failed" in result.plan.steps[0].observation
    assert executor.calls == []


def test_submit_readonly_executes_ephemeral_command_with_deterministic_checker(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    plan = _plan()
    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        plan,
        executor=executor,
        verifier=PrivilegedVerifierAgent(None),
    )

    result = workflow.submit_readonly("show Python version", "python3 -V")

    assert result.kind == "completed"
    assert result.plan.status == "completed"
    assert result.plan.risk == "readonly"
    assert result.plan.steps[0].checks[0].status == "passed"
    assert executor.calls == ["readonly-action"]
    assert workflow.store.list() == []


def test_submit_readonly_refuses_command_outside_readonly_policy(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    plan = _plan()
    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        plan,
        executor=executor,
        verifier=PrivilegedVerifierAgent(None),
    )

    result = workflow.submit_readonly(
        "write a file",
        "echo ready > /tmp/should-not-exist",
    )

    assert result.kind == "clarification"
    assert "not deterministically read-only" in result.message
    assert executor.calls == []
    assert workflow.store.list() == []


@pytest.mark.parametrize(
    "command",
    [
        "ls $(touch /tmp/ops-priv-bypass)",
        "cat `touch /tmp/ops-priv-bypass` /etc/hosts",
        "find /tmp/scope -delete",
        "find /tmp/scope -exec touch /tmp/ops-priv-bypass {} +",
        "find /tmp/scope -fprint /tmp/ops-priv-bypass",
        "ls\ntouch /tmp/ops-priv-bypass",
        "find /tmp/scope -de''lete",
        "find /tmp/scope -ex''ec touch /tmp/ops-priv-bypass {} +",
        r"find /tmp/scope -de\lete",
    ],
)
def test_submit_readonly_refuses_shell_and_find_side_effects(tmp_path, command):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    executor = StubExecutor()
    workflow = _workflow(
        tmp_path,
        _plan(),
        executor=executor,
        verifier=PrivilegedVerifierAgent(None),
    )

    result = workflow.submit_readonly("inspect safely", command)

    assert result.kind == "clarification"
    assert executor.calls == []


def test_abort_list_show_commands_are_deterministic(tmp_path):
    plan = _plan(status="awaiting_confirmation")
    workflow = _workflow(tmp_path, plan)
    workflow.submit("test goal")

    listing = workflow.handle_command("list-priv")
    showing = workflow.handle_command("show-priv priv-test")
    audit = workflow.handle_command("audit-priv priv-test")
    aborted = workflow.handle_command("abort-priv priv-test")

    assert "priv-test" in listing.message
    assert "计划目标：test goal" in showing.message
    assert "执行内容：" in showing.message
    assert "schema_version" not in showing.message
    assert "echo ok" not in showing.message
    assert '"schema_version": 3' in audit.message
    assert "echo ok" in audit.message
    assert aborted.plan.status == "aborted"


def test_default_plan_output_is_compact_chinese_summary(tmp_path):
    steps = [
        _step("step-%s" % index, command="echo secret-%s" % index)
        for index in range(1, 9)
    ]
    plan = _plan(status="awaiting_confirmation", steps=steps, risk="medium")
    workflow = _workflow(tmp_path, plan)

    result = workflow.submit("部署平台")

    assert result.kind == "awaiting_confirmation"
    assert "高权限操作计划 priv-test" in result.message
    assert "目标：test goal" in result.message
    assert "风险：中｜状态：等待确认" in result.message
    assert "schema_version" not in result.message
    assert "echo secret" not in result.message
    assert "另有 2 个步骤" in result.message
    assert "确认执行：confirm-priv priv-test" in result.message
    assert "查看完整计划：show-priv priv-test" in result.message


def test_natural_plan_view_hides_legacy_internal_recovery_guard(tmp_path):
    plan = _plan(status="awaiting_confirmation")
    plan.steps[0].status = "paused"
    plan.steps[0].observation = (
        "启动失败。；自动只读诊断已完成，但暂时无法生成可靠修复计划："
        "修复草案没有在重试失败动作前加入诊断或修复步骤"
    )
    workflow = _workflow(tmp_path, plan)
    workflow.store.save(plan)

    result = workflow.handle_command("show-priv priv-test")

    assert "启动失败" in result.message
    assert "可重新运行 replan-priv" in result.message
    assert "修复草案没有在重试" not in result.message


def test_planner_timeout_returns_short_safe_message_instead_of_crashing(tmp_path):
    workflow = _workflow(tmp_path, _plan())
    workflow.planner = FailingPlanner()

    result = workflow.submit("部署平台")

    assert result.kind == "blocked"
    assert "planner timed out" in result.message
    assert "当前没有执行任何操作" in result.message


@pytest.mark.parametrize(
    "command",
    [
        "list-priv",
        "show-priv priv-123",
        "audit-priv priv-123",
        "confirm-priv priv-123",
        "confirm-priv-step priv-123 step-1",
        "resume-priv priv-123",
        "continue-priv priv-123",
        "retry-priv priv-123",
        "replan-priv priv-123",
        "abort-priv priv-123",
    ],
)
def test_plan_control_recognizes_only_exact_command_grammar(command):
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    assert PrivilegedOpsWorkflow.is_control_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "list-priv extra",
        "show-priv",
        "show-priv priv-123 extra",
        "audit-priv",
        "audit-priv priv-123 extra",
        "confirm-priv",
        "confirm-priv priv-123 extra",
        "confirm-priv-step priv-123",
        "resume-priv priv-123 extra",
        "please confirm-priv priv-123",
    ],
)
def test_plan_control_rejects_malformed_or_natural_language_commands(command):
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    assert PrivilegedOpsWorkflow.is_control_command(command) is False


def test_workflow_emits_lifecycle_audit_events(tmp_path):
    events = []
    plan = _plan()
    workflow = _workflow(tmp_path, plan, events=events)

    workflow.submit("test goal")

    names = [name for name, _ in events]
    assert names == [
        "privileged_plan_created",
        "privileged_plan_approved",
        "privileged_step_started",
        "privileged_step_finished",
        "privileged_verification",
        "privileged_plan_completed",
    ]


def test_replan_required_replaces_steps_and_invalidates_old_authorization(tmp_path):
    original = _plan()
    replacement = _plan(
        steps=[_step("replacement", command="echo repaired")],
    )
    planner = StubPlanner(original)
    verifier = StubVerifier(status="replan_required")
    executor = StubExecutor()
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        executor=executor,
        verifier=verifier,
        execution_agent=PreboundExecutionAgent(),
        store=PrivilegedPlanStore(tmp_path, user_id="alice", project_id="p1"),
    )
    planner.result = original

    workflow.submit("test goal")
    planner.result = replacement
    result = workflow.replan("priv-test", reason="service did not become active")

    assert result.kind == "awaiting_confirmation"
    assert result.plan.steps[0].step_id == "replacement"
    assert result.plan.authorized_hash == ""
    assert result.plan.is_authorized is False
    assert "confirm-priv priv-test" in result.message


def test_failure_automatically_diagnoses_and_drafts_unapproved_recovery(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    failed = _step(
        "start",
        action="start_platform_screens",
        risk="medium",
    )
    failed.args = {
        "platform": "vemu_uestc",
        "project_root": "/home/klonet-agent/vemu_uestc",
    }
    original = _plan(steps=[failed], risk="medium")
    repair = _plan(
        status="awaiting_confirmation",
        steps=[
            _step("repair", action="prepare_project_files", risk="medium"),
            _step("start-again", action="start_platform_screens", risk="medium"),
        ],
        risk="medium",
    )
    events = []

    class Planner:
        def __init__(self):
            self.calls = []

        def plan(self, goal, **kwargs):
            self.calls.append((goal, kwargs))
            return repair

    class Executor:
        def execute(self, step):
            del step
            events.append("execute")
            return ExecutionEvidence(
                return_code=1,
                stderr=(
                    "component=master ModuleNotFoundError: "
                    "No module named 'vemu_uestc'"
                ),
                environment_changed=False,
            )

    class ContextBuilder:
        def build(self, goal, **kwargs):
            events.append("readonly_diagnosis")
            assert "Failure Packet" in goal
            assert "supplemental_environment_context" in kwargs
            return GroundedPlanContext(
                knowledge_evidence="Klonet import recovery knowledge",
                environment_evidence="read-only host diagnosis",
                action_catalog="registered actions",
            )

        @staticmethod
        def current_environment_fingerprint():
            return "env-1"

    class Summarizer:
        def summarize(self, step, **kwargs):
            del step, kwargs
            return "master 无法导入 vemu_uestc，启动预检失败。"

    planner = Planner()
    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        executor=Executor(),
        verifier=StubVerifier(status="failed"),
        execution_agent=PreboundExecutionAgent(),
        store=PrivilegedPlanStore(
            tmp_path,
            user_id="alice",
            project_id="p1",
        ),
        context_builder=ContextBuilder(),
        summarizer=Summarizer(),
    )
    workflow.store.save(original)

    result = workflow.execute("priv-test")

    assert result.kind == "awaiting_confirmation"
    assert events == ["execute", "readonly_diagnosis"]
    assert result.plan.authorized_hash == ""
    assert result.plan.is_authorized is False
    assert result.plan.status == "awaiting_confirmation"
    assert result.plan.steps[0].step_id == "repair"
    assert result.plan.failure_packets[0].execution_evidence["return_code"] == 1
    assert "Planner 已根据真实证据" in result.message
    assert "确认新计划：confirm-priv priv-test" in result.message


def test_each_step_is_explained_before_execution(tmp_path):
    progress = []
    plan = _plan(steps=[_step("validate", risk="readonly")], risk="medium")

    class Summarizer:
        def describe_execution(self, step, **kwargs):
            assert step.step_id == "validate"
            assert kwargs == {"index": 1, "total": 1}
            return (
                "第 1/1 步：将检查项目入口文件是否完整；"
                "这是只读操作，不会修改服务器。"
            )

        def summarize(self, step, **kwargs):
            del step, kwargs
            return "项目入口文件检查完成。"

    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    workflow = PrivilegedOpsWorkflow(
        planner=StubPlanner(plan),
        executor=StubExecutor(),
        verifier=StubVerifier(),
        execution_agent=PreboundExecutionAgent(),
        store=PrivilegedPlanStore(
            tmp_path,
            user_id="alice",
            project_id="p1",
        ),
        summarizer=Summarizer(),
        on_progress=progress.append,
    )

    result = workflow.submit("校验项目")

    assert result.kind == "completed"
    assert progress == [
        "第 1/1 步：将检查项目入口文件是否完整；这是只读操作，不会修改服务器。"
    ]


@pytest.mark.skip(reason="replaced by same-Planner Failure Packet recovery in V3")
def test_generic_recovery_pipeline_probes_then_reviews_repair_plan(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence
    from klonet_agent.ops.privileged.recovery import (
        RecoveryAnalysis,
        RecoveryConclusion,
        RecoveryPlanReview,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    failed = _step("change", action="run_ops_command", risk="medium")
    failed.args = {"program": "make", "argv": ["deploy"], "cwd": "/srv/app"}
    original = _plan(steps=[failed], risk="medium")
    repair = _plan(
        status="awaiting_confirmation",
        steps=[_step("repair", action="write_ops_file", risk="medium")],
        risk="medium",
    )
    repair.steps[0].args = {
        "path": "/srv/app/app.conf",
        "content": "port=8081",
    }
    events = []

    class RecoveryAgent:
        def analyze(self, plan, step, **kwargs):
            del plan, step
            events.append("analyze")
            assert "platform_health" in kwargs["probe_catalog"]
            return RecoveryAnalysis(
                summary="部署失败可能与端口冲突有关。",
                hypotheses=["目标端口已被占用"],
                probes=[
                    {
                        "probe": "process_detail",
                        "args": {"ports": [8080]},
                        "purpose": "确认端口占用者",
                    }
                ],
                required_capability="调整应用监听端口",
            )

        def conclude(self, plan, step, analysis, evidence):
            del plan, step, analysis
            events.append("conclude")
            assert "port_owner=other-service" in evidence
            return RecoveryConclusion(
                summary="确认 8080 端口已被其他服务占用。",
                confirmed_cause="端口冲突",
                required_capability="调整应用监听端口",
                planning_guidance="修改应用配置后重新部署",
            )

        def review_plan(self, failed_step, conclusion, replacement):
            del failed_step, conclusion, replacement
            events.append("review")
            return RecoveryPlanReview(
                covers_cause=True,
                explanation="修复计划先调整端口配置。",
            )

    class ContextBuilder:
        def recovery_probe_catalog(self):
            return "platform_health\nprocess_detail"

        def run_recovery_diagnostics(self, requests):
            events.append("probe")
            assert requests[0]["probe"] == "process_detail"
            return "port_owner=other-service"

        def build(self, goal, **kwargs):
            events.append("ground")
            assert "端口冲突" in goal
            assert "port_owner=other-service" in kwargs[
                "supplemental_environment_context"
            ]
            return GroundedPlanContext(
                knowledge_evidence="Klonet deployment evidence",
                environment_evidence="port_owner=other-service",
                action_catalog="registered actions",
            )

    class Planner:
        def plan(self, goal, **kwargs):
            del goal, kwargs
            events.append("plan")
            return repair

    class Executor:
        def execute(self, step):
            del step
            events.append("execute")
            return ExecutionEvidence(
                return_code=1,
                stderr="bind failed on port 8080",
                environment_changed=False,
            )

    workflow = PrivilegedOpsWorkflow(
        planner=Planner(),
        executor=Executor(),
        verifier=StubVerifier(status="failed"),
        store=PrivilegedPlanStore(
            tmp_path,
            user_id="alice",
            project_id="p1",
        ),
        context_builder=ContextBuilder(),
        recovery_agent=RecoveryAgent(),
    )
    workflow.store.save(original)

    result = workflow.execute("priv-test")

    assert result.kind == "awaiting_confirmation"
    assert events == [
        "execute",
        "analyze",
        "probe",
        "conclude",
        "ground",
        "plan",
        "review",
    ]
    assert result.plan.authorized_hash == ""
    assert result.plan.recovery_history[0]["recovery_conclusion"][
        "confirmed_cause"
    ] == "端口冲突"


@pytest.mark.skip(reason="legacy RecoveryAgent was removed in Agentic V3")
def test_recovery_capability_gap_is_explained_without_internal_guard_text(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence
    from klonet_agent.ops.privileged.recovery import (
        RecoveryAnalysis,
        RecoveryConclusion,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    failed = _step("start", action="start_platform_screens", risk="medium")
    failed.args = {"platform": "p", "project_root": "/srv/p"}
    original = _plan(steps=[failed], risk="medium")
    repeated = _plan(
        status="awaiting_confirmation",
        steps=[_step("start-again", action="start_platform_screens", risk="medium")],
        risk="medium",
    )
    repeated.steps[0].args = dict(failed.args)

    class RecoveryAgent:
        def analyze(self, *args, **kwargs):
            del args, kwargs
            return RecoveryAnalysis(
                summary="启动环境配置不完整。",
                required_capability="调整受控启动环境",
            )

        def conclude(self, *args, **kwargs):
            del args, kwargs
            return RecoveryConclusion(
                summary="启动环境缺少项目要求的运行路径。",
                confirmed_cause="启动环境配置缺失",
                required_capability="调整受控启动环境",
            )

    class ContextBuilder:
        def recovery_probe_catalog(self):
            return ""

        def run_recovery_diagnostics(self, requests):
            del requests
            return "diagnosis complete"

        def build(self, *args, **kwargs):
            del args, kwargs
            return GroundedPlanContext(
                knowledge_evidence="Klonet evidence",
                environment_evidence="diagnosis complete",
                action_catalog="registered actions",
            )

    class Planner:
        def plan(self, *args, **kwargs):
            del args, kwargs
            return repeated

    class Executor:
        def execute(self, step):
            del step
            return ExecutionEvidence(return_code=1, stderr="start failed")

    workflow = PrivilegedOpsWorkflow(
        planner=Planner(),
        executor=Executor(),
        verifier=StubVerifier(status="failed"),
        store=PrivilegedPlanStore(
            tmp_path,
            user_id="alice",
            project_id="p1",
        ),
        context_builder=ContextBuilder(),
        recovery_agent=RecoveryAgent(),
    )
    workflow.store.save(original)

    result = workflow.execute("priv-test")

    assert result.kind == "paused"
    assert "调整受控启动环境" in result.message
    assert "只是重复失败操作" in result.message
    assert "修复草案没有在重试" not in result.message


@pytest.mark.skip(reason="covered by V3 failure-fingerprint loop guard")
def test_replan_with_unchanged_evidence_does_not_repeat_old_plan(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence
    from klonet_agent.ops.privileged.recovery import (
        RecoveryAnalysis,
        RecoveryConclusion,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    failed = _step("start", action="start_platform_screens", risk="medium")
    failed.args = {"platform": "p", "project_root": "/srv/p"}
    original = _plan(steps=[failed], risk="medium")
    repeated = _plan(
        status="awaiting_confirmation",
        steps=[_step("start-again", action="start_platform_screens", risk="medium")],
        risk="medium",
    )
    repeated.steps[0].args = dict(failed.args)
    planner_calls = []

    class RecoveryAgent:
        def analyze(self, *args, **kwargs):
            del args, kwargs
            return RecoveryAnalysis(
                probes=[],
                required_capability="修正运行配置",
            )

        def conclude(self, *args, **kwargs):
            del args, kwargs
            return RecoveryConclusion(
                summary="运行配置仍未修正。",
                confirmed_cause="配置错误",
                required_capability="修正运行配置",
            )

    class ContextBuilder:
        def recovery_probe_catalog(self):
            return "project_layout"

        def run_recovery_diagnostics(self, requests):
            assert requests
            return "same deterministic evidence"

        def build(self, *args, **kwargs):
            del args, kwargs
            return GroundedPlanContext(
                knowledge_evidence="Klonet evidence",
                environment_evidence="same deterministic evidence",
                action_catalog="registered actions",
            )

    class Planner:
        def plan(self, *args, **kwargs):
            del args, kwargs
            planner_calls.append("plan")
            return repeated

    class Executor:
        def execute(self, step):
            del step
            return ExecutionEvidence(
                return_code=1,
                stderr="Connection refused",
                environment_changed=False,
            )

    workflow = PrivilegedOpsWorkflow(
        planner=Planner(),
        executor=Executor(),
        verifier=StubVerifier(status="failed"),
        store=PrivilegedPlanStore(
            tmp_path,
            user_id="alice",
            project_id="p1",
        ),
        context_builder=ContextBuilder(),
        recovery_agent=RecoveryAgent(),
    )
    workflow.store.save(original)

    first = workflow.execute("priv-test")
    second = workflow.replan("priv-test", reason="user requested replan")

    assert first.kind == "paused"
    assert second.kind == "paused"
    assert planner_calls == ["plan"]
    assert "没有发现新证据" in second.message
    assert "这不是一份新的待执行计划" in second.message
    assert "高权限操作计划" not in second.message
    assert len(second.plan.recovery_history) == 2
    assert all(
        item["outcome"] == "unavailable"
        for item in second.plan.recovery_history
    )
