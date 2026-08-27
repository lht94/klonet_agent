from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_latest_plan_id_for_goal_uses_plan_store_as_authority():
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    workflow = MutationWorkflow.__new__(MutationWorkflow)
    workflow.store = SimpleNamespace(list=lambda: [
        SimpleNamespace(plan_id="priv-ops-newest-other", goal="其他目标"),
        SimpleNamespace(plan_id="priv-ops-matching", goal="重启 test 平台"),
        SimpleNamespace(plan_id="priv-ops-older-match", goal="重启 test 平台"),
    ])

    assert workflow.latest_plan_id_for_goal("重启 test 平台") == (
        "priv-ops-matching"
    )
    assert workflow.latest_plan_id_for_goal("不存在的目标") == ""


def test_binding_context_preserves_exact_ops_source_before_large_runtime_evidence():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    bundle = EvidenceBundle(goal="repair 102")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest(
                probe="running_platforms",
                args={"project_roots": ["/x"]},
                purpose="runtime baseline",
            ),
            "runtime evidence\n" + "x" * 20000,
        )
    )
    exact_source = (
        "def KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE():\n"
        '    raise RuntimeError("KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE")\n'
    )
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest(
                probe="ops_file",
                args={"path": "/home/klonet-agent/102/mains/master_main.py"},
                purpose="read exact source",
            ),
            exact_source,
        )
    )

    context = MutationWorkflow._binding_context(bundle)

    assert context is not None
    assert exact_source in context.environment_evidence
    assert context.environment_evidence.index("probe=ops_file") < context.environment_evidence.index(
        "probe=running_platforms"
    )


def test_workflow_confirmation_redacts_registered_action_credentials():
    from klonet_agent.ops.privileged.contracts import ExecutionBinding
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    step = ChangeStep(
        step_id="redis",
        title="create Redis",
        objective="create isolated Redis",
        risk="high",
        expected_changes=["Redis container is created"],
        postconditions=[{"checker": "exit_code_zero"}],
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="create_docker_container",
            args={
                "name": "v4e2e-redis",
                "image": "redis:7",
                "port_bindings": ["127.0.0.1:47005:6379"],
                "command": ["redis-server", "--requirepass", "local-secret"],
                "environment": ["REDIS_PASSWORD=local-secret"],
            },
            risk="high",
            postconditions=[{"checker": "exit_code_zero"}],
        ),
    )
    plan = ChangePlan.new(goal="deploy", risk="high", steps=[step])

    message = MutationWorkflow._confirmation_message(plan)

    assert "local-secret" not in message
    assert "敏感参数已隐藏" in message
    assert "变更计划" in message
    assert "目标：" in message
    assert "风险：" in message
    assert "冻结资源：" not in message
    assert "变更步骤：" in message
    assert "请使用以下命令确认这份精确计划" in message
    assert "参数=" not in message
    assert "执行方式：受控动作 create_docker_container" in message
    assert "show-priv-plan-details" in message


def test_reload_nginx_verification_accepts_non_systemd_master_process():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    step = PrivilegedStep(
        step_id="reload-nginx",
        title="reload nginx",
        postconditions=[
            {"checker": "service_active", "args": {"service": "nginx"}}
        ],
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="reload_nginx",
            args={},
            risk="medium",
        ),
    )

    verification = MutationWorkflow._verification_step(step)

    assert verification.postconditions == [
        {"checker": "nginx_config_valid", "args": {}},
        {
            "checker": "process_running",
            "args": {"pattern": "^nginx: master process"},
        },
    ]


def _change_plan(*, hierarchical=False):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    change = ChangeStep(
        step_id="deploy",
        title="deploy",
        objective="deploy isolated instance",
        risk="high",
        expected_changes=["instance is created"],
        postconditions=[{"checker": "exit_code_zero"}],
    )
    binding = ExecutionBinding(
        kind="registered_action",
        risk="high",
        action="manage_service",
        args={"service": "v4e2e", "operation": "start"},
        postconditions=[{"checker": "exit_code_zero"}],
    )
    if hierarchical:
        change.implementation_plan = ImplementationPlan(
            implementation_id="impl-deploy",
            semantic_step_id="deploy",
            objective="deploy",
            steps=[
                PrivilegedStep(
                    step_id="deploy-1",
                    title="start",
                    risk="high",
                    expected_changes=["instance is created"],
                    execution_binding=binding,
                    postconditions=[{"checker": "exit_code_zero"}],
                )
            ],
        )
    else:
        change.execution_binding = binding
    return ChangePlan(
        plan_id="priv-ops-flow",
        goal="deploy",
        risk="high",
        steps=[change],
    )


class MemoryStore:
    def __init__(self):
        self.plans = {}

    def save(self, plan):
        self.plans[plan.plan_id] = plan

    def load(self, plan_id):
        return self.plans[plan_id]


class FakePlanner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def plan(self, *args, **kwargs):
        self.calls += 1
        return self.outcomes.pop(0)


class FakeBinder:
    def __init__(self):
        self.calls = 0
        self.kwargs = []

    def bind(self, plan, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        plan.status = "awaiting_confirmation"
        return plan


class FakeExecutor:
    def __init__(self):
        self.steps = []

    def execute(self, step):
        from klonet_agent.ops.privileged.contracts import ExecutionEvidence

        self.steps.append(step.step_id)
        return ExecutionEvidence(return_code=0, environment_changed=True)


class FakeVerifier:
    def __init__(self, status="passed"):
        self.status = status
        self.steps = []

    def verify_step(self, plan, step):
        self.steps.append(step.step_id)
        return SimpleNamespace(status=self.status, reason=self.status)

    def verify_recovered_step(self, plan, step):
        self.steps.append("recovered:" + step.step_id)
        return SimpleNamespace(status=self.status, reason=self.status)

    def verify_plan_execution(self, plan):
        return SimpleNamespace(
            status="passed", reason="verified", failed_criteria=[],
        )


class RaisingVerifier:
    def verify_step(self, plan, step):
        raise RuntimeError("checker crashed")


def _workflow(tmp_path, plan, *, verifier_status="passed"):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    planner = FakePlanner([GoalOutcome(status="need_execution", plan=plan)])
    binder = FakeBinder()
    executor = FakeExecutor()
    verifier = FakeVerifier(verifier_status)
    store = MemoryStore()
    workflow = MutationWorkflow(
        planner=planner,
        binder=binder,
        store=store,
        executor=executor,
        verifier=verifier,
    )
    return workflow, planner, binder, store, executor, verifier


def _submit(workflow, goal, *, evidence_bundle, evidence_conclusion, **kwargs):
    """Exercise the Coordinator-owned Plan/Replan loop with real mutation stages."""

    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator,
    )

    coordinator = object.__new__(PrivilegedOpsCoordinator)
    coordinator.mutation_workflow = workflow
    coordinator.discovery = workflow.discovery
    coordinator.synthesis = workflow.synthesis
    coordinator.on_progress = kwargs.get("on_progress")
    return coordinator._submit_mutation(
        goal,
        evidence_bundle=evidence_bundle,
        evidence_conclusion=evidence_conclusion,
        intent_context=kwargs.get("intent_context"),
        initial_candidate_plan=kwargs.get("initial_candidate_plan"),
    )


def test_submit_binds_and_persists_but_never_executes_before_confirmation(tmp_path):
    workflow, planner, binder, store, executor, _ = _workflow(tmp_path, _change_plan())

    result = _submit(workflow, "deploy", evidence_bundle=object(), evidence_conclusion=object())

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == binder.calls == 1
    assert executor.steps == []
    assert store.load(result.plan.plan_id).status == "awaiting_confirmation"
    assert "confirm-priv-plan %s %s" % (
        result.plan.plan_id,
        result.plan.content_hash,
    ) in result.message
    assert "deploy isolated instance" in result.message
    assert "执行方式：受控动作 manage_service" in result.message
    assert "参数=" not in result.message
    assert "exit_code_zero" in result.message


def test_submit_passes_collected_discovery_evidence_to_binding(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )

    workflow, _, binder, _, _, _ = _workflow(tmp_path, _change_plan())
    bundle = EvidenceBundle(goal="deploy")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("docker_images", {}, "select image"),
            "inspect_docker_images\nredis latest sha256:a sha256:b now 1MB",
        )
    )

    _submit(workflow, "deploy", evidence_bundle=bundle, evidence_conclusion=object())

    context = binder.kwargs[0]["grounded_context"]
    assert "inspect_docker_images" in context.environment_evidence


def test_confirmation_rejects_stale_hash_without_execution(tmp_path):
    workflow, _, _, _, executor, _ = _workflow(tmp_path, _change_plan())
    submitted = _submit(workflow, "deploy", evidence_bundle=object(), evidence_conclusion=object())

    result = workflow.confirm(submitted.plan.plan_id, "stale")

    assert result.kind == "confirmation_rejected"
    assert executor.steps == []
    assert submitted.plan.is_authorized is False


