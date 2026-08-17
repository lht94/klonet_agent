from __future__ import annotations

from types import SimpleNamespace


class StubClassifier:
    def __init__(
        self, intent, command="", goal_clarity="clear", goal_relation="new",
        goal_kind="", operation="none", scope="none", components=(),
    ):
        self.decision = SimpleNamespace(
            intent=intent,
            command=command,
            goal_clarity=goal_clarity,
            should_clarify=False,
            clarification_question="",
            reason="test",
            goal_relation=goal_relation,
            goal_kind=goal_kind,
            operation=operation,
            scope=scope,
            components=components,
        )
        self.calls = []

    def classify(self, text, conversation_context=""):
        self.calls.append((text, conversation_context))
        return self.decision


class StubDiscovery:
    def __init__(self, bundle):
        self.bundle = bundle
        self.calls = []

    def collect(self, goal, *, command="", conversation_context=""):
        self.calls.append((goal, command, conversation_context))
        return self.bundle


class StubSynthesis:
    def __init__(self, conclusion):
        self.conclusion = conclusion
        self.calls = []

    def synthesize(self, goal, bundle):
        self.calls.append((goal, bundle))
        return self.conclusion


class StubResponse:
    def __init__(self):
        self.calls = []

    def render_readonly(self, goal, conclusion):
        self.calls.append((goal, conclusion))
        return "发现 3 个候选平台。"


class StubGoalVerifier:
    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = []

    def verify_goal(self, goal, bundle, conclusion, attempted_keys=None):
        from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

        self.calls.append((goal, bundle, conclusion, attempted_keys))
        return self.outcome or GoalOutcome("achieved")

    def verify_pre_execution(self, goal, bundle, *, operation, scope):
        from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

        return PrivilegedVerifierAgent.verify_pre_execution(
            goal, bundle, operation=operation, scope=scope,
        )


class NoMutationWorkflow:
    def __init__(self):
        self.calls = []

    def plan_once(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("readonly request must not enter mutation workflow")


class RecordingMutationWorkflow:
    def __init__(self):
        self.calls = []

    def plan_once(self, *args, **kwargs):
        from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

        self.calls.append((args, kwargs))
        return GoalOutcome(
            "need_execution", plan=SimpleNamespace(status="draft"),
        )

    def bind_once(self, plan, *, evidence_bundle):
        from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

        return GoalOutcome(
            "needs_user_decision", user_question="plan", plan=plan,
        )


def _evidence():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceClaim,
        EvidenceConclusion,
        EvidenceRecord,
        ProbeRequest,
    )

    bundle = EvidenceBundle(goal="检查平台")
    record = bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("platform_instances", {}, "discover platforms"),
            "platform=vemu_uestc",
        )
    )
    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim("发现平台实例", [record.evidence_id])]
    )
    return bundle, conclusion


def test_readonly_without_command_uses_discovery_synthesis_and_response_only():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle, conclusion = _evidence()
    classifier = StubClassifier("readonly_action")
    discovery = StubDiscovery(bundle)
    synthesis = StubSynthesis(conclusion)
    response = StubResponse()
    mutation = NoMutationWorkflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=classifier,
        discovery=discovery,
        synthesis=synthesis,
        response=response,
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("检查下现在服务器上有哪些平台")

    assert result.handled is True
    assert result.kind == "completed"
    assert result.outcome.status == "achieved"
    assert result.message == "发现 3 个候选平台。"
    assert discovery.calls == [("检查下现在服务器上有哪些平台", "", "")]
    assert synthesis.calls == [("检查下现在服务器上有哪些平台", bundle)]
    assert response.calls == [("检查下现在服务器上有哪些平台", conclusion)]
    assert mutation.calls == []


def test_completed_plan_status_followup_reads_receipt_without_discovery_or_verifier():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    bundle, conclusion = _evidence()

    class ContextStore:
        def load(self):
            return OperationalContextSnapshot(
                resolved_goal="帮我重启 v4e2e 平台",
                phase="completed",
                evidence=bundle,
            )

    class StatusWorkflow(NoMutationWorkflow):
        def render_latest_status(self):
            return "计划已完成：master 与 worker 均通过健康检查。"

    class ForbiddenVerifier(StubGoalVerifier):
        def verify_goal(self, *args, **kwargs):
            raise AssertionError("status query must not enter Verifier")

    discovery = StubDiscovery(bundle)
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation", goal_kind="status_query"),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=StatusWorkflow(),
        verifier=ForbiddenVerifier(),
        context_store=ContextStore(),
    )

    result = coordinator.handle("啥意思，你已经执行完了吗")

    assert result.kind == "plan_status"
    assert result.message == "计划已完成：master 与 worker 均通过健康检查。"
    assert discovery.calls == []


