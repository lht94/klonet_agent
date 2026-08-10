from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_binding_context_preserves_exact_ops_source_before_large_runtime_evidence():
    from klonet_agent.ops.privileged.v4.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

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

    context = V4MutationWorkflow._binding_context(bundle)

    assert context is not None
    assert exact_source in context.environment_evidence
    assert context.environment_evidence.index("probe=ops_file") < context.environment_evidence.index(
        "probe=running_platforms"
    )


def test_v4_confirmation_redacts_registered_action_credentials():
    from klonet_agent.ops.privileged.contracts import ExecutionBinding
    from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4, ChangeStepV4
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    step = ChangeStepV4(
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
    plan = ChangePlanV4.new(goal="deploy", risk="high", steps=[step])

    message = V4MutationWorkflow._confirmation_message(plan)

    assert "local-secret" not in message
    assert "[REDACTED]" in message
    assert "V4 变更计划" in message
    assert "目标：" in message
    assert "风险：" in message
    assert "冻结资源：" not in message
    assert "变更步骤：" in message
    assert "请使用以下命令确认这份精确计划" in message


def test_reload_nginx_verification_accepts_non_systemd_master_process():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

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

    verification = V4MutationWorkflow._verification_step(step)

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
    from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4, ChangeStepV4

    change = ChangeStepV4(
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
        action="service_control",
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
                    execution_binding=binding,
                    postconditions=[{"checker": "exit_code_zero"}],
                )
            ],
        )
    else:
        change.execution_binding = binding
    return ChangePlanV4(
        plan_id="priv-v4-flow",
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


class RaisingVerifier:
    def verify_step(self, plan, step):
        raise RuntimeError("checker crashed")


def _workflow(tmp_path, plan, *, verifier_status="passed"):
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    planner = FakePlanner([V4PlanningOutcome(status="ready", plan=plan)])
    binder = FakeBinder()
    executor = FakeExecutor()
    verifier = FakeVerifier(verifier_status)
    store = MemoryStore()
    workflow = V4MutationWorkflow(
        planner=planner,
        binder=binder,
        store=store,
        executor=executor,
        verifier=verifier,
    )
    return workflow, planner, binder, store, executor, verifier


def test_submit_binds_and_persists_but_never_executes_before_confirmation(tmp_path):
    workflow, planner, binder, store, executor, _ = _workflow(tmp_path, _change_plan())

    result = workflow.submit("deploy", evidence_bundle=object(), evidence_conclusion=object())

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == binder.calls == 1
    assert executor.steps == []
    assert store.load(result.plan.plan_id).status == "awaiting_confirmation"
    assert "confirm-priv-v4 %s %s" % (
        result.plan.plan_id,
        result.plan.content_hash,
    ) in result.message
    assert "deploy isolated instance" in result.message
    assert "已注册动作 service_control" in result.message
    assert "exit_code_zero" in result.message


def test_submit_passes_collected_discovery_evidence_to_binding(tmp_path):
    from klonet_agent.ops.privileged.v4.contracts import (
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

    workflow.submit("deploy", evidence_bundle=bundle, evidence_conclusion=object())

    context = binder.kwargs[0]["grounded_context"]
    assert "inspect_docker_images" in context.environment_evidence


def test_confirmation_rejects_stale_hash_without_execution(tmp_path):
    workflow, _, _, _, executor, _ = _workflow(tmp_path, _change_plan())
    submitted = workflow.submit("deploy", evidence_bundle=object(), evidence_conclusion=object())

    result = workflow.confirm(submitted.plan.plan_id, "stale")

    assert result.kind == "confirmation_rejected"
    assert executor.steps == []
    assert submitted.plan.is_authorized is False


@pytest.mark.parametrize("hierarchical", [False, True])
def test_exact_confirmation_executes_then_verifies_and_completes(tmp_path, hierarchical):
    workflow, _, _, store, executor, verifier = _workflow(
        tmp_path, _change_plan(hierarchical=hierarchical)
    )
    submitted = workflow.submit("deploy", evidence_bundle=object(), evidence_conclusion=object())

    result = workflow.confirm(submitted.plan.plan_id, submitted.plan.content_hash)

    assert result.kind == "completed"
    assert executor.steps == (["deploy-1"] if hierarchical else ["deploy"])
    assert verifier.steps == (
        ["deploy-1", "deploy"] if hierarchical else ["deploy"]
    )
    assert store.load(submitted.plan.plan_id).status == "completed"
    if not hierarchical:
        persisted = store.load(submitted.plan.plan_id).steps[0]
        assert persisted.execution_attempts == 1
        assert persisted.evidence.return_code == 0


def test_failed_verification_pauses_without_retrying_execution(tmp_path):
    workflow, _, _, store, executor, _ = _workflow(
        tmp_path, _change_plan(), verifier_status="failed"
    )
    submitted = workflow.submit("deploy", evidence_bundle=object(), evidence_conclusion=object())

    result = workflow.confirm(submitted.plan.plan_id, submitted.plan.content_hash)

    assert result.kind == "paused"
    assert executor.steps == ["deploy"]
    assert store.load(submitted.plan.plan_id).steps[0].status == "paused"


def test_exact_reconfirmation_recovers_current_state_without_reexecuting(tmp_path):
    workflow, _, _, store, executor, verifier = _workflow(
        tmp_path, _change_plan(hierarchical=True), verifier_status="failed"
    )
    submitted = workflow.submit(
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )
    plan_id = submitted.plan.plan_id
    content_hash = submitted.plan.content_hash
    paused = workflow.confirm(plan_id, content_hash)
    assert paused.kind == "paused"
    assert executor.steps == ["deploy-1"]

    verifier.status = "passed"
    resumed = workflow.confirm(plan_id, content_hash)

    assert resumed.kind == "completed"
    assert executor.steps == ["deploy-1"]
    assert "recovered:deploy-1" in verifier.steps
    assert store.load(plan_id).status == "completed"


def test_exact_reconfirmation_retries_only_conclusive_no_change_failure(tmp_path):
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

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

        def verify_recovered_step(self, plan, step):
            return SimpleNamespace(status="failed", reason="target still absent")

    plan = _change_plan(hierarchical=True)
    store = MemoryStore()
    executor = SequenceExecutor()
    workflow = V4MutationWorkflow(
        planner=FakePlanner([V4PlanningOutcome(status="ready", plan=plan)]),
        binder=FakeBinder(),
        store=store,
        executor=executor,
        verifier=SequenceVerifier(),
    )
    submitted = workflow.submit(
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )
    plan_id = submitted.plan.plan_id
    content_hash = submitted.plan.content_hash

    assert workflow.confirm(plan_id, content_hash).kind == "paused"
    resumed = workflow.confirm(plan_id, content_hash)

    assert resumed.kind == "completed"
    assert executor.calls == 2


def test_exact_reconfirmation_can_retry_exited_screen_with_no_listener():
    from klonet_agent.ops.privileged.contracts import (
        CheckResult,
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    step = PrivilegedStep(
        step_id="master",
        title="start master",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="start_screen_component",
            args={"screen_session": "v4e2e_m", "component": "master"},
            risk="medium",
        ),
        evidence=ExecutionEvidence(return_code=0, environment_changed=True),
        checks=[
            CheckResult("screen_session_exists", "failed"),
            CheckResult("port_listening", "failed"),
        ],
    )

    assert V4MutationWorkflow._can_retry_conclusive_no_change(step) is True


def test_exact_reconfirmation_can_retry_nginx_when_destination_is_absent():
    from klonet_agent.ops.privileged.contracts import (
        CheckResult,
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    step = PrivilegedStep(
        step_id="nginx",
        title="install nginx site",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="install_nginx_config",
            args={"config_name": "klonet-v4-e2e"},
            risk="medium",
        ),
        evidence=ExecutionEvidence(return_code=1, environment_changed=True),
        checks=[CheckResult("file_exists", "failed")],
    )

    assert V4MutationWorkflow._can_retry_conclusive_no_change(step) is True


def test_semantic_config_verification_is_composed_from_atomic_bindings():
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

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
        "path": "/srv/v4/vemu_config/config.py",
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
                "cwd": "/srv/v4",
            },
        }
    ]

    semantic = V4MutationWorkflow._semantic_verification_step(change)

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
                "cwd": "/srv/v4",
            },
        },
        {
            "checker": "file_contains",
            "args": {
                "path": "/srv/v4/vemu_config/config.py",
                "text": "PROJ_CONFIG = WtxConfig()",
            },
        },
    ]