def test_confirmation_blocks_persisted_screen_plan_that_violates_current_contract(
    tmp_path,
):
    from copy import deepcopy
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PlanResource,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    micro = PrivilegedStep(
        step_id="restart__master",
        title="重启 master Screen 组件",
        risk="medium",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            risk="medium",
            action="restart_screen_component",
            args={
                "component": "master",
                "screen_session": "test_m",
                "run_as_uid": 1000,
                "python_executable": "/envs/test/bin/python3.8",
            },
            postconditions=[{"checker": "port_listening", "args": {"port": 45554}}],
        ),
    )
    change = ChangeStep(
        step_id="restart",
        title="重启 test 的应用组件",
        objective="重启 master",
        risk="medium",
        expected_changes=[
            "restart master role",
            "preserve healthy master role without restart",
        ],
        postconditions=[{"checker": "port_listening", "args": {"port": 45554}}],
        implementation_plan=ImplementationPlan(
            implementation_id="impl-restart",
            semantic_step_id="restart",
            objective="重启 master",
            steps=[micro],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-old-screen",
        goal="用 Screen 重启 test",
        risk="medium",
        steps=[change],
        resources=[
            PlanResource(
                "python", "path", "frozen", "python_executable",
                "/envs/test/bin/python3.8", "old_binding",
                consumers=["restart.python_executable"],
            ),
        ],
        status="awaiting_confirmation",
    )
    workflow, _, _, store, executor, _ = _workflow(tmp_path, plan)
    store.save(plan)

    queried_plan = deepcopy(plan)
    queried_plan.plan_id = "priv-ops-old-screen-query"
    query_workflow, _, _, query_store, query_executor, _ = _workflow(
        tmp_path, queried_plan,
    )

    class PlanResponse:
        def render_plan_turn(self, question, **kwargs):
            assert question == "我之前不是修订了这个计划吗"
            assert "讨论过三个修订要求" in kwargs["conversation_context"]
            assert "selected_plan_contract_error=" in kwargs["plan_context"]
            return "讨论过修订，但没有生成新的正式 Plan ID；旧计划已阻塞。"

    query_workflow.response = PlanResponse()
    query_store.save(queried_plan)

    queried = query_workflow.manage_plan_turn(
        queried_plan.plan_id,
        question="我之前不是修订了这个计划吗",
        conversation_context="讨论过三个修订要求",
    )

    assert queried.kind == "awaiting_user_decision"
    assert "没有生成新的正式 Plan ID" in queried.message
    assert query_store.load(queried_plan.plan_id).status == "blocked"
    assert query_executor.steps == []

    result = workflow.confirm(plan.plan_id, plan.content_hash)

    assert result.kind == "awaiting_user_decision"
    assert result.failure.category == "persisted_plan_contract_invalid"
    assert store.load(plan.plan_id).status == "blocked"
    assert executor.steps == []
    assert plan.is_authorized is False


def test_confirmation_accepts_screen_plan_with_component_identity_and_acceptance(
    tmp_path,
):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    change = ChangeStep(
        step_id="restart",
        title="重启 master Screen 组件",
        objective="在 test_m 中重启 master",
        risk="medium",
        expected_changes=["restart master role in Screen"],
        postconditions=[
            {"checker": "screen_session_exists", "args": {"session": "test_m"}},
        ],
        execution_binding=ExecutionBinding(
            kind="registered_action",
            risk="medium",
            action="restart_screen_component",
            args={
                "component": "master",
                "screen_session": "test_m",
                "run_as_uid": 1000,
                "python_executable": "/envs/test/bin/python3.8",
            },
            postconditions=[
                {"checker": "screen_session_exists", "args": {"session": "test_m"}},
            ],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-current-screen",
        goal="用 Screen 重启 test master",
        risk="medium",
        steps=[change],
        resources=[
            PlanResource(
                "master_uid", "identifier", "frozen", "master_uid", 1000,
                "runtime_evidence", consumers=["restart.run_as_uid"],
            ),
            PlanResource(
                "master_python", "path", "frozen", "master_python_executable",
                "/envs/test/bin/python3.8", "runtime_evidence",
                consumers=["restart.python_executable"],
            ),
        ],
        status="awaiting_confirmation",
    )
    workflow, _, _, store, executor, _ = _workflow(tmp_path, plan)
    store.save(plan)

    result = workflow.confirm(plan.plan_id, plan.content_hash)

    assert result.kind == "goal_verification"
    assert executor.steps == ["restart"]
    assert result.plan.is_authorized is True


@pytest.mark.parametrize("hierarchical", [False, True])
def test_exact_confirmation_executes_then_waits_for_goal_verdict(tmp_path, hierarchical):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

    workflow, _, _, store, executor, verifier = _workflow(
        tmp_path, _change_plan(hierarchical=hierarchical)
    )
    submitted = _submit(workflow, "deploy", evidence_bundle=object(), evidence_conclusion=object())

    result = workflow.confirm(submitted.plan.plan_id, submitted.plan.content_hash)

    assert result.kind == "goal_verification"
    assert executor.steps == (["deploy-1"] if hierarchical else ["deploy"])
    assert verifier.steps == (
        ["deploy-1", "deploy"] if hierarchical else ["deploy"]
    )
    assert store.load(submitted.plan.plan_id).status == "verifying"
    completed = workflow.complete_goal(
        result.plan, GoalOutcome("achieved", reason="whole goal verified"),
    )
    assert completed.kind == "completed"
    assert store.load(submitted.plan.plan_id).status == "completed"
    if not hierarchical:
        persisted = store.load(submitted.plan.plan_id).steps[0]
        assert persisted.execution_attempts == 1
        assert persisted.evidence.return_code == 0


def test_complete_goal_rejects_achieved_verdict_when_plan_tree_is_incomplete(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

    plan = _change_plan(hierarchical=True)
    workflow, _, _, store, _, _ = _workflow(tmp_path, plan)
    plan.status = "paused"
    plan.steps[0].status = "paused"
    plan.steps[0].implementation_plan.status = "paused"
    plan.steps[0].implementation_plan.steps[0].status = "completed"
    store.save(plan)

    result = workflow.complete_goal(
        plan, GoalOutcome("achieved", reason="incorrect external verdict"),
    )

    assert result.kind == "paused"
    assert "未完成节点" in result.message
    assert store.load(plan.plan_id).status == "paused"


def test_complete_goal_revalidates_fully_verified_paused_recovery(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

    workflow, _, _, store, _, _ = _workflow(
        tmp_path, _change_plan(hierarchical=True),
    )
    submitted = _submit(
        workflow, "deploy", evidence_bundle=object(),
        evidence_conclusion=object(),
    )
    executed = workflow.confirm(
        submitted.plan.plan_id, submitted.plan.content_hash,
    )
    assert executed.kind == "goal_verification"
    executed.plan.status = "paused"
    store.save(executed.plan)

    result = workflow.complete_goal(
        executed.plan,
        GoalOutcome("achieved", reason="whole goal verified after recovery"),
    )

    assert result.kind == "completed"
    assert store.load(executed.plan.plan_id).status == "completed"


def test_plan_store_repairs_historical_outer_completed_inner_pending_state(tmp_path):
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    plan = _change_plan(hierarchical=True)
    plan.status = "completed"
    plan.steps[0].status = "paused"
    plan.steps[0].implementation_plan.status = "paused"
    plan.steps[0].implementation_plan.steps[0].status = "completed"
    store.save(plan)

    repaired = store.load(plan.plan_id)

    assert repaired.status == "paused"
    assert repaired.steps[0].implementation_plan.steps[0].status == "completed"
    assert repaired.completion_gaps == [
        "change:deploy=paused",
        "implementation:impl-deploy=paused",
    ]


def test_failed_verification_pauses_without_retrying_execution(tmp_path):
    workflow, _, _, store, executor, _ = _workflow(
        tmp_path, _change_plan(), verifier_status="failed"
    )
    submitted = _submit(workflow, "deploy", evidence_bundle=object(), evidence_conclusion=object())

    result = workflow.confirm(submitted.plan.plan_id, submitted.plan.content_hash)

    assert result.kind == "paused"
    assert result.failure is None
    assert executor.steps == ["deploy"]
    assert store.load(submitted.plan.plan_id).steps[0].status == "paused"


def test_execution_interrupt_persists_unknown_state_before_cli_handles_it(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    class InterruptingExecutor:
        def execute(self, step):
            del step
            raise KeyboardInterrupt()

    plan = _change_plan(hierarchical=True)
    store = MemoryStore()
    workflow = MutationWorkflow(
        planner=FakePlanner([GoalOutcome(status="need_execution", plan=plan)]),
        binder=FakeBinder(), store=store, executor=InterruptingExecutor(),
        verifier=FakeVerifier(),
    )
    submitted = _submit(
        workflow, "deploy", evidence_bundle=object(),
        evidence_conclusion=object(),
    )

    with pytest.raises(KeyboardInterrupt):
        workflow.confirm(submitted.plan.plan_id, submitted.plan.content_hash)

    persisted = store.load(submitted.plan.plan_id)
    atomic = persisted.steps[0].implementation_plan.steps[0]
    assert persisted.status == "paused"
    assert persisted.steps[0].status == "paused"
    assert persisted.steps[0].implementation_plan.status == "paused"
    assert atomic.status == "execution_unknown"
    assert atomic.execution_attempts == 1
    assert atomic.evidence is None


def test_recovery_checks_unknown_step_once_and_turns_unmet_state_into_replan_fact():
    from klonet_agent.ops.privileged.contracts import (
        CheckResult, VerificationDecision,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    class RecoveredVerifier:
        def __init__(self):
            self.calls = []

        def verify_recovered_step(self, plan, step):
            del plan
            self.calls.append(step.step_id)
            step.checks = [
                CheckResult(
                    "process_pid_absent", "failed", observed="pid still exists",
                )
            ]
            return VerificationDecision(
                status="failed",
                reason="current state does not satisfy recovered postconditions",
            )

        def verify_deterministic_step(self, plan, step):
            raise AssertionError("unknown execution must use recovered verification")

    plan = _change_plan(hierarchical=True)
    plan.status = "paused"
    change = plan.steps[0]
    change.status = "paused"
    change.implementation_plan.status = "paused"
    atomic = change.implementation_plan.steps[0]
    atomic.status = "execution_unknown"
    atomic.postconditions = [
        {"checker": "process_pid_absent", "args": {"pid": 123}}
    ]
    verifier = RecoveredVerifier()
    workflow = MutationWorkflow.__new__(MutationWorkflow)
    workflow.verifier = verifier
    workflow.store = MemoryStore()
    workflow.store.save(plan)

    reconciled = workflow.reconcile_recovery_state(plan)

    assert verifier.calls == ["deploy-1"]
    assert reconciled.steps[0].implementation_plan.steps[0].status == "paused"
    assert reconciled.steps[0].implementation_plan.status == "paused"
    assert reconciled.steps[0].status == "paused"
    assert reconciled.steps[0].implementation_plan.steps[0].checks[0].status == "failed"


def test_exact_reconfirmation_returns_failure_decision_without_reexecuting(tmp_path):
    workflow, _, _, store, executor, verifier = _workflow(
        tmp_path, _change_plan(hierarchical=True), verifier_status="failed"
    )
    submitted = _submit(workflow,
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )
    plan_id = submitted.plan.plan_id
    content_hash = submitted.plan.content_hash
    paused = workflow.confirm(plan_id, content_hash)
    assert paused.kind == "paused"
    assert executor.steps == ["deploy-1"]

    resumed = workflow.confirm(plan_id, content_hash)

    assert resumed.kind == "paused"
    assert executor.steps == ["deploy-1"]
    assert "recovered:deploy-1" not in verifier.steps
    assert store.load(plan_id).status == "paused"


def test_reconfirmation_uses_failure_store_instead_of_stale_plan_snapshot(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    plan = _change_plan(hierarchical=True)
    plan.authorize()
    plan.status = "paused"
    failure = FailureRecord(
        failure_id="failure-store-authority",
        stage="verification",
        category="test_failure",
        summary="目标未完成",
        technical_reason="checker failed",
        goal=plan.goal,
        goal_kind="execution",
        plan_id=plan.plan_id,
        options=[RecoveryOption(
            "continue_current_goal", "继续处理", "继续原目标",
            "continue_current_goal", recommended=True,
        )],
    )
    plan.failure = failure
    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    store.save(plan)
    failure.selected_option_id = "continue_current_goal"
    store.save_failure(failure)
    workflow, _, _, _, executor, _ = _workflow(tmp_path, plan)
    workflow.store = store

    result = workflow.confirm(plan.plan_id, plan.content_hash)

    assert result.kind == "paused"
    assert result.failure is None
    assert executor.steps == []


def test_reconfirmation_still_gates_on_unresolved_canonical_failure(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    plan = _change_plan(hierarchical=True)
    plan.authorize()
    plan.status = "paused"
    failure = FailureRecord(
        failure_id="failure-store-pending",
        stage="verification",
        category="test_failure",
        summary="目标未完成",
        technical_reason="checker failed",
        goal=plan.goal,
        goal_kind="execution",
        plan_id=plan.plan_id,
        options=[RecoveryOption(
            "continue_current_goal", "继续处理", "继续原目标",
            "continue_current_goal", recommended=True,
        )],
    )
    plan.failure = failure
    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    store.save(plan)
    store.save_failure(failure)
    workflow, _, _, _, executor, _ = _workflow(tmp_path, plan)
    workflow.store = store

    result = workflow.confirm(plan.plan_id, plan.content_hash)

    assert result.kind == "awaiting_user_decision"
    assert result.failure.failure_id == failure.failure_id
    assert executor.steps == []


def test_exact_reconfirmation_never_retries_even_conclusive_no_change_failure(tmp_path):
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    class SequenceExecutor:
        def __init__(self):
            self.calls = 0

        def execute(self, step):
            self.calls += 1
            return ExecutionEvidence(
                return_code=1 if self.calls == 1 else 0,
                environment_changed=False if self.calls == 1 else True,
            )

    class SequenceVerifier:
        def __init__(self):
            self.verify_calls = 0

        def verify_step(self, plan, step):
            self.verify_calls += 1
            status = "failed" if self.verify_calls == 1 else "passed"
            return SimpleNamespace(status=status, reason=status)

        def verify_plan_execution(self, plan):
            return SimpleNamespace(
                status="passed", reason="verified", failed_criteria=[],
            )

        def verify_recovered_step(self, plan, step):
            return SimpleNamespace(status="failed", reason="target still absent")

    plan = _change_plan(hierarchical=True)
    store = MemoryStore()
    executor = SequenceExecutor()
    workflow = MutationWorkflow(
        planner=FakePlanner([GoalOutcome(status="need_execution", plan=plan)]),
        binder=FakeBinder(),
        store=store,
        executor=executor,
        verifier=SequenceVerifier(),
    )
    submitted = _submit(workflow,
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )
    plan_id = submitted.plan.plan_id
    content_hash = submitted.plan.content_hash

    assert workflow.confirm(plan_id, content_hash).kind == "paused"
    resumed = workflow.confirm(plan_id, content_hash)

    assert resumed.kind == "paused"
    assert executor.calls == 1


def test_explicit_failure_recovery_retries_one_unchanged_paused_action_once(tmp_path):
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence

    plan = _change_plan(hierarchical=True)
    plan.authorize()
    plan.status = "paused"
    change = plan.steps[0]
    change.status = "paused"
    change.implementation_plan.status = "paused"
    atomic = change.implementation_plan.steps[0]
    atomic.status = "paused"
    atomic.execution_attempts = 1
    atomic.evidence = ExecutionEvidence(
        return_code=2,
        environment_changed=False,
    )
    workflow, _, _, store, executor, _ = _workflow(tmp_path, plan)
    store.save(plan)

    result = workflow.retry_unchanged_paused_action(plan.plan_id)

    assert result is not None
    assert result.kind == "goal_verification"
    assert executor.steps == ["deploy-1"]
    persisted = store.load(plan.plan_id)
    assert persisted.steps[0].implementation_plan.steps[0].execution_attempts == 2
    assert persisted.steps[0].implementation_plan.steps[0].status == "completed"


@pytest.mark.parametrize(
    ("return_code", "environment_changed", "timed_out", "attempts"),
    [
        (2, True, False, 1),
        (2, None, False, 1),
        (2, False, True, 1),
        (2, False, False, 2),
        (0, False, False, 1),
    ],
)
def test_explicit_failure_recovery_never_retries_unsafe_or_repeated_action(
    tmp_path, return_code, environment_changed, timed_out, attempts,
):
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence

    plan = _change_plan(hierarchical=True)
    plan.authorize()
    plan.status = "paused"
    change = plan.steps[0]
    change.status = "paused"
    change.implementation_plan.status = "paused"
    atomic = change.implementation_plan.steps[0]
    atomic.status = "paused"
    atomic.execution_attempts = attempts
    atomic.evidence = ExecutionEvidence(
        return_code=return_code,
        timed_out=timed_out,
        environment_changed=environment_changed,
    )
    workflow, _, _, store, executor, _ = _workflow(tmp_path, plan)
    store.save(plan)

    result = workflow.retry_unchanged_paused_action(plan.plan_id)

    assert result is None
    assert executor.steps == []


def test_explicit_failure_recovery_requires_original_exact_authorization(tmp_path):
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence

    plan = _change_plan(hierarchical=True)
    plan.status = "paused"
    change = plan.steps[0]
    change.status = "paused"
    change.implementation_plan.status = "paused"
    atomic = change.implementation_plan.steps[0]
    atomic.status = "paused"
    atomic.execution_attempts = 1
    atomic.evidence = ExecutionEvidence(return_code=2, environment_changed=False)
    workflow, _, _, store, executor, _ = _workflow(tmp_path, plan)
    store.save(plan)

    result = workflow.retry_unchanged_paused_action(plan.plan_id)

    assert result is None
    assert executor.steps == []


def test_recovery_reconciles_paused_zero_exit_step_without_reexecution(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import checkers as checker_module
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ExecutionEvidence, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    monkeypatch.setattr(
        checker_module, "_pid_cwd", lambda _pid: str(tmp_path),
    )
    micro = PrivilegedStep(
        step_id="restart-alpha__master",
        title="Restart master screen component",
        status="paused",
        execution_attempts=1,
        execution_binding=ExecutionBinding(
            kind="registered_action",
            risk="medium",
            action="restart_screen_component",
            args={
                "platform": "alpha", "component": "master",
                "screen_session": "alpha_m", "project_root": str(tmp_path),
                "master_port": 47001,
            },
        ),
        postconditions=[{
            "checker": "component_restart_identity",
            "args": {"component": "master", "project_root": str(tmp_path)},
        }],
        evidence=ExecutionEvidence(
            return_code=0,
            mutation={
                "kind": "component_restart", "component": "master",
                "session": "alpha_m", "old_pids": "", "new_pids": "202",
            },
        ),
    )
    change = ChangeStep(
        step_id="restart-alpha",
        title="restart alpha",
        objective="restart alpha master",
        risk="medium",
        expected_changes=["restart requested master role"],
        postconditions=[{"checker": "port_listening", "args": {"port": 47001}}],
        status="paused",
        implementation_plan=ImplementationPlan(
            implementation_id="impl-alpha",
            semantic_step_id="restart-alpha",
            objective="restart alpha",
            steps=[micro],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-alpha", goal="restart alpha", risk="medium",
        steps=[change], status="paused",
    )
    store = MemoryStore()
    store.save(plan)
    workflow = MutationWorkflow.__new__(MutationWorkflow)
    workflow.store = store
    workflow.verifier = PrivilegedVerifierAgent(llm=None)
    workflow._verification_step = lambda step: step

    reconciled = workflow.reconcile_recovery_state(plan)

    assert reconciled.steps[0].implementation_plan.steps[0].status == "completed"
    assert reconciled.steps[0].implementation_plan.status == "completed"
    assert reconciled.steps[0].implementation_plan.steps[0].execution_attempts == 1


def test_successor_plan_persists_completed_component_effect_across_replans():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        _authoritative_recovery_scope,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    def component_step(component, status, suffix):
        return PrivilegedStep(
            step_id="restart-alpha__%s" % component,
            title="restart %s" % component,
            status=status,
            execution_binding=ExecutionBinding(
                kind="registered_action", risk="medium",
                action="restart_screen_component",
                args={
                    "component": component,
                    "project_root": "/srv/alpha",
                    "platform": "alpha",
                    "screen_session": "alpha_%s" % suffix,
                },
                postconditions=[{
                    "checker": "screen_session_exists",
                    "args": {"session": "alpha_%s" % suffix},
                }],
            ),
        )

    def change(steps):
        return ChangeStep(
            step_id="restart-alpha", title="restart alpha",
            objective="restart all alpha roles", risk="medium",
            expected_changes=["all requested roles use Screen"],
            postconditions=[{
                "checker": "screen_session_exists",
                "args": {"session": "alpha_w"},
            }],
            status="paused",
            implementation_plan=ImplementationPlan(
                implementation_id="impl-alpha",
                semantic_step_id="restart-alpha",
                objective="restart all alpha roles",
                status="awaiting_confirmation",
                steps=steps,
            ),
        )

    root = PlanResource(
        name="alpha_root", kind="path", status="frozen",
        role="instance_root", value="/srv/alpha",
        consumers=["restart-alpha.project_root"],
    )
    predecessor = ChangePlan(
        plan_id="priv-ops-predecessor", goal="restart alpha", risk="medium",
        steps=[change([
            component_step("master", "completed", "m"),
            component_step("worker", "paused", "w"),
        ])],
        resources=[root], status="paused",
    )
    successor = ChangePlan(
        plan_id="priv-ops-successor", goal="restart alpha", risk="medium",
        steps=[change([
            component_step("master", "pending", "m"),
            component_step("worker", "pending", "w"),
        ])],
        resources=[root], status="awaiting_confirmation",
    )

    MutationWorkflow._inherit_completed_component_effects(
        successor, predecessor,
    )

    steps = successor.steps[0].implementation_plan.steps
    masters = [
        item for item in steps
        if item.execution_binding.args.get("component") == "master"
    ]
    assert len(masters) == 1
    assert masters[0].status == "completed"
    assert next(
        item for item in steps
        if item.execution_binding.args.get("component") == "worker"
    ).status == "pending"
    assert _authoritative_recovery_scope(successor)[
        "recovery_completed_components_by_root"
    ] == {"/srv/alpha": ["master"]}


def test_binding_failure_candidate_keeps_predecessor_completed_components(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    def atomic(component, status):
        return PrivilegedStep(
            step_id="restart-test__%s" % component,
            title="restart %s" % component,
            status=status,
            execution_binding=ExecutionBinding(
                kind="registered_action", risk="medium",
                action="restart_screen_component",
                args={
                    "component": component, "project_root": "/srv/test",
                    "platform": "test", "screen_session": "test_%s" % {
                        "master": "m", "celery": "c",
                        "web_terminal": "web", "worker": "w",
                    }[component],
                },
            ),
        )

    def semantic(implementation=None):
        return ChangeStep(
            step_id="restart-test", title="restart test",
            objective="restart test roles", risk="medium",
            expected_changes=["all test roles use Screen"],
            postconditions=[{
                "checker": "screen_session_exists", "args": {"session": "test_w"},
            }],
            implementation_plan=implementation,
        )

    root = PlanResource(
        name="test_root", kind="path", status="frozen",
        role="instance_root", value="/srv/test",
        consumers=["restart-test.project_root"],
    )
    predecessor = ChangePlan(
        plan_id="priv-ops-executed", goal="adopt all", risk="medium",
        steps=[semantic(ImplementationPlan(
            implementation_id="impl-executed", semantic_step_id="restart-test",
            objective="restart test roles", status="paused",
            steps=[
                atomic("master", "completed"), atomic("celery", "completed"),
                atomic("web_terminal", "completed"), atomic("worker", "paused"),
            ],
        ))],
        resources=[root], status="paused",
    )
    candidate = ChangePlan(
        plan_id="priv-ops-candidate", goal="adopt all", risk="medium",
        steps=[semantic()], resources=[root], status="draft",
    )

    class Binder:
        def bind(self, plan, **kwargs):
            plan.steps[0].implementation_plan = ImplementationPlan(
                implementation_id="impl-partial", semantic_step_id="restart-test",
                objective="restart test roles", status="draft",
                steps=[atomic("worker", "pending")],
            )
            raise ChangeBindingError("worker binding contract invalid")

    workflow = MutationWorkflow(
        planner=object(), binder=Binder(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=object(), verifier=object(),
    )
    result = workflow.bind_once(
        candidate, evidence_bundle=object(), predecessor_plan=predecessor,
    )

    assert result.status == "need_replan"
    steps = result.candidate_plan.steps[0].implementation_plan.steps
    by_component = {
        step.execution_binding.args["component"]: step for step in steps
    }
    assert {name for name, step in by_component.items() if step.status == "completed"} == {
        "master", "celery", "web_terminal",
    }
    assert by_component["worker"].status == "pending"


def test_successor_recovery_changes_action_but_preserves_screen_identity():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    def worker_step(root, platform, session, action, status):
        postconditions = [{
            "checker": "screen_session_exists",
            "args": {"session": session},
        }]
        return PrivilegedStep(
            step_id="restart-alpha__worker",
            title="recover worker",
            status=status,
            postconditions=[dict(item) for item in postconditions],
            execution_binding=ExecutionBinding(
                kind="registered_action", risk="medium", action=action,
                args={
                    "component": "worker", "project_root": root,
                    "platform": platform, "screen_session": session,
                },
                postconditions=[dict(item) for item in postconditions],
            ),
        )

    def plan(plan_id, root, step):
        semantic = ChangeStep(
            step_id="restart-alpha", title="restart alpha",
            objective="recover worker", risk="medium",
            expected_changes=["worker uses Screen"],
            postconditions=[{
                "checker": "screen_session_exists",
                "args": {"session": step.execution_binding.args["screen_session"]},
            }],
            implementation_plan=ImplementationPlan(
                implementation_id="impl-alpha",
                semantic_step_id="restart-alpha",
                objective="recover worker", steps=[step],
            ),
        )
        resource = PlanResource(
            name="alpha_root", kind="path", status="frozen",
            role="instance_root", value=root,
            consumers=["restart-alpha.project_root"],
        )
        return ChangePlan(
            plan_id=plan_id, goal="recover worker", risk="medium",
            steps=[semantic], resources=[resource],
        )

    predecessor = plan(
        "priv-ops-predecessor", "/srv/alpha",
        worker_step(
            "/srv/alpha", "alpha", "alpha_w",
            "start_screen_component", "paused",
        ),
    )
    successor = plan(
        "priv-ops-successor", "/srv/alpha",
        worker_step(
            "/srv/alpha", "renamed-alpha", "renamed-alpha_w",
            "restart_screen_component", "pending",
        ),
    )

    MutationWorkflow._inherit_completed_component_effects(
        successor, predecessor,
    )

    recovered = successor.steps[0].implementation_plan.steps[0]
    assert recovered.execution_binding.action == "restart_screen_component"
    assert recovered.execution_binding.args["platform"] == "alpha"
    assert recovered.execution_binding.args["screen_session"] == "alpha_w"
    assert recovered.execution_binding.postconditions[0]["args"]["session"] == "alpha_w"
    assert recovered.postconditions[0]["args"]["session"] == "alpha_w"

    unrelated = plan(
        "priv-ops-unrelated", "/srv/beta",
        worker_step(
            "/srv/beta", "beta", "beta_w",
            "restart_screen_component", "pending",
        ),
    )
    MutationWorkflow._inherit_completed_component_effects(
        unrelated, predecessor,
    )
    assert unrelated.steps[0].implementation_plan.steps[0].execution_binding.args[
        "screen_session"
    ] == "beta_w"


def test_confirmation_marks_completed_actions_as_non_repeating():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    atomic = PrivilegedStep(
        step_id="restart-alpha__master", title="restart master",
        status="completed",
        execution_binding=ExecutionBinding(
            kind="registered_action", risk="medium",
            action="restart_screen_component",
            args={
                "component": "master", "project_root": "/srv/alpha",
                "platform": "alpha", "screen_session": "alpha_m",
            },
        ),
    )
    change = ChangeStep(
        step_id="restart-alpha", title="restart alpha",
        objective="restart alpha", risk="medium",
        expected_changes=["master uses Screen"],
        postconditions=[{"checker": "screen_session_exists"}],
        implementation_plan=ImplementationPlan(
            implementation_id="impl-alpha", semantic_step_id="restart-alpha",
            objective="restart alpha", steps=[atomic],
        ),
    )
    plan = ChangePlan.new(goal="restart alpha", risk="medium", steps=[change])

    message = MutationWorkflow._confirmation_message(plan)

    assert "已完成（不会重复执行）：重启交互式 Screen 组件 master（alpha_m）" in message


def test_semantic_config_verification_is_composed_from_atomic_bindings():
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    plan = _change_plan(hierarchical=True)
    change = plan.steps[0]
    change.postconditions = [
        {"checker": "process_not_running", "args": {"pattern": "worker_main"}},
        {"checker": "port_listening", "args": {"port": 47001}},
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:47001/server_health/", "expected_code": 1}},
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:49999/server_health/", "expected_code": 1}},
    ]
    step = change.implementation_plan.steps[0]
    step.execution_binding.action = "set_python_class_attribute"
    step.execution_binding.args = {
        "path": "/srv/app/vemu_config/config.py",
        "class_name": "WtxConfig",
        "attribute": "master_port",
        "value": "47001",
    }
    step.postconditions = [
        {
            "checker": "python_attribute_equals",
            "args": {
                "module": "vemu_config.config",
                "attribute": "master_port",
                "expected": "47001",
                "cwd": "/srv/app",
            },
        }
    ]

    semantic = MutationWorkflow._semantic_verification_step(change)

    assert semantic.postconditions == [
        {
            "checker": "port_listening",
            "args": {"port": 47001},
        },
        {
            "checker": "backend_health",
            "args": {
                "url": "http://127.0.0.1:47001/server_health/",
                "expected_code": 1,
            },
        },
        {
            "checker": "python_attribute_equals",
            "args": {
                "module": "vemu_config.config",
                "attribute": "WtxConfig.master_port",
                "expected": 47001,
                "cwd": "/srv/app",
            },
        },
        {
            "checker": "file_contains",
            "args": {
                "path": "/srv/app/vemu_config/config.py",
                "text": "PROJ_CONFIG = WtxConfig()",
            },
        },
    ]


def test_semantic_verification_uses_latest_execution_not_appended_completed_node():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionEvidence, ExecutionBinding, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangeStep
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    worker_evidence = ExecutionEvidence(
        return_code=0, stdout="worker", finished_at="2026-08-25T04:31:00+00:00",
    )
    old_master_evidence = ExecutionEvidence(
        return_code=0, stdout="master", finished_at="2026-08-25T03:00:00+00:00",
    )

    def atomic(step_id, component, evidence):
        return PrivilegedStep(
            step_id=step_id, title=component, status="completed",
            evidence=evidence,
            execution_binding=ExecutionBinding(
                kind="registered_action", risk="medium",
                action="restart_screen_component",
                args={"component": component, "project_root": "/srv/alpha"},
            ),
        )

    change = ChangeStep(
        step_id="restart-alpha", title="restart alpha worker",
        objective="restart worker", risk="medium",
        expected_changes=["worker healthy"],
        postconditions=[{"checker": "backend_health", "args": {
            "url": "http://127.0.0.1:47002/server_health/",
        }}],
        implementation_plan=ImplementationPlan(
            implementation_id="impl-alpha", semantic_step_id="restart-alpha",
            objective="restart worker",
            steps=[
                atomic("worker", "worker", worker_evidence),
                atomic("master", "master", old_master_evidence),
            ],
        ),
    )

    semantic = MutationWorkflow._semantic_verification_step(change)

    assert semantic.evidence is worker_evidence


def test_semantic_failure_demotes_only_component_that_owns_failed_health_check():
    from klonet_agent.ops.privileged.contracts import (
        CheckResult, ExecutionBinding, ExecutionEvidence, ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangeStep
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    def component_step(component, port, finished_at):
        return PrivilegedStep(
            step_id="restart-alpha__%s" % component,
            title="restart %s" % component,
            status="completed",
            evidence=ExecutionEvidence(return_code=0, finished_at=finished_at),
            execution_binding=ExecutionBinding(
                kind="registered_action", risk="medium",
                action="restart_screen_component",
                args={
                    "component": component,
                    "%s_port" % component: port,
                    "project_root": "/srv/alpha",
                    "screen_session": "alpha_%s" % component,
                },
            ),
        )

    master = component_step("master", 47001, "2026-08-25T03:00:00+00:00")
    worker = component_step("worker", 47002, "2026-08-25T04:00:00+00:00")
    change = ChangeStep(
        step_id="restart-alpha", title="restart alpha", objective="restart alpha",
        risk="medium", expected_changes=["roles healthy"],
        postconditions=[
            {"checker": "backend_health", "args": {
                "url": "http://127.0.0.1:47001/server_health/",
            }},
            {"checker": "backend_health", "args": {
                "url": "http://127.0.0.1:47002/server_health/",
            }},
        ],
        implementation_plan=ImplementationPlan(
            implementation_id="impl-alpha", semantic_step_id="restart-alpha",
            objective="restart alpha", status="completed",
            steps=[master, worker],
        ),
    )
    semantic = MutationWorkflow._semantic_verification_step(change)
    semantic.checks = [
        CheckResult("backend_health", "passed", observed="http=200"),
        CheckResult("backend_health", "failed", observed="http=502"),
    ]

    changed = MutationWorkflow._demote_semantically_unaccepted_components(
        change, semantic,
    )

    assert changed is True
    assert master.status == "completed"
    assert worker.status == "paused"
    assert change.implementation_plan.status == "paused"
    assert worker.checks[-1].observed == "http=502"


def test_recovery_reconciles_old_semantic_failure_before_building_scope():
    from klonet_agent.ops.privileged.contracts import (
        CheckResult, ExecutionBinding, ExecutionEvidence, ImplementationPlan,
        PrivilegedStep, VerificationDecision,
    )
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        _authoritative_recovery_scope,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    def component_step(component, port, finished_at):
        return PrivilegedStep(
            step_id="restart-alpha__%s" % component,
            title="restart %s" % component,
            status="completed",
            evidence=ExecutionEvidence(return_code=0, finished_at=finished_at),
            execution_binding=ExecutionBinding(
                kind="registered_action", risk="medium",
                action="restart_screen_component",
                args={
                    "component": component,
                    "%s_port" % component: port,
                    "project_root": "/srv/alpha",
                    "screen_session": "alpha_%s" % component,
                },
            ),
        )

    class DeterministicVerifier:
        def verify_deterministic_step(self, plan, step):
            del plan
            step.checks = [
                CheckResult("backend_health", "passed", observed="http=200"),
                CheckResult("backend_health", "failed", observed="http=502"),
            ]
            return VerificationDecision(status="failed", reason="worker unhealthy")

    change = ChangeStep(
        step_id="restart-alpha", title="restart alpha", objective="restart alpha",
        risk="medium", expected_changes=["roles healthy"],
        postconditions=[
            {"checker": "backend_health", "args": {
                "url": "http://127.0.0.1:47001/server_health/",
            }},
            {"checker": "backend_health", "args": {
                "url": "http://127.0.0.1:47002/server_health/",
            }},
        ], status="paused", observation="semantic acceptance failed",
        implementation_plan=ImplementationPlan(
            implementation_id="impl-alpha", semantic_step_id="restart-alpha",
            objective="restart alpha", status="completed",
            steps=[
                component_step("master", 47001, "2026-08-25T03:00:00+00:00"),
                component_step("worker", 47002, "2026-08-25T04:00:00+00:00"),
            ],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-alpha", goal="restart alpha", risk="medium",
        steps=[change], status="paused",
        resources=[PlanResource(
            name="alpha_root", kind="path", status="frozen",
            role="instance_root", value="/srv/alpha",
            consumers=["restart-alpha.project_root"],
        )],
    )
    workflow = MutationWorkflow.__new__(MutationWorkflow)
    workflow.store = MemoryStore()
    workflow.store.save(plan)
    workflow.verifier = DeterministicVerifier()

    reconciled = workflow.reconcile_recovery_state(plan)
    steps = reconciled.steps[0].implementation_plan.steps
    scope = _authoritative_recovery_scope(reconciled)

    assert [item.status for item in steps] == ["completed", "paused"]
    assert scope["recovery_completed_components_by_root"] == {
        "/srv/alpha": ["master"],
    }


def test_recovery_accepts_old_semantic_step_when_all_checks_now_pass():
    from klonet_agent.ops.privileged.contracts import (
        CheckResult, ExecutionBinding, ExecutionEvidence, ImplementationPlan,
        PrivilegedStep, VerificationDecision,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    class PassingVerifier:
        def verify_deterministic_step(self, plan, step):
            del plan
            step.checks = [CheckResult("backend_health", "passed", observed="http=200")]
            return VerificationDecision(status="passed", reason="healthy")

    atomic = PrivilegedStep(
        step_id="restart-alpha__worker", title="restart worker",
        status="completed", evidence=ExecutionEvidence(return_code=0),
        execution_binding=ExecutionBinding(
            kind="registered_action", risk="medium",
            action="restart_screen_component",
            args={
                "component": "worker", "worker_port": 47002,
                "project_root": "/srv/alpha", "screen_session": "alpha_w",
            },
        ),
    )
    change = ChangeStep(
        step_id="restart-alpha", title="restart alpha worker",
        objective="restart worker", risk="medium",
        expected_changes=["worker healthy"],
        postconditions=[{"checker": "backend_health", "args": {
            "url": "http://127.0.0.1:47002/server_health/",
        }}], status="paused", observation="old semantic failure",
        implementation_plan=ImplementationPlan(
            implementation_id="impl-alpha", semantic_step_id="restart-alpha",
            objective="restart worker", status="completed", steps=[atomic],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-alpha", goal="restart alpha", risk="medium",
        steps=[change], status="paused",
    )
    workflow = MutationWorkflow.__new__(MutationWorkflow)
    workflow.store = MemoryStore()
    workflow.store.save(plan)
    workflow.verifier = PassingVerifier()

    reconciled = workflow.reconcile_recovery_state(plan)

    assert reconciled.steps[0].status == "completed"
    assert atomic.status == "completed"


def test_control_command_requires_exact_workflow_syntax(tmp_path):
    workflow, _, _, _, executor, _ = _workflow(tmp_path, _change_plan())
    submitted = _submit(workflow, "deploy", evidence_bundle=object(), evidence_conclusion=object())

    ignored = workflow.handle_control(
        "please confirm-priv-plan %s %s" % (
            submitted.plan.plan_id,
            submitted.plan.content_hash,
        )
    )

    assert ignored is None
    assert executor.steps == []


def test_planner_evidence_gap_returns_to_discovery_then_replans(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    bundle = SimpleNamespace(records=[])
    gap = GoalOutcome(
        status="need_evidence",
        evidence_requests=[ProbeRequest("ports", {"ports": [47001]}, "freeze port")],
    )
    ready = GoalOutcome(status="need_execution", plan=_change_plan())
    planner = FakePlanner([gap, ready])

    class Discovery:
        calls = 0

        def collect_requests(self, requests, evidence_bundle):
            self.calls += 1
            evidence_bundle.records.append("port-free")
            return evidence_bundle

    class Synthesis:
        calls = 0

        def synthesize(self, goal, evidence_bundle):
            self.calls += 1
            return SimpleNamespace(confirmed_facts=["port-free"])

    discovery = Discovery()
    synthesis = Synthesis()
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=discovery,
        synthesis=synthesis,
        max_replanning_rounds=2,
    )

    result = _submit(workflow,
        "deploy", evidence_bundle=bundle, evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == 2
    assert discovery.calls == synthesis.calls == 1


def test_repeated_process_detail_gap_uses_readonly_fallback_then_replans(tmp_path):
    import json

    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, GoalOutcome, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    request = ProbeRequest(
        "process_detail",
        {"process_keywords": ["master_main"]},
        "继承 master 运行身份",
        ("master cwd", "master python executable", "master full cmdline"),
    )
    bundle = EvidenceBundle(goal="重启 test 平台")
    bundle.add(EvidenceRecord.from_probe(
        request,
        "pid=1234 cwd=unknown python_executable=unknown cmdline=truncated",
    ))
    planner = FakePlanner([
        GoalOutcome(status="need_evidence", evidence_requests=[request]),
        GoalOutcome(status="need_execution", plan=_change_plan()),
    ])

    class FallbackLLM:
        def complete(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({
                    "status": "command",
                    "command": "ps -p 1234 -o user=,args=",
                    "reason": "obtain full process identity",
                })
            ))])

    commands = []
    discovery = DiscoveryAgent(
        FallbackLLM(),
        probe_runner=lambda requests: (_ for _ in ()).throw(
            AssertionError("cached registered probe must not be repeated")
        ),
        readonly_command_runner=lambda command: commands.append(command) or (
            "lzl /home/lzl/miniconda3/envs/test/bin/python3.8 "
            "-m gunicorn master_main:flask_app"
        ),
    )
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=discovery,
        synthesis=SimpleNamespace(
            synthesize=lambda goal, evidence_bundle: SimpleNamespace()
        ),
    )
    progress = []

    result = _submit(
        workflow,
        bundle.goal,
        evidence_bundle=bundle,
        evidence_conclusion=SimpleNamespace(),
        intent_context={"operation": "restart", "scope": "platform"},
        on_progress=progress.append,
    )

    assert result.kind == "awaiting_confirmation"
    assert commands == ["ps -p 1234 -o user=,args="]
    assert any(
        item.request.probe == "readonly_command" and item.status == "available"
        for item in bundle.records
    )
    assert planner.calls == 2
    assert any("已取得新的目标相关事实" in item for item in progress)


def test_verified_candidate_plan_is_finalized_without_model_reselection(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    candidate = _change_plan()

    class CandidatePlanner:
        def __init__(self):
            self.calls = 0
            self.finalize_calls = 0

        def plan(self, *args, **kwargs):
            self.calls += 1
            return GoalOutcome(
                status="need_evidence",
                candidate_plan=candidate,
                evidence_requests=[
                    ProbeRequest("ports", {"ports": [47001]}, "verify port")
                ],
            )

        def finalize_candidate(self, plan, bundle):
            self.finalize_calls += 1
            assert plan is candidate
            return GoalOutcome(status="need_execution", plan=plan)

    planner = CandidatePlanner()
    discovery = SimpleNamespace(
        collect_requests=lambda requests, evidence_bundle: evidence_bundle
    )
    synthesis = SimpleNamespace(
        synthesize=lambda goal, evidence_bundle: SimpleNamespace()
    )
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=discovery,
        synthesis=synthesis,
    )

    result = _submit(workflow,
        "deploy", evidence_bundle=SimpleNamespace(), evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == planner.finalize_calls == 1


def test_predecessor_plan_is_not_finalized_as_current_evidence_candidate(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        GoalOutcome, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    predecessor = _change_plan()
    successor = _change_plan()

    class ReplanningPlanner:
        def __init__(self):
            self.calls = []
            self.finalize_calls = 0

        def plan(self, *args, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                # The current decision cannot yet form a candidate.  The
                # predecessor is supplied separately only to preserve scope.
                return GoalOutcome(
                    status="need_evidence",
                    evidence_requests=[
                        ProbeRequest("ports", {"ports": [5115]}, "new decision")
                    ],
                )
            return GoalOutcome(status="need_execution", plan=successor)

        def finalize_candidate(self, plan, bundle):
            self.finalize_calls += 1
            raise AssertionError("predecessor must never enter candidate finalization")

    planner = ReplanningPlanner()
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=SimpleNamespace(
            collect_requests=lambda requests, evidence_bundle: evidence_bundle
        ),
        synthesis=SimpleNamespace(
            synthesize=lambda goal, evidence_bundle: SimpleNamespace()
        ),
    )

    result = _submit(
        workflow,
        "revise deploy",
        evidence_bundle=SimpleNamespace(),
        evidence_conclusion=SimpleNamespace(),
        initial_candidate_plan=predecessor,
    )

    assert result.kind == "awaiting_confirmation"
    assert len(planner.calls) == 2
    assert planner.calls[1]["candidate_plan"] is predecessor
    assert planner.finalize_calls == 0


def test_replan_accumulates_affected_steps_across_evidence_rounds(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        GoalOutcome, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    final_plan = _change_plan()

    class GapPlanner:
        def __init__(self):
            self.calls = []

        def plan(self, *args, **kwargs):
            self.calls.append(kwargs)
            turn = len(self.calls)
            if turn == 1:
                return GoalOutcome(
                    status="need_evidence",
                    evidence_requests=[ProbeRequest(
                        "ports", {"ports": [5115]}, "alpha port",
                        affected_steps=("restart-alpha",),
                    )],
                    replan_context={
                        "active_gap_affected_steps": ["restart-alpha"],
                    },
                )
            if turn == 2:
                return GoalOutcome(
                    status="need_evidence",
                    evidence_requests=[ProbeRequest(
                        "ports", {"ports": [45556]}, "beta port",
                        affected_steps=("restart-beta",),
                    )],
                    replan_context={
                        "active_gap_affected_steps": ["restart-beta"],
                    },
                )
            return GoalOutcome(status="need_execution", plan=final_plan)

    planner = GapPlanner()
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=SimpleNamespace(
            collect_requests=lambda requests, evidence_bundle: evidence_bundle
        ),
        synthesis=SimpleNamespace(
            synthesize=lambda goal, evidence_bundle: SimpleNamespace()
        ),
    )

    result = _submit(
        workflow, "revise deploy",
        evidence_bundle=SimpleNamespace(),
        evidence_conclusion=SimpleNamespace(),
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls[1]["intent_context"]["active_gap_affected_steps"] == [
        "restart-alpha"
    ]
    assert planner.calls[2]["intent_context"]["active_gap_affected_steps"] == [
        "restart-alpha", "restart-beta"
    ]


def test_occupied_candidate_ports_trigger_one_bounded_replan(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    candidate = _change_plan()
    replacement = _change_plan()

    class CandidatePlanner:
        def __init__(self):
            self.calls = []
            self.finalize_calls = 0

        def plan(self, *args, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return GoalOutcome(
                    status="need_evidence",
                    candidate_plan=candidate,
                    evidence_requests=[
                        ProbeRequest("ports", {"ports": [6379]}, "verify port")
                    ],
                )
            return GoalOutcome(status="need_execution", plan=replacement)

        def finalize_candidate(self, plan, bundle):
            self.finalize_calls += 1
            return GoalOutcome(
                status="blocked",
                candidate_plan=plan,
                reason="candidate ports became occupied: 6379",
            )

    planner = CandidatePlanner()
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=SimpleNamespace(
            collect_requests=lambda requests, evidence_bundle: evidence_bundle
        ),
        synthesis=SimpleNamespace(
            synthesize=lambda goal, evidence_bundle: SimpleNamespace()
        ),
    )

    result = _submit(workflow,
        "deploy", evidence_bundle=SimpleNamespace(), evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_confirmation"
    assert len(planner.calls) == 2
    assert planner.finalize_calls == 1
    assert "candidate ports became occupied: 6379" in planner.calls[1]["binding_feedback"]


def test_planner_discovery_loop_stops_at_explicit_budget(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    gap = GoalOutcome(
        status="need_evidence",
        evidence_requests=[ProbeRequest("ports", {"ports": [47001]}, "freeze port")],
    )
    planner = FakePlanner([gap, gap, gap, gap, gap])
    discovery = SimpleNamespace(
        collect_requests=lambda requests, evidence_bundle: evidence_bundle
    )
    synthesis = SimpleNamespace(
        synthesize=lambda goal, evidence_bundle: SimpleNamespace()
    )
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=discovery,
        synthesis=synthesis,
        max_replanning_rounds=4,
    )

    result = _submit(workflow,
        "deploy", evidence_bundle=SimpleNamespace(), evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_user_decision"
    assert result.failure.category == "planning_evidence_budget_exhausted"
    assert result.failure.options[0].recommended is True
    assert planner.calls == 5


def test_planner_discovery_stops_after_one_no_progress_replan(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, GoalOutcome, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    gap = GoalOutcome(
        status="need_evidence",
        evidence_requests=[ProbeRequest(
            "command_available", {"commands": ["screen"]}, "find screen",
        )],
    )
    planner = FakePlanner([gap, gap])
    bundle = EvidenceBundle(goal="重启全部平台")

    class Discovery:
        calls = 0

        def collect_requests(self, requests, evidence_bundle):
            self.calls += 1
            request = requests[0]
            evidence_bundle.add(EvidenceRecord.from_probe(
                request,
                "probe refused: probe_not_registered=%s" % request.probe,
                status="unavailable",
            ))
            return evidence_bundle

    discovery = Discovery()
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=discovery,
        synthesis=SimpleNamespace(
            synthesize=lambda goal, evidence_bundle: SimpleNamespace()
        ),
        max_replanning_rounds=4,
    )

    progress = []
    result = _submit(
        workflow,
        bundle.goal,
        evidence_bundle=bundle,
        evidence_conclusion=SimpleNamespace(),
        on_progress=progress.append,
    )

    assert result.kind == "awaiting_user_decision"
    assert result.failure.category == "planning_evidence_no_progress"
    assert "probes=command_available" in result.failure.technical_reason
    assert planner.calls == discovery.calls == 2
    assert any("Replan 1/4" in item and "command_available" in item for item in progress)
    assert any("安全只读补证均未产生新事实" in item for item in progress)
    assert any("Planner 返回 need_evidence" in item for item in progress)


def test_post_execution_replan_failure_preserves_changed_environment():
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, GoalOutcome, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    predecessor = _change_plan()
    predecessor.status = "paused"
    predecessor.steps[0].status = "paused"
    predecessor.steps[0].evidence = ExecutionEvidence(
        return_code=1, environment_changed=True,
    )
    gap = GoalOutcome(
        status="need_evidence",
        evidence_requests=[ProbeRequest(
            "runtime_startup_identity", {"project_root": "/srv/app"},
            "recover missing role", ("run_as_uid",),
            "refresh", "gap-runtime",
        )],
    )
    workflow = MutationWorkflow(
        planner=FakePlanner([gap, gap]),
        binder=FakeBinder(), store=MemoryStore(), executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=SimpleNamespace(
            collect_requests=lambda requests, evidence_bundle: evidence_bundle
        ),
        synthesis=SimpleNamespace(
            synthesize=lambda goal, evidence_bundle: SimpleNamespace()
        ),
        max_replanning_rounds=4,
    )

    result = _submit(
        workflow, "recover app",
        evidence_bundle=EvidenceBundle(goal="recover app"),
        evidence_conclusion=SimpleNamespace(),
        initial_candidate_plan=predecessor,
    )

    assert result.failure.category == "planning_evidence_no_progress"
    assert result.failure.environment_changed == "true"
    assert result.failure.plan_id == predecessor.plan_id


def test_default_discovery_budget_allows_four_rounds_then_ready(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    gap = GoalOutcome(
        status="need_evidence",
        evidence_requests=[ProbeRequest("ports", {"ports": [47001]}, "freeze port")],
    )
    planner = FakePlanner([gap, gap, gap, gap, GoalOutcome(
        status="need_execution", plan=_change_plan()
    )])
    workflow = MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=SimpleNamespace(
            collect_requests=lambda requests, evidence_bundle: evidence_bundle
        ),
        synthesis=SimpleNamespace(
            synthesize=lambda goal, evidence_bundle: SimpleNamespace()
        ),
    )

    result = _submit(workflow,
        "deploy", evidence_bundle=SimpleNamespace(), evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == 5


def test_verifier_exception_is_persisted_as_pause_after_single_execution(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    executor = FakeExecutor()
    store = MemoryStore()
    workflow = MutationWorkflow(
        planner=FakePlanner([GoalOutcome(status="need_execution", plan=_change_plan())]),
        binder=FakeBinder(),
        store=store,
        executor=executor,
        verifier=RaisingVerifier(),
    )
    submitted = _submit(workflow,
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )

    result = workflow.confirm(submitted.plan.plan_id, submitted.plan.content_hash)

    assert result.kind == "paused"
    assert result.failure is None
    assert "checker crashed" in result.message
    assert executor.steps == ["deploy"]


def test_binder_failure_replans_at_most_once_then_succeeds(tmp_path):
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    class Binder:
        calls = 0

        def bind(self, plan, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ChangeBindingError("capability unavailable")
            plan.status = "awaiting_confirmation"
            return plan

    planner = FakePlanner(
        [
            GoalOutcome(status="need_execution", plan=_change_plan()),
            GoalOutcome(status="need_execution", plan=_change_plan()),
        ]
    )
    binder = Binder()
    workflow = MutationWorkflow(
        planner=planner,
        binder=binder,
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
    )

    result = _submit(workflow,
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == binder.calls == 2


def test_binding_replan_receives_structured_implementation_gap(tmp_path):
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    class Planner:
        def __init__(self):
            self.feedback = []

        def plan(self, *args, binding_feedback="", **kwargs):
            self.feedback.append(binding_feedback)
            return GoalOutcome(status="need_execution", plan=_change_plan())

    class Binder:
        calls = 0

        def bind(self, plan, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ChangeBindingError(
                    "runtime_recovery_action_missing=worker",
                    failed_criteria=["worker 缺少启动动作"],
                    replan_context={
                        "step_id": "restart-runtime",
                        "missing_roles": ["worker"],
                        "missing_effects": ["worker_runtime_recovery"],
                    },
                )
            plan.status = "awaiting_confirmation"
            return plan

    planner = Planner()
    workflow = MutationWorkflow(
        planner=planner, binder=Binder(), store=MemoryStore(),
        executor=FakeExecutor(), verifier=FakeVerifier(),
    )

    result = _submit(
        workflow, "收编 test worker", evidence_bundle=object(),
        evidence_conclusion=object(),
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.feedback[0] == ""
    feedback = json.loads(planner.feedback[1])
    assert feedback["implementation_gap"] == {
        "step_id": "restart-runtime",
        "missing_roles": ["worker"],
        "missing_effects": ["worker_runtime_recovery"],
    }
    assert feedback["failed_criteria"] == ["worker 缺少启动动作"]


def test_binding_replan_need_evidence_returns_to_discovery_then_binds(tmp_path):
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, GoalOutcome, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    request = ProbeRequest(
        "process_detail", {"project_root": "/srv/test"},
        "确认 worker 的运行身份", required_facts=("worker cwd",),
    )

    class Binder:
        calls = 0

        def bind(self, plan, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ChangeBindingError("runtime_recovery_action_missing=worker")
            plan.status = "awaiting_confirmation"
            return plan

    class Discovery:
        calls = 0

        def collect_requests(self, requests, bundle):
            self.calls += 1
            bundle.add(EvidenceRecord.from_probe(
                requests[0], "worker cwd=/srv/test pid=1234",
            ))
            return bundle

    class Synthesis:
        calls = 0

        def synthesize(self, goal, bundle):
            self.calls += 1
            return SimpleNamespace(confirmed_facts=["worker cwd=/srv/test"])

    planner = FakePlanner([
        GoalOutcome(status="need_execution", plan=_change_plan()),
        GoalOutcome(status="need_evidence", evidence_requests=[request]),
        GoalOutcome(status="need_execution", plan=_change_plan()),
    ])
    binder = Binder()
    discovery = Discovery()
    synthesis = Synthesis()
    workflow = MutationWorkflow(
        planner=planner, binder=binder, store=MemoryStore(),
        executor=FakeExecutor(), verifier=FakeVerifier(),
        discovery=discovery, synthesis=synthesis,
    )
    bundle = EvidenceBundle(goal="收编 test worker")

    result = _submit(
        workflow, bundle.goal, evidence_bundle=bundle,
        evidence_conclusion=SimpleNamespace(),
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == 3
    assert binder.calls == 2
    assert discovery.calls == synthesis.calls == 1
    assert any(record.request.probe == "process_detail" for record in bundle.records)


def test_binding_probe_evidence_is_visible_to_the_following_replan(tmp_path):
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, GoalOutcome,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    discovery = DiscoveryAgent(
        SimpleNamespace(),
        probe_runner=lambda requests: "pid=4321 cwd=/srv/test",
    )

    class Planner:
        def __init__(self):
            self.record_counts = []

        def plan(self, goal, bundle, conclusion, **kwargs):
            self.record_counts.append(len(bundle.records))
            return GoalOutcome(status="need_execution", plan=_change_plan())

    class Binder:
        calls = 0

        def bind(self, plan, **kwargs):
            self.calls += 1
            if self.calls == 1:
                discovery.run_ad_hoc_requests([{
                    "probe": "process_detail",
                    "args": {"project_root": "/srv/test"},
                    "purpose": "确认 worker 运行身份",
                }])
                raise ChangeBindingError("worker identity needed by replan")
            plan.status = "awaiting_confirmation"
            return plan

    class Synthesis:
        calls = 0

        def synthesize(self, goal, bundle):
            self.calls += 1
            return SimpleNamespace(confirmed_facts=["pid=4321"])

    planner = Planner()
    binder = Binder()
    synthesis = Synthesis()
    workflow = MutationWorkflow(
        planner=planner, binder=binder, store=MemoryStore(),
        executor=FakeExecutor(), verifier=FakeVerifier(),
        discovery=discovery, synthesis=synthesis,
    )
    bundle = EvidenceBundle(goal="收编 test worker")

    result = _submit(
        workflow, bundle.goal, evidence_bundle=bundle,
        evidence_conclusion=SimpleNamespace(),
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.record_counts == [0, 1]
    assert synthesis.calls == 1
    assert bundle.records[0].request.probe == "process_detail"


def test_second_binder_failure_is_persisted_as_blocked_without_traceback(tmp_path):
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    class Binder:
        calls = 0

        def bind(self, plan, **kwargs):
            self.calls += 1
            raise ChangeBindingError("clone target could not be grounded")

    store = MemoryStore()
    workflow = MutationWorkflow(
        planner=FakePlanner(
            [
                GoalOutcome(status="need_execution", plan=_change_plan()),
                GoalOutcome(status="need_execution", plan=_change_plan()),
            ]
        ),
        binder=Binder(),
        store=store,
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
    )

    result = _submit(workflow,
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )

    assert result.kind == "awaiting_user_decision"
    assert result.failure.stage == "binding"
    assert "clone target could not be grounded" in result.failure.technical_reason
    assert result.plan.status == "blocked"
    assert store.load(result.plan.plan_id).status == "blocked"


def test_binding_replan_failure_reports_system_gap_without_blaming_user(tmp_path):
    from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    gap = (
        "语义计划要求恢复 worker，但 Implementation Plan 没有为该角色"
        "绑定启动或重启动作。"
    )

    class Binder:
        def bind(self, plan, **kwargs):
            raise ChangeBindingError(
                "runtime_recovery_action_missing=worker",
                category="implementation_contract_invalid",
                replan_recommended=False,
                failed_criteria=[gap],
            )

    workflow = MutationWorkflow(
        planner=FakePlanner([
            GoalOutcome(status="need_execution", plan=_change_plan()),
            GoalOutcome(status="blocked", reason="binding replan did not produce a ready plan"),
        ]),
        binder=Binder(), store=MemoryStore(), executor=FakeExecutor(),
        verifier=FakeVerifier(),
    )

    result = _submit(
        workflow, "把 test 的 worker 收编进 Screen",
        evidence_bundle=object(), evidence_conclusion=object(),
    )

    assert result.kind == "awaiting_user_decision"
    assert result.failure.failed_checks == [gap]
    assert result.failure.missing_decisions == []
    assert "系统确认的缺口：%s" % gap in result.message
    assert "是否需要补充用户边界：否" in result.message
    assert "没有显示必须补充的用户边界" in result.failure.options[1].description


@pytest.mark.parametrize(
    "user_input",
    [
        "2 告诉我为什么失败了",
        "选择 2：先告诉我这次为什么失败",
    ],
)
def test_direction_choice_with_failure_question_explains_gap_then_asks_scope(
    tmp_path, user_input,
):
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    gap = "语义计划要求恢复 worker，但实施计划缺少 worker 启动或重启动作。"
    workflow = MutationWorkflow(
        planner=object(), binder=object(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=object(), verifier=object(),
    )
    failed = workflow.failure_result(
        stage="binding",
        category="binding_replan_unresolved",
        summary="实施绑定发现计划缺少动作约束。",
        technical_reason="runtime_recovery_action_missing=worker",
        goal="收编 test worker",
        goal_kind="execution",
        failed_checks=[gap],
    )

    result = workflow.handle_control(user_input)

    assert result.kind == "clarification"
    assert result.failure.failure_id == failed.failure.failure_id
    assert result.failure.selected_option_id == "provide_direction"
    assert "技术原因：runtime_recovery_action_missing=worker" in result.message
    assert gap in result.message
    assert "没有显示必须由你补充的边界" in result.message
    assert "继续处理" in result.message


def test_failure_record_derives_completed_step_summary_from_authoritative_plan(
    tmp_path,
):
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    workflow = MutationWorkflow(
        planner=object(), binder=object(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=object(), verifier=object(),
    )
    plan = ChangePlan(
        plan_id="priv-ops-partial", goal="收编全部平台", risk="medium",
        status="executing",
        steps=[
            ChangeStep(
                step_id="restart-alpha", title="收编 alpha", objective="alpha",
                risk="medium", expected_changes=["alpha in Screen"],
                postconditions=[{"checker": "process_running", "args": {}}],
                status="completed",
            ),
            ChangeStep(
                step_id="restart-beta", title="收编 beta", objective="beta",
                risk="medium", expected_changes=["beta in Screen"],
                postconditions=[{"checker": "process_running", "args": {}}],
                status="paused",
            ),
        ],
    )

    result = workflow.failure_result(
        stage="planning", category="post_execution_replan_failure",
        summary="执行后 Replan 连接失败", technical_reason="APIConnectionError",
        goal=plan.goal, goal_kind="execution", plan=plan,
        environment_changed="true",
    )

    assert result.failure.plan_id == plan.plan_id
    assert result.failure.completed_steps == ["收编 alpha"]


def test_direction_choice_can_carry_an_actual_scope_change_in_same_turn(tmp_path):
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    workflow = MutationWorkflow(
        planner=object(), binder=object(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=object(), verifier=object(),
    )
    failed = workflow.failure_result(
        stage="binding", category="binding_replan_unresolved",
        summary="计划不完整", technical_reason="worker action missing",
        goal="收编 test", goal_kind="execution",
    )

    result = workflow.handle_control("2 排除 worker，其他角色保持不变")

    assert result.kind == "failure_direction_provided"
    assert result.message == "排除 worker，其他角色保持不变"
    assert result.failure.failure_id == failed.failure.failure_id
    assert result.failure.user_direction == "排除 worker，其他角色保持不变"


def test_direction_question_names_a_real_missing_user_decision(tmp_path):
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    decision = "是否允许停止并重启当前健康的 worker"
    workflow = MutationWorkflow(
        planner=object(), binder=object(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=object(), verifier=object(),
    )
    failed = workflow.failure_result(
        stage="binding", category="user_boundary_missing",
        summary="需要确认变更边界", technical_reason="worker restart policy missing",
        goal="收编 test", goal_kind="execution",
        missing_decisions=[decision],
    )

    result = workflow.handle_control("2")

    assert "需要你决定：%s" % decision in failed.message
    assert decision in failed.failure.options[1].description
    assert result.kind == "clarification"
    assert "需要你决定：%s" % decision in result.message
    assert "没有显示必须由你补充的边界" not in result.message


def test_failure_control_persists_choice_and_never_executes_old_plan(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    class Binder:
        def bind(self, plan, **kwargs):
            from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
            raise ChangeBindingError("project_root consumer missing")

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    executor = FakeExecutor()
    workflow = MutationWorkflow(
        planner=FakePlanner([
            GoalOutcome(status="need_execution", plan=_change_plan()),
            GoalOutcome(status="need_execution", plan=_change_plan()),
        ]),
        binder=Binder(), store=store, executor=executor, verifier=FakeVerifier(),
    )
    failed = _submit(workflow,
        "帮我重启 v4_e2e 平台",
        evidence_bundle=object(), evidence_conclusion=object(),
    )

    details = workflow.handle_control("查看刚才失败的详细原因")

    selected = workflow.handle_control("选择 1")

    cancelled = workflow.handle_control(
        "choose-priv-option %s cancel" % failed.failure.failure_id
    )

    assert failed.kind == "awaiting_user_decision"
    assert details.kind == "failure_details"
    assert "project_root consumer missing" in details.message
    assert selected.kind == "failure_option_selected"
    assert cancelled.kind == "not_found"
    assert store.load_failure(failed.failure.failure_id).selected_option_id == (
        selected.failure.selected_option_id
    )
    assert executor.steps == []


@pytest.mark.parametrize(
    ("goal", "stage", "plan_id", "goal_kind"),
    [
        ("重启全部平台", "planning", "", "execution"),
        ("查看有哪些平台在运行", "verification", "", "health_check"),
        ("部署一个新平台", "binding", "priv-ops-existing", "execution"),
    ],
)
def test_all_failure_kinds_expose_the_same_user_level_controls(
    tmp_path, goal, stage, plan_id, goal_kind,
):
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    workflow = MutationWorkflow(
        planner=object(), binder=object(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=object(), verifier=object(),
    )

    result = workflow.failure_result(
        stage=stage,
        category="contract_unresolved",
        summary="目标尚未完成",
        technical_reason="missing grounded facts",
        goal=goal,
        goal_kind=goal_kind,
        plan_id=plan_id,
    )

    assert [item.action for item in result.failure.options] == [
        "continue_current_goal", "provide_direction", "cancel",
    ]
    assert [item.label for item in result.failure.options] == [
        "继续处理", "调整目标或处理范围", "取消本次操作",
    ]


def test_failure_with_user_decision_recommends_direction_not_blind_replan(tmp_path):
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    workflow = MutationWorkflow(
        planner=object(), binder=object(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=object(), verifier=object(),
    )
    decision = (
        "冲突处理请选择其一：为目标 worker 改用空闲端口后继续，"
        "或者本次跳过该角色"
    )

    result = workflow.failure_result(
        stage="planning", category="port_conflict_user_decision",
        summary="目标端口由另一运行时占用。",
        technical_reason="runtime_conflict",
        goal="收编所有平台", goal_kind="execution",
        missing_decisions=[decision],
    )

    assert [item.action for item in result.failure.options] == [
        "continue_current_goal", "provide_direction", "cancel",
    ]
    assert [item.recommended for item in result.failure.options] == [
        False, True, False,
    ]
    assert "2. 调整目标或处理范围（推荐）" in result.message
    assert decision in result.message


def test_llm_failure_explanation_cannot_replace_deterministic_three_choice_menu(
    tmp_path,
):
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore
    from klonet_agent.ops.privileged.workflow.response import ResponseAgent

    class LLM:
        def complete(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=(
                    "实施计划漏掉了 worker 的恢复动作；当前记录没有要求用户"
                    "补充条件。"
                ),
            ))])

    workflow = MutationWorkflow(
        planner=object(), binder=object(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=object(), verifier=object(), response=ResponseAgent(LLM()),
    )

    result = workflow.failure_result(
        stage="binding", category="binding_replan_unresolved",
        summary="实施计划不完整",
        technical_reason="runtime_recovery_action_missing=worker",
        goal="收编 test", goal_kind="execution",
    )

    assert "实施计划漏掉了 worker 的恢复动作" in result.message
    assert result.message.count("1. 继续处理") == 1
    assert result.message.count("2. 调整目标或处理范围") == 1
    assert result.message.count("3. 取消本次操作") == 1
    assert [item.action for item in result.failure.options] == [
        "continue_current_goal", "provide_direction", "cancel",
    ]


def test_repeated_failure_keeps_the_same_three_user_control_exits(tmp_path):
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    workflow = MutationWorkflow(
        planner=object(), binder=object(), store=store,
        executor=object(), verifier=object(),
    )
    first = workflow.failure_result(
        stage="planning",
        category="planning_evidence_no_progress",
        summary="没有新证据",
        technical_reason="process_detail repeated",
        goal="重启平台",
        goal_kind="execution",
    )

    selected = workflow.handle_control("选择 1")
    second = workflow.failure_result(
        stage="planning",
        category="planning_evidence_no_progress",
        summary="仍然没有新证据",
        technical_reason="process_detail repeated again",
        goal="重启平台",
        goal_kind="execution",
    )

    assert selected.failure.selected_option_id == "continue_current_goal"
    assert [item.action for item in second.failure.options] == [
        "continue_current_goal", "provide_direction", "cancel",
    ]
    assert [item.recommended for item in second.failure.options] == [
        True, False, False,
    ]
    assert "选择 1/2/3”" in second.message


def test_bare_numeric_failure_choice_uses_latest_pending_failure(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    failure = FailureRecord(
        failure_id="failure-bare-choice",
        stage="planning",
        category="planning_evidence_budget_exhausted",
        summary="planning stopped",
        technical_reason="no progress",
        goal="重启全部平台",
        goal_kind="execution",
        options=[RecoveryOption(
            option_id="continue_current_goal",
            label="继续处理",
            description="返回原工作流",
            action="continue_current_goal",
        )],
    )
    store.save_failure(failure)
    workflow = MutationWorkflow(
        planner=FakePlanner([]), binder=FakeBinder(), store=store,
        executor=FakeExecutor(), verifier=FakeVerifier(),
    )

    selected = workflow.handle_control("1")

    assert selected.kind == "failure_option_selected"
    assert selected.failure.failure_id == failure.failure_id
    assert selected.failure.selected_option_id == "continue_current_goal"
    assert store.load_failure(failure.failure_id).selected_option_id == (
        "continue_current_goal"
    )


def test_bare_numeric_choice_cannot_reselect_resolved_failure(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    failure = FailureRecord(
        failure_id="failure-already-selected",
        stage="planning",
        category="planning_evidence_budget_exhausted",
        summary="planning stopped",
        technical_reason="no progress",
        goal="重启全部平台",
        goal_kind="execution",
        options=[RecoveryOption(
            option_id="continue_current_goal", label="继续处理",
            description="返回原工作流", action="continue_current_goal",
        )],
        selected_option_id="continue_current_goal",
    )
    store.save_failure(failure)
    workflow = MutationWorkflow(
        planner=FakePlanner([]), binder=FakeBinder(), store=store,
        executor=FakeExecutor(), verifier=FakeVerifier(),
    )

    result = workflow.handle_control("1")

    assert result is None


def test_pending_failure_gate_blocks_semantic_fallthrough_and_uses_dynamic_options(
    tmp_path,
):
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    options = [
        RecoveryOption("continue_one", "方案一", "继续", "continue_current_goal"),
        RecoveryOption("direction_two", "方案二", "调整", "provide_direction"),
        RecoveryOption("cancel_three", "方案三", "取消", "cancel"),
    ]
    failure = FailureRecord(
        failure_id="failure-dynamic-gate",
        stage="planning",
        category="planning_evidence_no_progress",
        summary="planning stopped",
        technical_reason="no progress",
        goal="重启全部平台",
        goal_kind="execution",
        options=options,
    )
    store.save_failure(failure)
    workflow = MutationWorkflow(
        planner=FakePlanner([]), binder=FakeBinder(), store=store,
        executor=FakeExecutor(), verifier=FakeVerifier(),
    )

    blocked = workflow.handle_control("换一个完全不同的新目标")
    selected = workflow.handle_control("3")

    assert blocked.kind == "awaiting_user_decision"
    assert "暂不接受新的语义操作" in blocked.message
    assert "3. 方案三" in blocked.message
    assert selected.kind == "aborted"
    assert selected.failure.selected_option_id == "cancel_three"


def test_failure_control_resolves_the_persisted_option_label_before_intent_routing(
    tmp_path,
):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    class Binder:
        def bind(self, plan, **kwargs):
            from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
            raise ChangeBindingError("runtime evidence incomplete")

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    workflow = MutationWorkflow(
        planner=FakePlanner([
            GoalOutcome(status="need_execution", plan=_change_plan()),
            GoalOutcome(status="need_execution", plan=_change_plan()),
        ]),
        binder=Binder(), store=store, executor=FakeExecutor(), verifier=FakeVerifier(),
    )
    failed = _submit(
        workflow,
        "把 test 的 master 和 celery 收编进 Screen",
        evidence_bundle=object(), evidence_conclusion=object(),
    )

    selected = workflow.handle_control("继续处理")

    assert failed.failure.options[0].label == "继续处理"
    assert selected.kind == "failure_option_selected"
    assert selected.failure.failure_id == failed.failure.failure_id
    assert selected.failure.selected_option_id == "continue_current_goal"
    assert workflow.handle_control("继续聊聊只读证据是什么") is None


def test_failure_control_accepts_another_persisted_label_without_phrase_branch(
    tmp_path,
):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    class Binder:
        def bind(self, plan, **kwargs):
            from klonet_agent.ops.privileged.workflow.change_binding import ChangeBindingError
            raise ChangeBindingError("runtime evidence incomplete")

    workflow = MutationWorkflow(
        planner=FakePlanner([
            GoalOutcome(status="need_execution", plan=_change_plan()),
            GoalOutcome(status="need_execution", plan=_change_plan()),
        ]),
        binder=Binder(),
        store=ChangePlanStore(tmp_path, user_id="u", project_id="p"),
        executor=FakeExecutor(), verifier=FakeVerifier(),
    )
    _submit(
        workflow,
        "把 test 的 master 和 celery 收编进 Screen",
        evidence_bundle=object(), evidence_conclusion=object(),
    )

    result = workflow.handle_control("选择调整目标或处理范围")

    assert result.kind == "clarification"
    assert "目标范围" in result.message
    assert "下一条回复会直接用于修订原目标" in result.message

    supplied = workflow.handle_control("保留 master 和 worker，排除 web_terminal")

    assert supplied.kind == "failure_direction_provided"
    assert supplied.message == "保留 master 和 worker，排除 web_terminal"
    assert supplied.failure.selected_option_id == "provide_direction"
    assert supplied.failure.user_direction == (
        "保留 master 和 worker，排除 web_terminal"
    )


def test_awaiting_direction_still_answers_failure_reason_without_consuming_it(
    tmp_path,
):
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    failure = FailureRecord(
        failure_id="failure-await-direction",
        stage="binding",
        category="binding_contract_missing",
        summary="计划尚未完整",
        technical_reason="runtime action missing",
        goal="重启 test 平台",
        goal_kind="execution",
        options=[RecoveryOption(
            "provide_direction", "调整目标或处理范围", "补充边界",
            "provide_direction",
        )],
        selected_option_id="provide_direction",
    )
    store.save_failure(failure)
    workflow = MutationWorkflow(
        planner=object(), binder=object(), store=store,
        executor=object(), verifier=object(),
    )

    details = workflow.handle_control("现在是为啥失败啊")
    supplied = workflow.handle_control("不改端口，允许重启 master")

    assert details.kind == "failure_details"
    assert "runtime action missing" in details.message
    assert supplied.kind == "failure_direction_provided"
    persisted = store.load_failure(failure.failure_id)
    assert persisted.selected_option_id == "provide_direction"
    assert persisted.user_direction == "不改端口，允许重启 master"
    assert workflow.handle_control("新目标") is None


def test_plan_status_and_supersession_are_bound_to_explicit_plan_id(tmp_path):
    plan = _change_plan()
    workflow, _planner, _binder, store, _executor, _verifier = _workflow(
        tmp_path, plan,
    )
    plan.status = "awaiting_confirmation"
    store.save(plan)

    rendered = workflow.render_plan_status(plan.plan_id)
    aborted = workflow.abort_plan(plan.plan_id)

    assert plan.plan_id in rendered
    assert "confirm-priv-plan" in rendered
    assert workflow.plan_exists(plan.plan_id) is True
    assert workflow.plan_exists("priv-ops-does-not-exist") is False
    assert aborted is True
    assert store.load(plan.plan_id).status == "aborted"
    assert store.load(plan.plan_id).authorized_hash == ""


def test_completed_plan_cannot_be_retroactively_aborted(tmp_path):
    plan = _change_plan()
    workflow, _planner, _binder, store, _executor, _verifier = _workflow(
        tmp_path, plan,
    )
    plan.status = "completed"
    store.save(plan)

    assert workflow.abort_plan(plan.plan_id) is False
    assert store.load(plan.plan_id).status == "completed"