def test_readonly_diagnosis_automatically_collects_discoverable_gaps_until_achieved():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceClaim, EvidenceConclusion, EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

    bundle = EvidenceBundle(goal="检查 v4e2e 报错")
    first = bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("screen_session", {"session": "v4e2e_m"}, "traceback"),
        "Traceback truncated before the underlying exception",
    ))
    incomplete = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim("启动阶段出现截断堆栈", [first.evidence_id])],
        uncertainties=[EvidenceClaim("底层异常类型未知", [first.evidence_id])],
    )

    class LoopDiscovery(StubDiscovery):
        def __init__(self, value):
            super().__init__(value)
            self.followups = []

        def collect_requests(self, requests, target):
            self.followups.append(requests)
            target.add(EvidenceRecord.from_probe(
                requests[0],
                "PermissionError: /srv/app/logs/master.log is not writable",
            ))
            return target

    class LoopSynthesis:
        def __init__(self):
            self.calls = 0

        def synthesize(self, goal, target):
            self.calls += 1
            if self.calls == 1:
                return incomplete
            newest = target.records[-1]
            return EvidenceConclusion(confirmed_facts=[EvidenceClaim(
                "根因是 master 日志文件不可写导致日志处理器初始化失败",
                [newest.evidence_id],
            )])

    class GoalVerifier:
        def __init__(self):
            self.calls = 0

        def verify_goal(self, goal, target, conclusion):
            self.calls += 1
            if self.calls == 1:
                return GoalOutcome(
                    "need_evidence",
                    evidence_requests=[ProbeRequest(
                        "logs", {"path": "/srv/app/logs/master.log"},
                        "获取完整底层异常",
                    )],
                )
            return GoalOutcome("achieved")

    discovery = LoopDiscovery(bundle)
    synthesis = LoopSynthesis()
    verifier = GoalVerifier()
    response = StubResponse()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("readonly_action"),
        discovery=discovery,
        synthesis=synthesis,
        response=response,
        mutation_workflow=NoMutationWorkflow(),
        verifier=verifier,
    )

    result = coordinator.handle("检查 v4e2e 为什么报错")

    assert result.kind == "completed"
    assert len(discovery.followups) == 1
    assert synthesis.calls == 2
    assert verifier.calls == 2
    assert len(response.calls) == 1
    assert "根因" in response.calls[0][1].confirmed_facts[0].text


def test_diagnostic_loop_pauses_only_for_a_real_user_decision():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

    bundle, conclusion = _evidence()

    class GoalVerifier:
        def verify_goal(self, goal, target, synthesized):
            return GoalOutcome(
                "needs_user_decision",
                user_question="检测到两个同名实例，请指定项目根目录。",
            )

    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("readonly_action"),
        discovery=StubDiscovery(bundle),
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=GoalVerifier(),
    )

    result = coordinator.handle("检查报错")

    assert result.kind == "clarification"
    assert "项目根目录" in result.message


def test_readonly_command_is_collected_as_evidence_not_executed_as_plan():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle, conclusion = _evidence()
    discovery = StubDiscovery(bundle)
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("readonly_action", command="python3 -V"),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("查看 Python 版本")

    assert result.kind == "completed"
    assert discovery.calls == [("查看 Python 版本", "python3 -V", "")]


def test_conversation_bypasses_privileged_discovery_and_execution():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle, conclusion = _evidence()
    discovery = StubDiscovery(bundle)
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("什么是 Klonet？")

    assert result.handled is False
    assert result.kind == "conversation"
    assert discovery.calls == []