def test_control_command_requires_exact_v4_syntax(tmp_path):
    workflow, _, _, _, executor, _ = _workflow(tmp_path, _change_plan())
    submitted = workflow.submit("deploy", evidence_bundle=object(), evidence_conclusion=object())

    ignored = workflow.handle_control(
        "please confirm-priv-v4 %s %s" % (
            submitted.plan.plan_id,
            submitted.plan.content_hash,
        )
    )

    assert ignored is None
    assert executor.steps == []


def test_planner_evidence_gap_returns_to_discovery_then_replans(tmp_path):
    from klonet_agent.ops.privileged.v4.contracts import ProbeRequest
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    bundle = SimpleNamespace(records=[])
    gap = V4PlanningOutcome(
        status="need_evidence",
        probe_requests=[ProbeRequest("ports", {"ports": [47001]}, "freeze port")],
    )
    ready = V4PlanningOutcome(status="ready", plan=_change_plan())
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
    workflow = V4MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=discovery,
        synthesis=synthesis,
        max_replanning_rounds=2,
    )

    result = workflow.submit(
        "deploy", evidence_bundle=bundle, evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == 2
    assert discovery.calls == synthesis.calls == 1


def test_verified_candidate_plan_is_finalized_without_model_reselection(tmp_path):
    from klonet_agent.ops.privileged.v4.contracts import ProbeRequest
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    candidate = _change_plan()

    class CandidatePlanner:
        def __init__(self):
            self.calls = 0
            self.finalize_calls = 0

        def plan(self, *args, **kwargs):
            self.calls += 1
            return V4PlanningOutcome(
                status="need_evidence",
                candidate_plan=candidate,
                probe_requests=[
                    ProbeRequest("ports", {"ports": [47001]}, "verify port")
                ],
            )

        def finalize_candidate(self, plan, bundle):
            self.finalize_calls += 1
            assert plan is candidate
            return V4PlanningOutcome(status="ready", plan=plan)

    planner = CandidatePlanner()
    discovery = SimpleNamespace(
        collect_requests=lambda requests, evidence_bundle: evidence_bundle
    )
    synthesis = SimpleNamespace(
        synthesize=lambda goal, evidence_bundle: SimpleNamespace()
    )
    workflow = V4MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=discovery,
        synthesis=synthesis,
    )

    result = workflow.submit(
        "deploy", evidence_bundle=SimpleNamespace(), evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == planner.finalize_calls == 1


def test_occupied_candidate_ports_trigger_one_bounded_replan(tmp_path):
    from klonet_agent.ops.privileged.v4.contracts import ProbeRequest
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    candidate = _change_plan()
    replacement = _change_plan()

    class CandidatePlanner:
        def __init__(self):
            self.calls = []
            self.finalize_calls = 0

        def plan(self, *args, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return V4PlanningOutcome(
                    status="need_evidence",
                    candidate_plan=candidate,
                    probe_requests=[
                        ProbeRequest("ports", {"ports": [6379]}, "verify port")
                    ],
                )
            return V4PlanningOutcome(status="ready", plan=replacement)

        def finalize_candidate(self, plan, bundle):
            self.finalize_calls += 1
            return V4PlanningOutcome(
                status="blocked",
                candidate_plan=plan,
                reason="candidate ports became occupied: 6379",
            )

    planner = CandidatePlanner()
    workflow = V4MutationWorkflow(
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

    result = workflow.submit(
        "deploy", evidence_bundle=SimpleNamespace(), evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_confirmation"
    assert len(planner.calls) == 2
    assert planner.finalize_calls == 1
    assert "candidate ports became occupied: 6379" in planner.calls[1]["binding_feedback"]


def test_planner_discovery_loop_stops_at_explicit_budget(tmp_path):
    from klonet_agent.ops.privileged.v4.contracts import ProbeRequest
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    gap = V4PlanningOutcome(
        status="need_evidence",
        probe_requests=[ProbeRequest("ports", {"ports": [47001]}, "freeze port")],
    )
    planner = FakePlanner([gap, gap, gap, gap, gap])
    discovery = SimpleNamespace(
        collect_requests=lambda requests, evidence_bundle: evidence_bundle
    )
    synthesis = SimpleNamespace(
        synthesize=lambda goal, evidence_bundle: SimpleNamespace()
    )
    workflow = V4MutationWorkflow(
        planner=planner,
        binder=FakeBinder(),
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
        discovery=discovery,
        synthesis=synthesis,
        max_replanning_rounds=4,
    )

    result = workflow.submit(
        "deploy", evidence_bundle=SimpleNamespace(), evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "blocked"
    assert "budget" in result.message
    assert planner.calls == 5


def test_default_discovery_budget_allows_four_rounds_then_ready(tmp_path):
    from klonet_agent.ops.privileged.v4.contracts import ProbeRequest
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    gap = V4PlanningOutcome(
        status="need_evidence",
        probe_requests=[ProbeRequest("ports", {"ports": [47001]}, "freeze port")],
    )
    planner = FakePlanner([gap, gap, gap, gap, V4PlanningOutcome(
        status="ready", plan=_change_plan()
    )])
    workflow = V4MutationWorkflow(
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

    result = workflow.submit(
        "deploy", evidence_bundle=SimpleNamespace(), evidence_conclusion=SimpleNamespace()
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == 5


def test_verifier_exception_is_persisted_as_pause_after_single_execution(tmp_path):
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    executor = FakeExecutor()
    store = MemoryStore()
    workflow = V4MutationWorkflow(
        planner=FakePlanner([V4PlanningOutcome(status="ready", plan=_change_plan())]),
        binder=FakeBinder(),
        store=store,
        executor=executor,
        verifier=RaisingVerifier(),
    )
    submitted = workflow.submit(
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )

    result = workflow.confirm(submitted.plan.plan_id, submitted.plan.content_hash)

    assert result.kind == "paused"
    assert "checker crashed" in result.message
    assert executor.steps == ["deploy"]


def test_binder_failure_replans_at_most_once_then_succeeds(tmp_path):
    from klonet_agent.ops.privileged.v4.binding import V4BindingError
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    class Binder:
        calls = 0

        def bind(self, plan, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise V4BindingError("capability unavailable")
            plan.status = "awaiting_confirmation"
            return plan

    planner = FakePlanner(
        [
            V4PlanningOutcome(status="ready", plan=_change_plan()),
            V4PlanningOutcome(status="ready", plan=_change_plan()),
        ]
    )
    binder = Binder()
    workflow = V4MutationWorkflow(
        planner=planner,
        binder=binder,
        store=MemoryStore(),
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
    )

    result = workflow.submit(
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )

    assert result.kind == "awaiting_confirmation"
    assert planner.calls == binder.calls == 2


def test_second_binder_failure_is_persisted_as_blocked_without_traceback(tmp_path):
    from klonet_agent.ops.privileged.v4.binding import V4BindingError
    from klonet_agent.ops.privileged.v4.planner import V4PlanningOutcome
    from klonet_agent.ops.privileged.v4.workflow import V4MutationWorkflow

    class Binder:
        calls = 0

        def bind(self, plan, **kwargs):
            self.calls += 1
            raise V4BindingError("clone target could not be grounded")

    store = MemoryStore()
    workflow = V4MutationWorkflow(
        planner=FakePlanner(
            [
                V4PlanningOutcome(status="ready", plan=_change_plan()),
                V4PlanningOutcome(status="ready", plan=_change_plan()),
            ]
        ),
        binder=Binder(),
        store=store,
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
    )

    result = workflow.submit(
        "deploy", evidence_bundle=object(), evidence_conclusion=object()
    )

    assert result.kind == "blocked"
    assert result.plan.status == "blocked"
    assert store.load(result.plan.plan_id).status == "blocked"
