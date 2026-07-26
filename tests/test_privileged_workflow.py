from __future__ import annotations


def _step(step_id="step-1", command="echo ok", risk="low", approval_scope="plan"):
    from klonet_agent.ops.privileged.contracts import PrivilegedStep

    return PrivilegedStep(
        step_id=step_id,
        title=step_id,
        command=command,
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


def _workflow(tmp_path, plan, executor=None, verifier=None, events=None):
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    return PrivilegedOpsWorkflow(
        planner=StubPlanner(plan),
        executor=executor or StubExecutor(),
        verifier=verifier or StubVerifier(),
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


def test_high_risk_plan_requires_plan_and_exact_step_confirmation(tmp_path):
    step = _step(
        "delete-old",
        command="sudo rm -r /var/log/myapp-old",
        risk="high",
        approval_scope="step",
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
    stored.steps[0].command = "echo changed"
    stored.authorized_hash = "stale"
    stored.status = "approved"
    workflow.store.save(stored)

    result = workflow.execute("priv-test")

    assert result.kind == "blocked"
    assert executor.calls == []


def test_failure_stops_remaining_steps_and_marks_partial_completion(tmp_path):
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

    assert result.plan.status == "partially_completed"
    assert result.plan.steps[0].status == "completed"
    assert result.plan.steps[1].status == "failed"


def test_resume_interrupted_step_checks_state_but_never_reexecutes(tmp_path):
    plan = _plan()
    plan.status = "executing"
    plan.steps[0].status = "running"
    executor = StubExecutor()
    workflow = _workflow(tmp_path, plan, executor=executor)
    workflow.store.save(plan)

    result = workflow.handle_command("resume-priv priv-test")

    assert result.plan.status == "blocked"
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


def test_failed_precondition_blocks_before_executor_runs(tmp_path):
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

    assert result.kind == "blocked"
    assert result.plan.steps[0].status == "blocked"
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


def test_abort_list_show_commands_are_deterministic(tmp_path):
    plan = _plan(status="awaiting_confirmation")
    workflow = _workflow(tmp_path, plan)
    workflow.submit("test goal")

    listing = workflow.handle_command("list-priv")
    showing = workflow.handle_command("show-priv priv-test")
    aborted = workflow.handle_command("abort-priv priv-test")

    assert "priv-test" in listing.message
    assert "echo ok" in showing.message
    assert aborted.plan.status == "aborted"


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