def test_workflow_coordinator_handles_exact_control_before_classifier_or_discovery():
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator,
        WorkflowResult,
    )

    class NoCall:
        def __getattr__(self, name):
            raise AssertionError("component must not be called: %s" % name)

    class Controls:
        def __init__(self):
            self.calls = []

        def handle_control(self, text):
            self.calls.append(text)
            return WorkflowResult(True, "completed", "confirmed")

    controls = Controls()
    coordinator = PrivilegedOpsCoordinator(
        classifier=NoCall(),
        discovery=NoCall(),
        synthesis=NoCall(),
        response=NoCall(),
        mutation_workflow=controls,
        verifier=NoCall(),
    )

    result = coordinator.handle_with_context(
        "confirm-priv-plan priv-ops-flow " + "a" * 64,
        environment_context="ignored",
        conversation_context="recent",
    )

    assert result.kind == "completed"
    assert controls.calls == ["confirm-priv-plan priv-ops-flow " + "a" * 64]


def test_paused_confirm_result_automatically_diagnoses_and_replans():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

    failed_plan = SimpleNamespace(
        plan_id="priv-ops-failed",
        goal="修复 v4e2e master",
        status="paused",
        steps=[],
    )

    class Workflow:
        def __init__(self):
            self.submits = []

        def handle_control(self, text):
            return WorkflowResult(
                True, "paused", "master health check failed", plan=failed_plan,
            )

        def plan_once(self, goal, **kwargs):
            from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

            self.submits.append((goal, kwargs))
            return GoalOutcome(
                "need_execution", plan=SimpleNamespace(status="draft"),
            )

        def bind_once(self, plan, *, evidence_bundle):
            return GoalOutcome(
                "needs_user_decision", user_question="recovery plan", plan=plan,
            )

    class Discovery:
        def collect(
            self, goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            self.goal = goal
            self.seed = seed_bundle
            return seed_bundle or EvidenceBundle(goal=goal)

        def collect_requests(self, requests, bundle):
            raise AssertionError("diagnosis is already complete")

    class GoalVerifier:
        def verify_goal(
            self, goal, bundle, conclusion, attempted_keys=None,
            phase="readonly",
        ):
            assert phase == "post_execution"
            return GoalOutcome("need_replan")

    workflow = Workflow()
    discovery = Discovery()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=workflow,
        verifier=GoalVerifier(),
    )

    result = coordinator.handle(
        "confirm-priv-plan priv-ops-failed " + "a" * 64,
    )

    assert result.kind == "awaiting_confirmation"
    assert workflow.submits[0][0] == "修复 v4e2e master"
    evidence = workflow.submits[0][1]["evidence_bundle"]
    assert evidence.records[0].request.probe == "plan_execution"
    assert "master health check failed" in evidence.records[0].output
    assert "自动诊断" in discovery.goal


def test_workflow_coordinator_applies_goal_guard_before_any_discovery():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    class NoCall:
        def __getattr__(self, name):
            raise AssertionError("component must not be called: %s" % name)

    coordinator = PrivilegedOpsCoordinator(
        classifier=NoCall(),
        discovery=NoCall(),
        synthesis=NoCall(),
        response=NoCall(),
        mutation_workflow=NoCall(),
        verifier=NoCall(),
    )

    result = coordinator.handle("rm -rf / and delete all system files")

    assert result.kind == "denied"
    assert result.handled is True


def test_mutation_clarifies_multiple_abnormal_runtime_roots_before_planning():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceConclusion,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle = EvidenceBundle(goal="repair")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("running_platforms", {}, "runtime inventory"),
            "\n".join(
                [
                    "inspect_running_platforms",
                    "abnormal_count=3",
                    "platform=vemu_uestc project_root=/home/lzl/vemu_uestc backend_status=abnormal missing_roles=worker",
                    "platform=vemu_uestc project_root=/home/lzl/test/vemu_uestc backend_status=abnormal missing_roles=master",
                    "platform=klonet project_root=/home/lzl/xxy/klonet backend_status=abnormal missing_roles=master,worker",
                ]
            ),
        )
    )
    mutation = RecordingMutationWorkflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("mutating_action", operation="repair"),
        discovery=StubDiscovery(bundle),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("帮我修复后端不正常运行的平台")

    assert result.kind == "clarification"
    assert "/home/lzl/vemu_uestc" in result.message
    assert "/home/lzl/test/vemu_uestc" in result.message
    assert "/home/lzl/xxy/klonet" in result.message
    assert "项目根目录" in result.message
    assert mutation.calls == []


def test_mutation_with_explicit_abnormal_roots_enters_planning():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceConclusion,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle = EvidenceBundle(goal="repair")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("running_platforms", {}, "runtime inventory"),
            "platform=vemu_uestc project_root=/home/lzl/vemu_uestc backend_status=abnormal\n"
            "platform=vemu_uestc project_root=/home/lzl/test/vemu_uestc backend_status=abnormal",
        )
    )
    mutation = RecordingMutationWorkflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("mutating_action", operation="repair"),
        discovery=StubDiscovery(bundle),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle(
        "修复 /home/lzl/vemu_uestc 和 /home/lzl/test/vemu_uestc，作为两个独立平台"
    )

    assert result.kind == "awaiting_confirmation"
    assert len(mutation.calls) == 1


def test_mutation_with_explicit_roots_already_healthy_completes_without_plan():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceConclusion,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle = EvidenceBundle(goal="repair")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("running_platforms", {}, "runtime inventory"),
            "\n".join(
                [
                    "platform=vemu_uestc project_root=/home/lzl/vemu_uestc "
                    "backend_status=healthy master_port=45551 master_endpoint=success "
                    "worker_port=45552 worker_endpoint=success",
                    "platform=vemu_uestc_test project_root=/home/lzl/test/vemu_uestc "
                    "backend_status=healthy master_port=45554 master_endpoint=success "
                    "worker_port=45555 worker_endpoint=success",
                    "platform=klonet project_root=/home/lzl/xxy/klonet "
                    "backend_status=abnormal master_port=46551 master_endpoint=failed "
                    "worker_port=46552 worker_endpoint=failed",
                ]
            ),
        )
    )
    mutation = NoMutationWorkflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("mutating_action", operation="repair"),
        discovery=StubDiscovery(bundle),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle(
        "修复 /home/lzl/vemu_uestc 和 /home/lzl/test/vemu_uestc，"
        "两个都要独立正常运行，不能共用端口"
    )

    assert result.kind == "completed"
    assert "无需变更" in result.message
    assert "/home/lzl/vemu_uestc" in result.message
    assert "master_port=45551" in result.message
    assert "/home/lzl/test/vemu_uestc" in result.message
    assert "worker_port=45555" in result.message
    assert "/home/lzl/xxy/klonet" not in result.message
    assert mutation.calls == []


def test_named_platform_alias_selects_one_abnormal_root_without_clarification():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceConclusion,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle = EvidenceBundle(goal="repair 102")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime inventory"),
        "platform=102 project_root=/home/klonet-agent/102 backend_status=abnormal\n"
        "platform=klonet project_root=/home/lzl/xxy/klonet backend_status=abnormal",
    ))
    mutation = RecordingMutationWorkflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("mutating_action", operation="repair"),
        discovery=StubDiscovery(bundle),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("修复 102 平台，其他实例不要修改")

    assert result.kind == "awaiting_confirmation"
    assert len(mutation.calls) == 1


def test_startup_traceback_collects_target_source_before_synthesis():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceConclusion,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle = EvidenceBundle(goal="repair")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("logs", {"path": "/home/klonet-agent/102/logs/master.log"}, "startup log"),
        'Traceback\n  File "/home/klonet-agent/102/mains/master_main.py", line 10, in <module>\n'
        '  File "/usr/lib/python3.8/importlib/__init__.py", line 1, in import_module\n'
        "RuntimeError: boot failed",
    ))

    class SourceDiscovery(StubDiscovery):
        def __init__(self, evidence):
            super().__init__(evidence)
            self.incremental = []

        def collect(self, goal, *, command="", conversation_context=""):
            self.bundle.goal = goal
            return super().collect(
                goal,
                command=command,
                conversation_context=conversation_context,
            )

        def collect_requests(self, requests, evidence):
            self.incremental.extend(requests)
            for request in requests:
                evidence.add(EvidenceRecord.from_probe(
                    request,
                    "read_ops_file\nKLONET_E2E_INJECTED_BOOT_FAILURE()",
                ))
                return evidence

        def collect_traceback_source_evidence(self, evidence):
            from klonet_agent.ops.privileged.workflow.discovery import (
                _traceback_source_requests,
            )

            return self.collect_requests(_traceback_source_requests(evidence), evidence)

    discovery = SourceDiscovery(bundle)
    synthesis = StubSynthesis(EvidenceConclusion())
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("mutating_action"),
        discovery=discovery,
        synthesis=synthesis,
        response=StubResponse(),
        mutation_workflow=RecordingMutationWorkflow(),
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("只修复 /home/klonet-agent/102 的启动异常")

    assert result.kind == "awaiting_confirmation"
    assert len(discovery.incremental) == 1
    request = discovery.incremental[0]
    assert request.probe == "ops_file"
    assert request.args["path"] == "/home/klonet-agent/102/mains/master_main.py"
    assert all(
        record.request.args.get("path") != "/usr/lib/python3.8/importlib/__init__.py"
        for record in bundle.records
    )
    assert any(record.request.probe == "ops_file" for record in synthesis.calls[0][1].records)


def test_named_platform_alias_scopes_traceback_source_collection():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceConclusion,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle = EvidenceBundle(goal="repair 102")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        "platform=102 project_root=/home/klonet-agent/102 backend_status=abnormal\n"
        "platform=other project_root=/srv/other backend_status=abnormal",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("logs", {"path": "/home/klonet-agent/102/logs/master.log"}, "log"),
        'File "/home/klonet-agent/102/mains/master_main.py", line 10, in <module>\n'
        "RuntimeError: boot failed",
    ))

    class SourceDiscovery(StubDiscovery):
        def __init__(self, evidence):
            super().__init__(evidence)
            self.incremental = []

        def collect_requests(self, requests, evidence):
            self.incremental.extend(requests)
            for request in requests:
                evidence.add(EvidenceRecord.from_probe(request, "read_ops_file\nsource"))
            return evidence

        def collect_traceback_source_evidence(self, evidence):
            from klonet_agent.ops.privileged.workflow.discovery import (
                _traceback_source_requests,
            )

            return self.collect_requests(_traceback_source_requests(evidence), evidence)

    discovery = SourceDiscovery(bundle)
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("mutating_action"),
        discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=RecordingMutationWorkflow(),
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("修复 102 平台启动异常，其他实例不要修改")

    assert result.kind == "awaiting_confirmation"
    assert [request.args["path"] for request in discovery.incremental] == [
        "/home/klonet-agent/102/mains/master_main.py"
    ]


def test_submit_previous_restart_goal_reuses_static_evidence_and_refreshes_runtime():
    from klonet_agent.ops.privileged.workflow.operational_context import OperationalContextSnapshot
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    previous = EvidenceBundle(goal="restart")
    previous.add(EvidenceRecord.from_probe(
        ProbeRequest("ops_file", {"path": "/srv/app/mains/gun.py"}, "startup"),
        "bind=47001",
    ))
    previous.add(EvidenceRecord.from_probe(
        ProbeRequest("process", {"keywords": ["v4e2e"]}, "runtime"),
        "pid=stale",
    ))
    snapshot = OperationalContextSnapshot(
        resolved_goal="帮我重启 v4e2e 的 master 和 worker",
        target_roots=["/srv/app"],
        phase="draft_ready",
        evidence=previous,
    )

    class ContextStore:
        def __init__(self):
            self.saved = []

        def load(self):
            return snapshot

        def save(self, value):
            self.saved.append(value)

    class SeedDiscovery:
        def __init__(self):
            self.calls = []

        def collect(self, goal, *, command="", conversation_context="", seed_bundle=None):
            self.calls.append((goal, seed_bundle))
            assert [item.request.probe for item in seed_bundle.records] == ["ops_file"]
            seed_bundle.add(EvidenceRecord.from_probe(
                ProbeRequest("running_platforms", {}, "refresh runtime"),
                "platform=v4e2e project_root=/srv/app backend_status=abnormal "
                "master_port=47001 master_endpoint=not_checked reason=role_not_running "
                "worker_port=47002 worker_endpoint=not_checked reason=role_not_running",
            ))
            return seed_bundle

    discovery = SeedDiscovery()
    mutation = RecordingMutationWorkflow()
    store = ContextStore()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("resume_plan"),
        discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
        context_store=store,
    )

    result = coordinator.handle("提交这个重启计划走审批流程")

    assert result.kind == "awaiting_confirmation"
    assert discovery.calls[0][0] == "帮我重启 v4e2e 的 master 和 worker"
    assert mutation.calls[0][0][0] == "帮我重启 v4e2e 的 master 和 worker"
    assert store.saved[-1].phase == "awaiting_confirmation"


def test_self_directed_followup_reuses_previous_diagnostic_goal():
    from klonet_agent.ops.privileged.workflow.operational_context import OperationalContextSnapshot
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    snapshot = OperationalContextSnapshot(
        resolved_goal="检查 v4e2e_m 为什么报错",
        target_roots=["/home/lzl/klonet_workflow_e2e"],
        phase="diagnosing",
        evidence=EvidenceBundle(goal="检查 v4e2e_m 为什么报错"),
    )

    class Store:
        def load(self):
            return snapshot

        def save(self, value):
            self.saved = value

    class Discovery:
        def __init__(self):
            self.goals = []

        def collect(
            self, goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            self.goals.append(goal)
            return seed_bundle or EvidenceBundle(goal=goal)

    discovery = Discovery()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "readonly_action",
            goal_relation="continue_previous",
            goal_kind="causal_diagnosis",
        ),
        discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("你自己定位啊")

    assert result.kind == "completed"
    assert discovery.goals == [
        "只读诊断并补齐以下运维目标所需证据：检查 v4e2e_m 为什么报错"
    ]


def test_causal_followup_refines_previous_goal_and_reuses_static_evidence():
    from klonet_agent.ops.privileged.workflow.operational_context import OperationalContextSnapshot
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    previous = EvidenceBundle(goal="v4e2e_m 现在是不是有报错")
    previous.add(EvidenceRecord.from_probe(
        ProbeRequest("ops_file", {"path": "/srv/app/mains/gun.py"}, "启动配置"),
        "errorlog=/srv/app/logs/error.log",
    ))
    previous.add(EvidenceRecord.from_probe(
        ProbeRequest("screen_session", {"session": "v4e2e_m"}, "当前报错"),
        "stale traceback",
    ))
    snapshot = OperationalContextSnapshot(
        resolved_goal="v4e2e_m 现在是不是有报错",
        target_roots=["/srv/app"],
        phase="diagnosing",
        evidence=previous,
    )

    class Store:
        def load(self):
            return snapshot

        def save(self, value):
            self.saved = value

    class Discovery:
        def __init__(self):
            self.calls = []

        def collect(
            self, goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            self.calls.append((goal, seed_bundle))
            return seed_bundle or EvidenceBundle(goal=goal)

    discovery = Discovery()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("readonly_action", goal_relation="refine_previous"),
        discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("这个报错是为啥啊")

    assert result.kind == "completed"
    goal, seed = discovery.calls[0]
    assert "v4e2e_m 现在是不是有报错" in goal
    assert "这个报错是为啥啊" in goal
    assert [item.request.probe for item in seed.records] == ["ops_file"]


def test_privileged_intent_contract_marks_causal_followup_as_refinement():
    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    decision = PrivilegedIntentClassifier._decision({
        "intent": "readonly_action",
        "goal_clarity": "discoverable",
        "goal_relation": "refine_previous",
        "confidence": 1,
    })

    assert decision.goal_relation == "refine_previous"


def test_failure_option_reenters_discovery_and_creates_new_component_plan():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, EvidenceRecord, FailureRecord,
        ProbeRequest, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    goal = "帮我重启 v4_e2e 平台"
    reusable = EvidenceBundle(goal=goal)
    knowledge = reusable.add(EvidenceRecord.from_probe(
        ProbeRequest("klonet_knowledge", {"query": goal}, "启动合同"),
        "source=startup_shutdown.md\nstartup_cwd=<project_root>",
    ))
    snapshot = OperationalContextSnapshot(
        resolved_goal=goal,
        target_roots=["/home/lzl/klonet_v4_e2e"],
        phase="awaiting_user_decision",
        evidence=reusable,
    )
    failure = FailureRecord(
        failure_id="failure-restart-loop",
        stage="binding",
        category="implementation_contract_invalid",
        summary="原子步骤无法落地",
        technical_reason="component contract missing",
        goal=goal,
        selected_option_id="component_restart",
        options=[RecoveryOption(
            option_id="component_restart",
            label="改用逐组件安全重启",
            description="刷新运行证据并生成新计划",
            action="component_restart",
            recommended=True,
        )],
    )

    class ControlMutation:
        def __init__(self):
            self.submissions = []

        def handle_control(self, text):
            return WorkflowResult(
                True, "failure_option_selected", "selected", failure=failure,
            )

        def plan_once(
            self, submitted_goal, *, evidence_bundle, evidence_conclusion,
            intent_context=None, binding_feedback="",
        ):
            from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

            self.submissions.append(
                (submitted_goal, evidence_bundle, dict(intent_context or {}))
            )
            return GoalOutcome(
                "need_execution", plan=SimpleNamespace(
                    plan_id="priv-ops-new", content_hash="new-hash"
                ),
            )

        def bind_once(self, plan, *, evidence_bundle):
            from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

            return GoalOutcome(
                "needs_user_decision", user_question="new plan", plan=plan,
            )

    class RefreshDiscovery:
        def collect(
            self, requested_goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            assert [item.evidence_id for item in seed_bundle.records] == [
                knowledge.evidence_id
            ]
            seed_bundle.add(EvidenceRecord.from_probe(
                ProbeRequest("running_platforms", {}, "刷新运行状态"),
                "platform=v4e2e project_root=/home/lzl/klonet_v4_e2e "
                "roles=master,worker configured_ports=master_port:47001,"
                "worker_port:47002 component_specs_b64=e30= "
                "runtime_identities=10:1000:/opt/python3.8",
            ))
            return seed_bundle

    class Store:
        def load(self):
            return snapshot

        def save(self, value):
            self.saved = value

    mutation = ControlMutation()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=RefreshDiscovery(),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("选择 1")

    assert result.kind == "awaiting_confirmation"
    submitted_goal, refreshed, intent = mutation.submissions[0]
    assert submitted_goal == goal
    assert knowledge.evidence_id in refreshed.evidence_ids
    assert intent["operation"] == "restart"
    assert intent["scope"] == "platform"
    assert intent["resolved_project_root"] == "/home/lzl/klonet_v4_e2e"


def test_operational_context_persists_goal_locale_and_only_reuses_static_evidence(
    tmp_path,
):
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot, OperationalContextStore,
    )
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    bundle = EvidenceBundle(goal="重启 v4e2e")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("project_layout", {"root": "/srv/app"}, "layout"),
        "project_root=/srv/app",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("process", {"keywords": ["app"]}, "runtime"),
        "pid=stale",
    ))
    store = OperationalContextStore(
        tmp_path, user_id="lzl", project_id="v4e2e",
    )
    store.save(OperationalContextSnapshot(
        resolved_goal="帮我重启 v4e2e 的 master 和 worker",
        output_locale="zh-CN",
        target_roots=["/srv/app"],
        phase="draft_ready",
        evidence=bundle,
    ))

    restored = store.load()

    assert restored is not None
    assert restored.resolved_goal == "帮我重启 v4e2e 的 master 和 worker"
    assert restored.output_locale == "zh-CN"
    assert restored.target_roots == ["/srv/app"]
    reusable = restored.reusable_evidence(restored.resolved_goal)
    assert [item.request.probe for item in reusable.records] == ["project_layout"]


def test_internal_planner_contract_error_is_localized_before_display():
    from klonet_agent.ops.privileged.workflow.coordinator import (
        WorkflowResult, _localized_result,
    )

    result = _localized_result(WorkflowResult(
        True,
        "blocked",
        "Change Planner output invalid after bounded repairs: blocked cannot "
        "offload discoverable implementation details; Discovery or Binding must resolve them",
    ))

    assert "没有执行任何变更" in result.message
    assert "Change Planner" not in result.message
    assert "Discovery or Binding" not in result.message


def test_unknown_internal_block_reason_is_not_leaked_in_english():
    from klonet_agent.ops.privileged.workflow.coordinator import (
        WorkflowResult, _localized_result,
    )

    result = _localized_result(WorkflowResult(
        True,
        "blocked",
        "resource_binding_violation=restart.platform must use instance_identifier",
    ))

    assert result.message == "内部安全校验未通过；本轮没有执行任何变更。"
    assert "resource_binding" not in result.message


def test_structured_continuation_replaces_phrase_specific_routing():
    decision = StubClassifier(
        "readonly_action",
        goal_relation="continue_previous",
        goal_kind="causal_diagnosis",
    ).decision

    assert decision.goal_relation == "continue_previous"
    assert decision.goal_kind == "causal_diagnosis"
