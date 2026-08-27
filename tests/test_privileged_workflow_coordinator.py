from __future__ import annotations

from types import SimpleNamespace

import pytest


class StubClassifier:
    def __init__(
        self, intent, command="", goal_clarity="clear", goal_relation="new",
        goal_kind="", operation="none", scope="none", components=(),
        plan_reference="",
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
            plan_reference=plan_reference,
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
        self.failures = []

    def plan_once(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("readonly request must not enter mutation workflow")

    def failure_result(self, **kwargs):
        from klonet_agent.ops.privileged.workflow.coordinator import WorkflowResult

        self.failures.append(kwargs)
        return WorkflowResult(
            True,
            "awaiting_user_decision",
            str(kwargs.get("technical_reason") or "workflow failed"),
            evidence=kwargs.get("evidence"),
        )


class RecordingMutationWorkflow:
    def __init__(self):
        self.calls = []

    def plan_once(self, *args, **kwargs):
        from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

        self.calls.append((args, kwargs))
        return GoalOutcome(
            "need_execution", plan=SimpleNamespace(
                status="draft", plan_id="priv-ops-test",
            ),
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
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )

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


def test_validated_readonly_route_is_announced_before_discovery():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle, conclusion = _evidence()
    events = []

    class OrderedDiscovery(StubDiscovery):
        def collect(self, goal, *, command="", conversation_context=""):
            events.append("discovery")
            return super().collect(
                goal, command=command, conversation_context=conversation_context,
            )

    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "readonly_action", goal_kind="health_check", operation="inspect",
        ),
        discovery=OrderedDiscovery(bundle),
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
        on_progress=events.append,
    )

    result = coordinator.handle("检查平台运行情况")

    assert result.kind == "completed"
    assert events == ["已识别：只读检查", "discovery"]


def test_conversation_route_does_not_emit_privileged_workflow_identity():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    events = []
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=StubDiscovery(_evidence()[0]),
        synthesis=StubSynthesis(_evidence()[1]),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
        on_progress=events.append,
    )

    result = coordinator.handle("你好")

    assert result.handled is False
    assert events == []


def test_mutating_route_announces_change_planning_before_discovery():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    bundle, conclusion = _evidence()
    events = []

    class OrderedDiscovery(StubDiscovery):
        def collect(self, goal, *, command="", conversation_context=""):
            events.append("discovery")
            return super().collect(
                goal, command=command, conversation_context=conversation_context,
            )

    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "mutating_action", goal_kind="execution", operation="restart",
            scope="platform",
        ),
        discovery=OrderedDiscovery(bundle),
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=RecordingMutationWorkflow(),
        verifier=StubGoalVerifier(),
        on_progress=events.append,
    )

    result = coordinator.handle("重启目标平台")

    assert result.kind == "awaiting_confirmation"
    assert events == ["已识别：变更规划", "discovery"]


def test_persisted_recovery_option_preempts_classifier_and_reenters_plan_loop():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, FailureRecord, GoalOutcome, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    bundle, conclusion = _evidence()
    failure = FailureRecord(
        failure_id="failure-recoveryroute",
        stage="discovery",
        category="planning_contract_unresolved",
        summary="planning failed",
        technical_reason="invalid semantic change",
        goal="把 test Master 和 Celery 收编进 Screen",
        goal_kind="execution",
        options=[RecoveryOption(
            option_id="continue_current_goal",
            label="继续处理",
            description="刷新证据后重新规划",
            action="continue_current_goal",
            recommended=True,
        )],
        selected_option_id="continue_current_goal",
    )

    class ForbiddenClassifier:
        def classify(self, *args, **kwargs):
            raise AssertionError("persisted recovery control must preempt classification")

    class RecoveryWorkflow:
        max_replanning_rounds = 1
        max_candidate_replans = 1
        planner = SimpleNamespace()

        def handle_control(self, text):
            assert text == "继续处理"
            return WorkflowResult(
                True, "failure_option_selected", failure=failure,
            )

        def plan_once(self, *args, **kwargs):
            return GoalOutcome(
                "need_execution",
                plan=SimpleNamespace(
                    plan_id="priv-ops-recovered", status="draft",
                ),
            )

        def bind_once(self, plan, *, evidence_bundle):
            return GoalOutcome(
                "needs_user_decision", user_question="recovered plan", plan=plan,
            )

    class ContextStore:
        def __init__(self):
            self.snapshot = OperationalContextSnapshot(
                resolved_goal=failure.goal,
                evidence=EvidenceBundle(goal=failure.goal),
            )

        def load(self):
            return self.snapshot

        def save(self, snapshot):
            self.snapshot = snapshot

    progress = []
    coordinator = PrivilegedOpsCoordinator(
        classifier=ForbiddenClassifier(),
        discovery=StubDiscovery(bundle),
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=RecoveryWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=ContextStore(),
        on_progress=progress.append,
    )

    result = coordinator.handle("继续处理")

    assert result.kind == "awaiting_confirmation"
    assert result.message == "recovered plan"
    assert result.plan.plan_id == "priv-ops-recovered"
    assert progress == [
        "已选择恢复方案：继续处理",
        "正在从 Discovery 阶段恢复原目标并刷新运行态证据",
        "证据补充完成，正在重新规划",
    ]


def test_pending_failure_state_blocks_classifier_for_unmatched_input(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    class ForbiddenClassifier:
        def classify(self, *args, **kwargs):
            raise AssertionError("pending failure must own the input boundary")

    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    failure = FailureRecord(
        failure_id="failure-state-gate",
        stage="planning",
        category="planning_evidence_no_progress",
        summary="planning stopped",
        technical_reason="no progress",
        goal="重启全部平台",
        goal_kind="execution",
        options=[RecoveryOption(
            option_id="continue_current_goal",
            label="继续处理",
            description="刷新证据",
            action="continue_current_goal",
        )],
    )
    store.save_failure(failure)
    workflow = MutationWorkflow(
        planner=object(), binder=object(), store=store,
        executor=object(), verifier=object(),
    )
    coordinator = PrivilegedOpsCoordinator(
        classifier=ForbiddenClassifier(), discovery=object(), synthesis=object(),
        response=object(), mutation_workflow=workflow, verifier=object(),
    )

    result = coordinator.handle("换一个新目标")

    assert result.kind == "awaiting_user_decision"
    assert result.failure.failure_id == failure.failure_id
    assert "暂不接受新的语义操作" in result.message


def test_readonly_failure_is_persisted_and_human_retry_reenters_readonly_loop(
    tmp_path,
):
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    bundle, conclusion = _evidence()

    class ForbiddenPlanner:
        def __init__(self):
            self.calls = 0

        def plan(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("readonly recovery must not create a change plan")

    planner = ForbiddenPlanner()
    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    verifier = StubGoalVerifier(GoalOutcome(
        "blocked",
        reason=(
            "Verifier 在有限次数校正后仍未形成有效的目标级判断。 "
            "contract_error=no new registered probe requests"
        ),
    ))
    mutation = MutationWorkflow(
        planner=planner,
        binder=object(),
        store=store,
        executor=object(),
        verifier=verifier,
    )
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("readonly_action", goal_kind="health_check"),
        discovery=StubDiscovery(bundle),
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=verifier,
    )

    failed = coordinator.handle("检查当前运行的平台")

    assert failed.kind == "awaiting_user_decision"
    assert failed.failure.stage == "verification"
    assert failed.failure.goal_kind == "health_check"
    assert failed.failure.plan_id == ""
    assert "no new registered probe requests" in failed.failure.technical_reason
    assert failed.failure.options[0].action == "continue_current_goal"
    assert "回到原工作流" in failed.failure.options[0].description
    assert store.load_failure(failed.failure.failure_id).goal == failed.failure.goal

    verifier.outcome = GoalOutcome("achieved")
    recovered = coordinator.handle("选择 1")

    assert recovered.kind == "completed"
    assert recovered.outcome.status == "achieved"
    assert planner.calls == 0


def test_completed_plan_status_followup_reads_receipt_without_discovery_or_verifier():
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    bundle, conclusion = _evidence()

    class ContextStore:
        def load(self):
            return OperationalContextSnapshot(
                resolved_goal="帮我重启 v4e2e 平台",
                active_plan_id="priv-ops-completed",
                evidence=bundle,
            )

    class StatusWorkflow(NoMutationWorkflow):
        def manage_plan_turn(self, plan_id, **kwargs):
            assert plan_id == "priv-ops-completed"
            assert kwargs["question"] == "啥意思，你已经执行完了吗"
            return WorkflowResult(
                True, "plan_status",
                "计划已完成：master 与 worker 均通过健康检查。",
            )

    class ForbiddenVerifier(StubGoalVerifier):
        def verify_goal(self, *args, **kwargs):
            raise AssertionError("status query must not enter Verifier")

    discovery = StubDiscovery(bundle)
    progress = []
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "resume_plan", operation="inspect", plan_reference="latest",
        ),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=StatusWorkflow(),
        verifier=ForbiddenVerifier(),
        context_store=ContextStore(),
        on_progress=progress.append,
    )

    result = coordinator.handle("啥意思，你已经执行完了吗")

    assert result.kind == "plan_status"
    assert result.message == "计划已完成：master 与 worker 均通过健康检查。"
    assert discovery.calls == []
    assert progress == ["已识别：计划管理"]


def test_natural_plan_resume_returns_persisted_plan_instead_of_replanning():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="重启 v4e2e 平台",
        active_plan_id="priv-ops-awaiting",
        evidence=EvidenceBundle(goal="重启 v4e2e 平台"),
    )

    class ContextStore:
        def load(self):
            return snapshot

    class ExistingPlanWorkflow(NoMutationWorkflow):
        def plan_exists(self, plan_id):
            return plan_id == "priv-ops-awaiting"

        def manage_plan_turn(self, plan_id, **kwargs):
            assert plan_id == "priv-ops-awaiting"
            return WorkflowResult(
                True, "plan_status", "计划 priv-ops-awaiting 等待确认。",
            )

    discovery = StubDiscovery(EvidenceBundle(goal="forbidden"))
    mutation = ExistingPlanWorkflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "resume_plan", operation="none", plan_reference="latest",
        ),
        discovery=discovery,
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
        context_store=ContextStore(),
    )

    result = coordinator.handle("继续刚才的计划")

    assert result.kind == "plan_status"
    assert result.message == "计划 priv-ops-awaiting 等待确认。"
    assert discovery.calls == []
    assert mutation.calls == []


def test_continue_supersedes_orphaned_draft_and_reenters_single_plan_loop():
    from klonet_agent.ops.privileged.intent import PrivilegedIntentDecision
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, GoalOutcome,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    goal = "直接帮我把test 平台规范的用screen重新启动吧"
    draft = SimpleNamespace(
        plan_id="priv-ops-orphaned-draft",
        goal=goal,
        status="draft",
    )
    replacement = SimpleNamespace(
        plan_id="priv-ops-replacement",
        goal=goal,
        status="draft",
    )
    snapshot = OperationalContextSnapshot(
        resolved_goal=goal,
        evidence=EvidenceBundle(goal=goal),
    )

    class Store:
        def __init__(self):
            self.saved = []

        def load(self):
            return snapshot

        def save(self, value):
            self.saved.append(value)

    class Workflow(NoMutationWorkflow):
        max_replanning_rounds = 1
        max_candidate_replans = 1

        def latest_plan_id_for_goal(self, requested_goal):
            assert requested_goal == goal
            return draft.plan_id

        def plan_exists(self, plan_id):
            return plan_id == draft.plan_id

        def load_plan(self, plan_id):
            assert plan_id == draft.plan_id
            return draft

        def abort_plan(self, plan_id):
            assert plan_id == draft.plan_id
            draft.status = "aborted"
            return True

        def plan_once(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return GoalOutcome("need_execution", plan=replacement)

        def bind_once(self, plan, *, evidence_bundle):
            assert plan is replacement
            replacement.status = "awaiting_confirmation"
            return GoalOutcome(
                "needs_user_decision",
                user_question="请确认恢复后的计划",
                plan=replacement,
            )

    class Classifier:
        def classify(self, *args, **kwargs):
            return PrivilegedIntentDecision(
                intent="resume_plan",
                requires_execution=False,
                plan_reference="latest",
                operation="none",
            )

    store = Store()
    workflow = Workflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=Classifier(),
        discovery=StubDiscovery(EvidenceBundle(goal="unused")),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=workflow,
        verifier=StubGoalVerifier(),
        context_store=store,
    )

    result = coordinator.handle("继续")

    assert result.kind == "awaiting_confirmation"
    assert result.plan is replacement
    assert draft.status == "aborted"
    assert store.saved[-1].active_plan_id == replacement.plan_id
    assert len(workflow.calls) == 1


def test_continue_restores_persisted_mutation_when_binding_never_created_a_plan():
    from klonet_agent.ops.privileged.intent import PrivilegedIntentDecision
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    goal = "把 test 平台的 master 和 celery 用 Screen 重新启动"
    snapshot = OperationalContextSnapshot(
        resolved_goal=goal,
        workflow_intent="mutating_action",
        goal_kind="execution",
        operation="restart",
        scope="component",
        components=["master", "celery"],
        evidence=EvidenceBundle(goal=goal),
    )

    class Store:
        def load(self):
            return snapshot

        def save(self, value):
            self.saved = value

    class ResumeClassifier:
        def classify(self, *args, **kwargs):
            return PrivilegedIntentDecision(
                intent="resume_plan",
                requires_execution=False,
                plan_reference="latest",
                operation="none",
            )

    bundle, conclusion = _evidence()
    discovery = StubDiscovery(bundle)
    mutation = RecordingMutationWorkflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=ResumeClassifier(),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("继续")

    assert result.kind == "awaiting_confirmation"
    assert discovery.calls[0][0].endswith(goal)
    assert mutation.calls[0][0][0] == goal
    assert mutation.calls[0][1]["intent_context"] == {
        "operation": "restart",
        "scope": "component",
        "components": ["master", "celery"],
    }


def test_continue_on_active_paused_plan_enters_post_execution_before_classifier():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    plan = SimpleNamespace(
        plan_id="priv-ops-paused", goal="重启全部平台", status="paused",
    )
    snapshot = OperationalContextSnapshot(
        resolved_goal=plan.goal, base_goal=plan.goal,
        active_plan_id=plan.plan_id, evidence=EvidenceBundle(goal=plan.goal),
    )

    class Store:
        def load(self):
            return snapshot

    class ForbiddenClassifier:
        def classify(self, *args, **kwargs):
            raise AssertionError("paused state must consume continue first")

    class Workflow:
        def handle_control(self, text):
            return None

        def resume_paused_plan(self, plan_id):
            assert plan_id == plan.plan_id
            return WorkflowResult(True, "paused", "paused", plan=plan)

    class Coordinator(PrivilegedOpsCoordinator):
        def _continue_post_execution(self, control, **kwargs):
            self.resumed = control.plan
            return WorkflowResult(True, "resumed", "replanning", plan=control.plan)

    coordinator = Coordinator(
        classifier=ForbiddenClassifier(), discovery=StubDiscovery(snapshot.evidence),
        synthesis=StubSynthesis(SimpleNamespace()), response=StubResponse(),
        mutation_workflow=Workflow(), verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("继续")

    assert result.kind == "resumed"
    assert coordinator.resumed is plan


def test_plan_refinement_revokes_old_hash_and_creates_one_replacement_plan():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    old_goal = "重启 test 平台"
    snapshot = OperationalContextSnapshot(
        resolved_goal=old_goal,
        active_plan_id="priv-ops-old",
        workflow_intent="mutating_action",
        goal_kind="execution",
        operation="restart",
        scope="platform",
        evidence=EvidenceBundle(goal=old_goal),
    )

    class Store:
        def __init__(self):
            self.saved = []

        def load(self):
            return snapshot

        def save(self, value):
            self.saved.append(value)

    class Workflow(RecordingMutationWorkflow):
        def __init__(self):
            super().__init__()
            self.aborted = []

        def abort_plan(self, plan_id):
            self.aborted.append(plan_id)
            return True

    bundle, conclusion = _evidence()
    workflow = Workflow()
    store = Store()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "mutating_action", goal_relation="refine_previous",
            goal_kind="execution", operation="restart", scope="component",
            components=("master", "celery", "worker"),
        ),
        discovery=StubDiscovery(bundle),
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=workflow,
        verifier=StubGoalVerifier(),
        context_store=store,
    )

    result = coordinator.handle(
        "修正计划：worker 保留原环境，不包含 web_terminal，并验收 Screen",
    )

    assert result.kind == "awaiting_confirmation"
    assert workflow.aborted == ["priv-ops-old"]
    assert store.saved[0].active_plan_id == ""
    assert store.saved[-1].active_plan_id == "priv-ops-test"
    assert workflow.calls[0][0][0] == old_goal
    assert workflow.calls[0][1]["intent_context"]["base_goal"] == old_goal
    assert workflow.calls[0][1]["intent_context"]["decision_history"] == [
        "修正计划：worker 保留原环境，不包含 web_terminal，并验收 Screen"
    ]
    assert store.saved[-1].base_goal == old_goal


def test_active_plan_scope_reduction_requires_explicit_overwrite_confirmation():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    base_goal = "把所有平台的所有角色收编进 Screen"
    snapshot = OperationalContextSnapshot(
        resolved_goal=base_goal, base_goal=base_goal,
        active_plan_id="priv-ops-all", workflow_intent="mutating_action",
        goal_kind="execution", evidence=EvidenceBundle(goal=base_goal),
    )

    class Store:
        def load(self):
            return snapshot

        def save(self, value):
            self.saved = value

    class Workflow(RecordingMutationWorkflow):
        def classify_recovery_reply(self, **kwargs):
            return {
                "relation": "revise",
                "reason": "explicitly excludes other platforms",
                "normalized_decision": kwargs["reply"],
                "candidate_base_goal": "只收编 test 平台",
                "conflicts": ["删除其他平台目标"],
            }

        def abort_plan(self, plan_id):
            raise AssertionError("覆盖确认前不得废弃现有计划")

    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "mutating_action", goal_relation="refine_previous",
            goal_kind="execution",
        ),
        discovery=StubDiscovery(EvidenceBundle(goal=base_goal)),
        synthesis=StubSynthesis(SimpleNamespace()), response=StubResponse(),
        mutation_workflow=Workflow(), verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("其他平台都取消，只处理 test")

    assert result.kind == "clarification"
    assert "确认覆盖目标" in result.message
    assert snapshot.base_goal == base_goal
    assert snapshot.pending_goal_revision == "只收编 test 平台"


def test_active_plan_supplement_replans_all_unfinished_predecessor_steps():
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, EvidenceBundle, GoalOutcome, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    base_goal = "把所有平台的所有角色收编进 Screen"
    predecessor = ChangePlan(
        plan_id="priv-ops-predecessor", goal=base_goal, risk="medium",
        steps=[
            ChangeStep(
                step_id="restart-alpha", title="alpha", objective="alpha",
                risk="medium", expected_changes=["alpha Screen ready"],
                postconditions=[{"checker": "screen_session_exists"}],
                status="paused",
            ),
            ChangeStep(
                step_id="restart-beta", title="beta", objective="beta",
                risk="medium", expected_changes=["beta Screen ready"],
                postconditions=[{"checker": "screen_session_exists"}],
                status="pending",
            ),
        ],
        resources=[
            PlanResource(
                name="alpha_root", kind="path", status="frozen",
                role="instance_root", value="/srv/alpha",
                consumers=["restart-alpha.project_root"],
            ),
            PlanResource(
                name="beta_root", kind="path", status="frozen",
                role="instance_root", value="/srv/beta",
                consumers=["restart-beta.project_root"],
            ),
        ],
        status="paused",
    )
    snapshot = OperationalContextSnapshot(
        resolved_goal=base_goal, base_goal=base_goal,
        active_plan_id=predecessor.plan_id,
        workflow_intent="mutating_action", goal_kind="execution",
        operation="restart", scope="platform",
        evidence=EvidenceBundle(goal=base_goal),
    )

    class Store:
        def load(self):
            return snapshot

        def save(self, value):
            self.saved = value

    class Workflow:
        max_replanning_rounds = 1
        max_candidate_replans = 1
        planner = SimpleNamespace()

        def classify_recovery_reply(self, **kwargs):
            return {
                "relation": "supplement", "normalized_decision": kwargs["reply"],
            }

        def load_plan(self, plan_id):
            assert plan_id == predecessor.plan_id
            return predecessor

        def abort_plan(self, plan_id):
            return True

        def plan_once(self, goal, **kwargs):
            self.plan_kwargs = kwargs
            # Candidate refinement may be in-place; it must not rewrite the
            # predecessor snapshot later supplied to Binding.
            predecessor.status = "draft"
            return GoalOutcome("need_execution", plan=predecessor)

        def bind_once(self, plan, *, evidence_bundle, predecessor_plan=None):
            self.bound_predecessor = predecessor_plan
            return GoalOutcome(
                "needs_user_decision", user_question="replacement", plan=plan,
            )

    workflow = Workflow()
    bundle, conclusion = _evidence()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "mutating_action", goal_relation="refine_previous",
            goal_kind="execution", operation="restart", scope="component",
            components=("worker",),
        ),
        discovery=StubDiscovery(bundle), synthesis=StubSynthesis(conclusion),
        response=StubResponse(), mutation_workflow=workflow,
        verifier=StubGoalVerifier(), context_store=Store(),
    )

    result = coordinator.handle("alpha worker 复用 alpha_w")

    assert result.kind == "awaiting_confirmation"
    context = workflow.plan_kwargs["intent_context"]
    assert context["recovery_required_step_ids"] == [
        "restart-alpha", "restart-beta",
    ]
    assert context["active_gap_affected_steps"] == [
        "restart-alpha", "restart-beta",
    ]
    assert workflow.plan_kwargs["candidate_plan"] is predecessor
    assert workflow.bound_predecessor is not predecessor
    assert workflow.bound_predecessor.plan_id == predecessor.plan_id
    assert workflow.bound_predecessor.status == "paused"
    assert predecessor.status == "draft"


def test_refinement_predecessor_prefers_executed_plan_over_unapproved_candidate():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    bundle = EvidenceBundle(goal="recover all")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest(
            "plan_execution",
            {"plan_id": "priv-ops-executed", "status": "paused"},
            "persisted execution",
        ),
        "plan_execution status=paused",
    ))
    snapshot = OperationalContextSnapshot(
        resolved_goal="recover all", active_plan_id="priv-ops-unapproved",
        evidence=bundle,
    )
    executed = SimpleNamespace(plan_id="priv-ops-executed", status="paused")
    unapproved = SimpleNamespace(
        plan_id="priv-ops-unapproved", status="awaiting_confirmation",
    )

    class Workflow:
        def load_plan(self, plan_id):
            return {
                "priv-ops-executed": executed,
                "priv-ops-unapproved": unapproved,
            }[plan_id]

    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=StubDiscovery(bundle), synthesis=StubSynthesis(SimpleNamespace()),
        response=StubResponse(), mutation_workflow=Workflow(),
        verifier=StubGoalVerifier(),
    )

    assert coordinator._refinement_predecessor(snapshot) is executed


def test_bare_confirmation_cannot_modify_or_approve_an_active_plan():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="重启 test 平台",
        active_plan_id="priv-ops-awaiting",
        evidence=EvidenceBundle(goal="重启 test 平台"),
    )

    class Store:
        def load(self):
            return snapshot

    class ForbiddenClassifier:
        def classify(self, *args, **kwargs):
            raise AssertionError("exact ambiguous confirmation is state-guarded")

    coordinator = PrivilegedOpsCoordinator(
        classifier=ForbiddenClassifier(),
        discovery=StubDiscovery(EvidenceBundle(goal="unused")),
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("确认")

    assert result.kind == "clarification"
    assert "不会修改或批准" in result.message
    assert "confirm-priv-plan" in result.message


def test_explicit_missing_plan_id_is_not_converted_into_goal_resumption():
    from klonet_agent.ops.privileged.intent import PrivilegedIntentDecision
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="重启 test 平台",
        workflow_intent="mutating_action",
        goal_kind="execution",
        operation="restart",
        scope="platform",
        evidence=EvidenceBundle(goal="重启 test 平台"),
    )

    class Store:
        def load(self):
            return snapshot

    class Classifier:
        def classify(self, *args, **kwargs):
            return PrivilegedIntentDecision(
                intent="resume_plan",
                requires_execution=False,
                plan_reference="priv-ops-does-not-exist",
                operation="none",
            )

    class MissingPlanWorkflow(NoMutationWorkflow):
        def plan_exists(self, plan_id):
            assert plan_id == "priv-ops-does-not-exist"
            return False

    discovery = StubDiscovery(EvidenceBundle(goal="forbidden"))
    coordinator = PrivilegedOpsCoordinator(
        classifier=Classifier(),
        discovery=discovery,
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=MissingPlanWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("继续 priv-ops-does-not-exist")

    assert result.kind == "clarification"
    assert "计划 ID" in result.message
    assert discovery.calls == []


def test_misclassified_recap_cannot_enter_plan_route_without_a_real_plan():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="盘点服务器上正在运行的 Klonet 平台",
        active_plan_id="",
        evidence=EvidenceBundle(goal="盘点服务器上正在运行的 Klonet 平台"),
    )

    class ContextStore:
        def load(self):
            return snapshot

    class NoPlanWorkflow(NoMutationWorkflow):
        def plan_exists(self, plan_id):
            raise AssertionError("empty latest reference must not query plan storage")

        def render_plan_status(self, plan_id):
            raise AssertionError("a read-only conversation has no Plan status")

    discovery = StubDiscovery(EvidenceBundle(goal="forbidden"))
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "resume_plan", operation="inspect", plan_reference="latest",
        ),
        discovery=discovery,
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=NoPlanWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=ContextStore(),
    )

    result = coordinator.handle("刚刚我们进行到哪一步了？")

    assert result.handled is False
    assert result.kind == "conversation"
    assert discovery.calls == []


def test_stale_active_plan_reference_is_rejected_by_persisted_state_guard():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="查看平台运行状态",
        active_plan_id="priv-ops-missing",
        evidence=EvidenceBundle(goal="查看平台运行状态"),
    )

    class ContextStore:
        def load(self):
            return snapshot

    class StalePlanWorkflow(NoMutationWorkflow):
        def plan_exists(self, plan_id):
            assert plan_id == "priv-ops-missing"
            return False

        def render_plan_status(self, plan_id):
            raise AssertionError("stale Plan must not be rendered")

    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "resume_plan", operation="inspect", plan_reference="latest",
        ),
        discovery=StubDiscovery(EvidenceBundle(goal="forbidden")),
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=StalePlanWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=ContextStore(),
    )

    result = coordinator.handle("回顾一下刚才的进度")

    assert result.handled is False
    assert result.kind == "conversation"


def test_conversational_continuation_never_promotes_itself_to_discovery():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="检查 102 平台运行状态",
        evidence=EvidenceBundle(goal="检查 102 平台运行状态"),
    )

    class ContextStore:
        def load(self):
            return snapshot

    discovery = StubDiscovery(EvidenceBundle(goal="forbidden"))
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "conversation", goal_relation="continue_previous",
        ),
        discovery=discovery,
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=ContextStore(),
    )

    result = coordinator.handle("刚才说到哪里了？")

    assert result.handled is False
    assert result.kind == "conversation"
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
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator,
        WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
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

    class Store:
        def __init__(self):
            self.snapshot = OperationalContextSnapshot(
                resolved_goal="重启 test 的 celery",
                base_goal="重启 test 的 celery",
                active_plan_id="priv-ops-flow",
                evidence=EvidenceBundle(goal="重启 test 的 celery"),
            )

        def load(self):
            return self.snapshot

        def save(self, value):
            self.snapshot = value

    controls = Controls()
    store = Store()
    coordinator = PrivilegedOpsCoordinator(
        classifier=NoCall(),
        discovery=NoCall(),
        synthesis=NoCall(),
        response=NoCall(),
        mutation_workflow=controls,
        verifier=NoCall(),
        context_store=store,
    )

    result = coordinator.handle_with_context(
        "confirm-priv-plan priv-ops-flow " + "a" * 64,
        environment_context="ignored",
        conversation_context="recent",
    )

    assert result.kind == "completed"
    assert controls.calls == ["confirm-priv-plan priv-ops-flow " + "a" * 64]
    assert store.snapshot.resolved_goal == ""
    assert store.snapshot.base_goal == ""
    assert store.snapshot.active_plan_id == ""


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
    submitted = workflow.submits[0][1]
    evidence = submitted["evidence_bundle"]
    assert evidence.records[0].request.probe == "plan_execution"
    assert "master health check failed" in evidence.records[0].output
    assert submitted["candidate_plan"] is failed_plan
    assert submitted["intent_context"]["recovery_failure_category"] == (
        "post_execution"
    )
    assert submitted["intent_context"]["operation"] == "restart"
    assert submitted["intent_context"]["scope"] == "platform"
    assert "验证已审批任务是否已经达到完整用户目标" in discovery.goal
    assert "未达到目标的原因" not in discovery.goal


def test_unknown_execution_reconciled_as_unmet_skips_goal_probe_loop_and_replans():
    from klonet_agent.ops.privileged.contracts import CheckResult
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, GoalOutcome,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )

    atomic = SimpleNamespace(
        step_id="stop-worker", status="execution_unknown", checks=[],
        evidence=None, observation="interrupted",
    )
    implementation = SimpleNamespace(steps=[atomic], status="paused")
    change = SimpleNamespace(
        step_id="restart-worker", status="paused", observation="interrupted",
        implementation_plan=implementation,
    )
    plan = SimpleNamespace(
        plan_id="priv-ops-unknown", goal="重启 worker", status="paused",
        steps=[change], resources=[], is_authorized=True,
    )

    class Workflow:
        def __init__(self):
            self.submits = []

        def handle_control(self, text):
            return WorkflowResult(True, "paused", "interrupted", plan=plan)

        def reconcile_recovery_state(self, candidate):
            assert candidate is plan
            atomic.status = "paused"
            atomic.checks = [
                CheckResult("process_pid_absent", "failed", observed="pid exists")
            ]
            return candidate

        def plan_once(self, goal, **kwargs):
            self.submits.append((goal, kwargs))
            return GoalOutcome(
                "need_execution", plan=SimpleNamespace(status="draft"),
            )

        def bind_once(self, candidate, *, evidence_bundle, **kwargs):
            del evidence_bundle, kwargs
            return GoalOutcome(
                "needs_user_decision", user_question="recovery plan",
                plan=candidate,
            )

    class Discovery:
        def __init__(self):
            self.collect_calls = 0

        def collect(
            self, goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            del goal, command, conversation_context, preload_capabilities
            self.collect_calls += 1
            return seed_bundle or EvidenceBundle(goal=plan.goal)

        def collect_requests(self, requests, bundle):
            raise AssertionError("deterministic recovery must not enter probe loop")

    class ForbiddenGoalVerifier:
        def verify_goal(self, *args, **kwargs):
            raise AssertionError("atomic recovered failure is already a replan contract")

    workflow = Workflow()
    discovery = Discovery()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"), discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()), response=StubResponse(),
        mutation_workflow=workflow, verifier=ForbiddenGoalVerifier(),
    )

    result = coordinator.handle("confirm existing plan")

    assert result.kind == "awaiting_confirmation"
    assert discovery.collect_calls == 1
    assert workflow.submits[0][0] == "重启 worker"
    assert workflow.submits[0][1]["intent_context"][
        "recovery_technical_reason"
    ].startswith("Interrupted steps are deterministically unmet")


def test_post_execution_without_grounded_replan_is_escalated_to_human():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, GoalOutcome,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )

    failed_plan = SimpleNamespace(
        plan_id="priv-ops-unresolved",
        goal="修复 v4e2e master",
        status="paused",
        steps=[],
    )

    class Workflow:
        def __init__(self):
            self.failure = None

        def handle_control(self, text):
            return WorkflowResult(True, "paused", "health failed", plan=failed_plan)

        def failure_result(self, **kwargs):
            self.failure = kwargs
            return WorkflowResult(
                True, "awaiting_user_decision", kwargs["technical_reason"],
                plan=kwargs.get("plan"), evidence=kwargs.get("evidence"),
            )

    class Discovery:
        def collect(
            self, goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            return seed_bundle or EvidenceBundle(goal=goal)

    class GoalVerifier:
        def verify_goal(self, *args, **kwargs):
            return GoalOutcome(
                "blocked", reason="尚未取得支持安全局部重规划的根因证据",
            )

    workflow = Workflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=Discovery(),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=workflow,
        verifier=GoalVerifier(),
    )

    result = coordinator.handle("confirm existing plan")

    assert result.kind == "awaiting_user_decision"
    assert workflow.failure["category"] == "post_execution_replan_not_grounded"
    assert workflow.failure["environment_changed"] == "unknown"
    assert workflow.failure["plan"] is failed_plan


def test_paused_plan_evidence_includes_bounded_execution_output():
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence
    from klonet_agent.ops.privileged.workflow.coordinator import (
        WorkflowResult, _paused_plan_evidence,
    )

    step = SimpleNamespace(
        step_id="stop-worker",
        status="paused",
        execution_attempts=1,
        observation="precondition changed",
        evidence=ExecutionEvidence(
            return_code=2,
            stdout="component_pid_state_drift",
            stderr="environment_changed=false",
            environment_changed=False,
        ),
        checks=[],
    )
    change = SimpleNamespace(
        step_id="restart-worker",
        status="paused",
        observation="implementation paused",
        implementation_plan=SimpleNamespace(steps=[step]),
    )
    plan = SimpleNamespace(
        plan_id="priv-ops-paused",
        status="paused",
        steps=[change],
    )

    rendered = _paused_plan_evidence(
        WorkflowResult(True, "paused", "step failed", plan=plan),
    )

    assert "execution_output=component_pid_state_drift environment_changed=false" in rendered


def test_authoritative_recovery_scope_reports_completed_component_effects():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, ImplementationPlan, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        _authoritative_recovery_scope,
    )

    change = ChangeStep(
        step_id="restart-alpha", title="restart alpha",
        objective="restart alpha roles", risk="medium",
        expected_changes=["restart all roles"],
        postconditions=[{"checker": "screen_session_exists", "args": {"session": "alpha_m"}}],
        status="paused",
        implementation_plan=ImplementationPlan(
            implementation_id="impl-alpha", semantic_step_id="restart-alpha",
            objective="restart alpha",
            steps=[
                PrivilegedStep(
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
                ),
                PrivilegedStep(
                    step_id="restart-alpha__worker", title="restart worker",
                    status="pending",
                ),
            ],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-alpha", goal="restart alpha", risk="medium",
        steps=[change],
        resources=[
            PlanResource(
                name="alpha_root", kind="path", status="frozen",
                role="instance_root", value="/srv/alpha",
                consumers=["restart-alpha.project_root"],
            ),
            PlanResource(
                name="alpha_identifier", kind="identifier", status="frozen",
                role="instance_identifier", value="stable-alpha",
                consumers=["restart-alpha.platform"],
            ),
        ],
        status="paused",
    )

    scope = _authoritative_recovery_scope(plan)

    assert scope["recovery_completed_components_by_root"] == {
        "/srv/alpha": ["master"],
    }
    assert scope["recovery_instance_identifiers_by_root"] == {
        "/srv/alpha": "stable-alpha",
    }


def test_unexecuted_draft_is_not_authoritative_recovery_state():
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        _authoritative_recovery_scope,
    )

    plan = ChangePlan(
        plan_id="priv-ops-draft", goal="restart all", risk="medium",
        steps=[ChangeStep(
            step_id="restart-old-alias", title="restart old alias",
            objective="restart /srv/test/vemu_uestc", risk="medium",
            expected_changes=["roles restart"],
            postconditions=[{"checker": "process_running", "args": {"pattern": "master"}}],
        )],
        resources=[PlanResource(
            name="old_identifier", kind="identifier", status="frozen",
            role="instance_identifier", value="vemu_uestc",
            consumers=["restart-old-alias.platform"],
        )],
        status="draft",
    )

    assert _authoritative_recovery_scope(plan) == {}


def test_unexecuted_aborted_plan_is_not_authoritative_recovery_state():
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        _authoritative_recovery_scope,
    )

    plan = ChangePlan(
        plan_id="priv-ops-aborted", goal="create platform", risk="medium",
        steps=[ChangeStep(
            step_id="create-platform", title="create platform",
            objective="create /srv/create_e2e", risk="medium",
            expected_changes=["new isolated platform"],
            postconditions=[{
                "checker": "file_exists",
                "args": {"path": "/srv/create_e2e"},
            }],
        )],
        resources=[PlanResource(
            name="instance_root", kind="path", status="frozen",
            role="instance_root", value="/srv/create_e2e",
            consumers=["create-platform.path"],
        )],
        status="aborted",
    )

    assert _authoritative_recovery_scope(plan) == {}


def test_attempted_aborted_plan_remains_authoritative_recovery_state():
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan, ChangeStep, PlanResource,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        _authoritative_recovery_scope,
    )

    plan = ChangePlan(
        plan_id="priv-ops-attempted", goal="create platform", risk="medium",
        steps=[ChangeStep(
            step_id="create-platform", title="create platform",
            objective="create /srv/create_e2e", risk="medium",
            expected_changes=["new isolated platform"],
            postconditions=[{
                "checker": "file_exists",
                "args": {"path": "/srv/create_e2e"},
            }],
            status="paused", execution_attempts=1,
        )],
        resources=[PlanResource(
            name="instance_root", kind="path", status="frozen",
            role="instance_root", value="/srv/create_e2e",
            consumers=["create-platform.path"],
        )],
        status="aborted",
    )

    assert _authoritative_recovery_scope(plan)[
        "recovery_required_project_roots"
    ] == ["/srv/create_e2e"]


def test_successful_plan_execution_requires_whole_goal_verification_before_completion():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, GoalOutcome,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )

    plan = SimpleNamespace(
        plan_id="priv-ops-verifying",
        goal="恢复目标平台的全部后端服务",
        status="verifying",
        steps=[],
    )

    class Workflow:
        def __init__(self):
            self.completed = []

        def handle_control(self, text):
            return WorkflowResult(
                True, "goal_verification", "step checks passed", plan=plan,
            )

        def complete_goal(self, completed_plan, outcome):
            assert completed_plan.status == "verifying"
            self.completed.append((completed_plan, outcome))
            completed_plan.status = "completed"
            return WorkflowResult(
                True, "completed", "目标已完成", plan=completed_plan,
                outcome=outcome,
            )

    class Discovery:
        def collect(
            self, goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            self.goal = goal
            return seed_bundle or EvidenceBundle(goal=goal)

        def collect_requests(self, requests, bundle):
            raise AssertionError("whole-goal evidence is already complete")

    class GoalVerifier:
        def __init__(self):
            self.calls = []

        def verify_goal(
            self, goal, bundle, conclusion, attempted_keys=None,
            phase="readonly", goal_kind="health_check",
        ):
            self.calls.append((goal, phase))
            return GoalOutcome("achieved", reason="完整目标已有证据支持")

    workflow = Workflow()
    verifier = GoalVerifier()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=Discovery(),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=workflow,
        verifier=verifier,
    )

    result = coordinator.handle(
        "confirm-priv-plan priv-ops-verifying " + "a" * 64,
    )

    assert result.kind == "completed"
    assert verifier.calls == [(plan.goal, "post_execution")]
    assert workflow.completed[0][1].status == "achieved"
    assert plan.status == "completed"


def test_post_execution_replan_failure_resumes_goal_verification_not_planning():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, EvidenceRecord, FailureRecord,
        GoalOutcome, ProbeRequest, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    goal = "把目标平台全部角色收编到 Screen"
    plan = SimpleNamespace(
        plan_id="priv-ops-executed", goal=goal, status="paused",
        steps=[], resources=[],
    )
    failure = FailureRecord(
        failure_id="failure-post-execution-replan",
        stage="planning",
        category="post_execution_replan_failure",
        summary="执行后局部重规划异常",
        technical_reason="replan changed completed candidate",
        goal=goal,
        goal_kind="execution",
        plan_id=plan.plan_id,
        selected_option_id="continue_current_goal",
        options=[RecoveryOption(
            option_id="continue_current_goal",
            label="继续处理",
            description="恢复原目标",
            action="continue_current_goal",
            recommended=True,
        )],
    )
    stale_plan = SimpleNamespace(
        plan_id="priv-ops-stale", goal=goal, status="paused",
        steps=[], resources=[],
    )
    stale_evidence = EvidenceBundle(goal=goal)
    stale_evidence.add(EvidenceRecord.from_probe(
        ProbeRequest(
            "plan_execution",
            {"plan_id": stale_plan.plan_id, "status": "paused"},
            "历史执行结果",
        ),
        "plan_id=priv-ops-stale status=paused "
        "step=old status=paused environment_changed=true",
    ))
    snapshot = OperationalContextSnapshot(
        resolved_goal=goal, base_goal=goal,
        active_plan_id=stale_plan.plan_id, evidence=stale_evidence,
    )

    class Workflow:
        def __init__(self):
            self.completed = []
            self.planned = False

        def handle_control(self, text):
            return WorkflowResult(
                True, "failure_option_selected", "selected", failure=failure,
            )

        def load_plan(self, plan_id):
            return {
                plan.plan_id: plan,
                stale_plan.plan_id: stale_plan,
            }[plan_id]

        def plan_once(self, *args, **kwargs):
            self.planned = True
            raise AssertionError("completed execution must not return to planning")

        def complete_goal(self, completed_plan, outcome):
            self.completed.append((completed_plan, outcome))
            completed_plan.status = "completed"
            return WorkflowResult(
                True, "completed", "目标已完成", plan=completed_plan,
                outcome=outcome,
            )

    class Discovery:
        def collect(
            self, requested_goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            assert "验证已审批任务是否已经达到完整用户目标" in requested_goal
            return seed_bundle or EvidenceBundle(goal=requested_goal)

        def collect_requests(self, requests, bundle):
            raise AssertionError("goal evidence is complete")

    class Verifier:
        def verify_goal(
            self, requested_goal, bundle, conclusion, attempted_keys=None,
            phase="readonly", goal_kind="health_check",
        ):
            assert requested_goal == goal
            assert phase == "post_execution"
            return GoalOutcome("achieved", reason="当前目标效果已满足")

    workflow = Workflow()

    class ContextStore:
        def load(self):
            return snapshot

        def save(self, value):
            self.saved = value

    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=Discovery(),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(), mutation_workflow=workflow,
        verifier=Verifier(), context_store=ContextStore(),
    )

    result = coordinator.handle("选择 1")

    assert result.kind == "completed"
    assert plan.status == "completed"
    assert workflow.planned is False
    assert workflow.completed[0][0] is plan
    assert stale_plan.status == "paused"


def test_workflow_coordinator_applies_goal_guard_to_mutating_intent():
    from klonet_agent.ops.privileged.intent import PrivilegedIntentDecision
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    class NoCall:
        pass

    class MutatingClassifier:
        def classify(self, *args, **kwargs):
            return PrivilegedIntentDecision(
                intent="mutating_action", requires_execution=True,
                goal_kind="execution",
            )

    class Workflow:
        handle_control = None

    coordinator = PrivilegedOpsCoordinator(
        classifier=MutatingClassifier(),
        discovery=NoCall(),
        synthesis=NoCall(),
        response=NoCall(),
        mutation_workflow=Workflow(),
        verifier=NoCall(),
    )

    result = coordinator.handle("rm -rf / and delete all system files")

    assert result.kind == "denied"
    assert result.handled is True


def test_dangerous_command_discussion_is_not_rejected_as_execution():
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle

    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=StubDiscovery(EvidenceBundle(goal="unused")),
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("为什么 rm -rf / 会被安全策略禁止？")

    assert result.handled is False
    assert result.kind == "conversation"


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
        classifier=StubClassifier(
            "mutating_action", goal_relation="continue_previous",
            goal_kind="execution", operation="restart", scope="component",
            components=("master", "worker"),
        ),
        discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
        context_store=store,
    )

    result = coordinator.handle("提交这个重启计划走审批流程")

    assert result.kind == "awaiting_confirmation"
    assert discovery.calls[0][0].endswith(
        "帮我重启 v4e2e 的 master 和 worker"
    )
    assert mutation.calls[0][0][0] == "帮我重启 v4e2e 的 master 和 worker"
    assert store.saved[-1].active_plan_id == "priv-ops-test"


def test_self_directed_followup_reuses_previous_diagnostic_goal():
    from klonet_agent.ops.privileged.workflow.operational_context import OperationalContextSnapshot
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator

    snapshot = OperationalContextSnapshot(
        resolved_goal="检查 v4e2e_m 为什么报错",
        target_roots=["/home/lzl/klonet_workflow_e2e"],
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
    response = StubResponse()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("readonly_action", goal_relation="refine_previous"),
        discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=response,
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
    rendered_goal = response.calls[0][0]
    assert rendered_goal == "v4e2e_m 现在是不是有报错；进一步要求：这个报错是为啥啊"
    assert not rendered_goal.startswith("只读诊断并补齐")


@pytest.mark.parametrize(
    ("current_request", "decision_kwargs"),
    [
        (
            "这四个平台哪些正常运行？",
            {
                "goal_kind": "health_check",
                "operation": "inspect",
                "scope": "component",
                "components": ("vemu_uestc", "klonet_worker_105", "test"),
            },
        ),
        (
            "它们为什么有两个异常？",
            {
                "goal_kind": "causal_diagnosis",
                "operation": "inspect",
                "scope": "platform",
            },
        ),
    ],
)
def test_typed_semantic_change_cannot_be_erased_by_coarse_continuation(
    current_request, decision_kwargs,
):
    """A coarse relation may inherit the target, never discard a new result."""

    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    previous_goal = "查看机器上有哪些 Klonet 平台在跑"
    snapshot = OperationalContextSnapshot(
        resolved_goal=previous_goal,
        workflow_intent="readonly_action",
        goal_kind="health_check",
        operation="inspect",
        scope="platform",
        evidence=EvidenceBundle(goal=previous_goal),
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

    classifier = StubClassifier(
        "readonly_action",
        goal_relation="continue_previous",
        **decision_kwargs,
    )
    discovery = Discovery()
    response = StubResponse()
    coordinator = PrivilegedOpsCoordinator(
        classifier=classifier,
        discovery=discovery,
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=response,
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle(current_request)

    assert result.kind == "completed"
    assert previous_goal in discovery.goals[0]
    assert current_request in discovery.goals[0]
    assert current_request in response.calls[0][0]
    classifier_context = classifier.calls[0][1]
    assert "previous_goal_kind=health_check" in classifier_context
    assert "previous_operation=inspect" in classifier_context
    assert "previous_scope=platform" in classifier_context


@pytest.mark.parametrize("retry_text", ["重新试试", "再执行一次检查", "接着查"])
def test_continuation_persists_exact_resolved_goal_before_discovery(retry_text):
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    previous_goal = "列出当前服务器上正在运行的所有平台"
    snapshot = OperationalContextSnapshot(
        resolved_goal=previous_goal,
        evidence=EvidenceBundle(goal=previous_goal),
    )

    class Store:
        def __init__(self):
            self.saved = []

        def load(self):
            return snapshot

        def save(self, value):
            self.saved.append(value)

    class CrashingDiscovery:
        def collect(
            self, goal, *, command="", conversation_context="",
            seed_bundle=None, preload_capabilities=False,
        ):
            raise RuntimeError("simulated discovery crash")

    store = Store()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "readonly_action", goal_relation="continue_previous",
            goal_kind="health_check",
        ),
        discovery=CrashingDiscovery(),
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=store,
    )

    result = coordinator.handle(retry_text)

    assert result.kind == "awaiting_user_decision"
    assert "simulated discovery crash" in result.message
    assert coordinator.mutation_workflow.failures[0]["stage"] == "discovery"
    assert store.saved
    assert store.saved[0].resolved_goal == previous_goal
    assert store.saved[0].evidence.goal == previous_goal
    assert store.saved[0].workflow_intent == "readonly_action"
    assert store.saved[0].goal_kind == "health_check"


def test_privileged_intent_contract_marks_causal_followup_as_refinement():
    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    decision = PrivilegedIntentClassifier._decision({
        "intent": "readonly_action",
        "goal_clarity": "discoverable",
        "goal_relation": "refine_previous",
        "confidence": 1,
    })

    assert decision.goal_relation == "refine_previous"


def test_failure_option_reenters_discovery_without_injecting_planning_strategy():
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
        evidence=reusable,
    )
    failure = FailureRecord(
        failure_id="failure-restart-loop",
        stage="binding",
        category="implementation_contract_invalid",
        summary="原子步骤无法落地",
        technical_reason="component contract missing",
        goal=goal,
        goal_kind="execution",
        plan_id="priv-ops-old",
        evidence_requests=[ProbeRequest(
            "process_detail", {"process_keywords": ["master_main"]},
            "补齐 master 运行身份",
            ("master cwd", "master python executable"),
        )],
        selected_option_id="continue_current_goal",
        options=[RecoveryOption(
            option_id="continue_current_goal",
            label="继续处理",
            description="刷新运行证据并返回原主循环",
            action="continue_current_goal",
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

        def load_plan(self, plan_id):
            assert plan_id == "priv-ops-old"
            return SimpleNamespace(
                plan_id=plan_id, goal=goal, status="paused",
                steps=[], resources=[],
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
        def __init__(self):
            self.targeted = []

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

        def collect_requests(self, requests, bundle):
            self.targeted.extend(requests)
            bundle.add(EvidenceRecord.from_probe(
                ProbeRequest(
                    "readonly_command",
                    {"command": "ps -p 10 -o args="},
                    "补齐 master 运行身份",
                    requests[0].required_facts,
                    requests[0].freshness,
                ),
                "/opt/python3.8 -m gunicorn master_main:flask_app",
            ))
            return bundle

    class Store:
        def load(self):
            return snapshot

        def save(self, value):
            self.saved = value

    mutation = ControlMutation()
    discovery = RefreshDiscovery()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"),
        discovery=discovery,
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
    assert any(
        item.request.probe == "plan_execution" for item in refreshed.records
    )
    assert intent["operation"] == "none"
    assert intent["scope"] == "none"
    assert "planning_strategy" not in intent
    assert intent["components"] == []
    assert intent["recovery_failure_stage"] == "binding"
    assert intent["recovery_failure_category"] == "implementation_contract_invalid"
    assert intent["recovery_technical_reason"] == "component contract missing"
    assert intent["rejected_evidence_need_keys"] == [
        failure.evidence_requests[0].need_key
    ]
    assert intent["resolved_project_root"] == "/home/lzl/klonet_v4_e2e"
    assert discovery.targeted[0].freshness == "refresh"
    assert discovery.targeted[0].required_facts == (
        "master cwd", "master python executable",
    )


def test_failure_direction_is_recorded_as_decision_without_rewriting_base_goal():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, FailureRecord, GoalOutcome,
        RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )

    goal = "把 test 平台组件收编进 Screen"
    failure = FailureRecord(
        failure_id="failure-direction-route",
        stage="binding",
        category="binding_contract_missing",
        summary="需要调整目标范围",
        technical_reason="component boundary unresolved",
        goal=goal,
        goal_kind="execution",
        options=[RecoveryOption(
            "provide_direction", "调整目标或处理范围", "补充边界",
            "provide_direction",
        )],
        selected_option_id="provide_direction",
    )

    class ForbiddenClassifier:
        def classify(self, *args, **kwargs):
            raise AssertionError("边界补充不得重新进入普通分类")

    class Workflow:
        max_replanning_rounds = 1
        max_candidate_replans = 1
        planner = SimpleNamespace()

        def __init__(self):
            self.intent_context = None
            self.submitted_goal = ""

        def handle_control(self, text):
            return WorkflowResult(
                True, "failure_direction_provided", text, failure=failure,
            )

        def plan_once(self, submitted_goal, **kwargs):
            self.submitted_goal = submitted_goal
            self.intent_context = kwargs["intent_context"]
            return GoalOutcome(
                "need_execution",
                plan=SimpleNamespace(plan_id="priv-ops-revised", status="draft"),
            )

        def bind_once(self, plan, *, evidence_bundle):
            return GoalOutcome(
                "needs_user_decision", user_question="revised plan", plan=plan,
            )

    class Discovery:
        def collect(self, requested_goal, **kwargs):
            return EvidenceBundle(goal=requested_goal)

    workflow = Workflow()
    coordinator = PrivilegedOpsCoordinator(
        classifier=ForbiddenClassifier(),
        discovery=Discovery(),
        synthesis=StubSynthesis(EvidenceConclusion()),
        response=StubResponse(),
        mutation_workflow=workflow,
        verifier=StubGoalVerifier(),
    )

    result = coordinator.handle("保留 master/worker，排除 web_terminal")

    assert result.kind == "awaiting_confirmation"
    assert workflow.submitted_goal == goal
    assert workflow.intent_context["base_goal"] == goal
    assert workflow.intent_context["decision_history"] == [
        "保留 master/worker，排除 web_terminal"
    ]
    assert workflow.intent_context["recovery_failure_category"] == (
        "binding_contract_missing"
    )


def test_recovery_reply_that_replaces_scope_requires_explicit_goal_confirmation():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, FailureRecord, RecoveryOption,
    )
    from klonet_agent.ops.privileged.workflow.coordinator import (
        PrivilegedOpsCoordinator, WorkflowResult,
    )
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    base_goal = "把所有平台的所有角色收编进 screen"
    failure = FailureRecord(
        failure_id="failure-goal-revision", stage="binding",
        category="binding_contract_missing", summary="worker contract missing",
        technical_reason="runtime identity missing", goal=base_goal,
        goal_kind="execution", selected_option_id="provide_direction",
        options=[RecoveryOption(
            "provide_direction", "调整目标或处理范围", "补充边界",
            "provide_direction",
        )],
    )

    class Workflow:
        def handle_control(self, text):
            return WorkflowResult(
                True, "failure_direction_provided", text, failure=failure,
            )

        def classify_recovery_reply(self, **kwargs):
            return {
                "relation": "revise",
                "reason": "explicitly excludes non-test platforms",
                "normalized_decision": "只处理 test",
                "candidate_base_goal": "只把 test 平台所有角色收编进 screen",
                "conflicts": ["排除其他平台"],
            }

        def plan_once(self, *args, **kwargs):
            raise AssertionError("goal replacement must be confirmed before planning")

    class Store:
        def __init__(self):
            self.snapshot = OperationalContextSnapshot(
                resolved_goal=base_goal, base_goal=base_goal,
                evidence=EvidenceBundle(goal=base_goal),
            )

        def load(self):
            return self.snapshot

        def save(self, snapshot):
            self.snapshot = snapshot

    store = Store()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier("conversation"), discovery=SimpleNamespace(),
        synthesis=SimpleNamespace(), response=StubResponse(),
        mutation_workflow=Workflow(), verifier=StubGoalVerifier(),
        context_store=store,
    )

    result = coordinator.handle("其他平台都取消，只处理 test")

    assert result.kind == "clarification"
    assert "确认覆盖目标" in result.message
    assert store.snapshot.base_goal == base_goal
    assert store.snapshot.pending_goal_revision == "只把 test 平台所有角色收编进 screen"


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
        ProbeRequest(
            "process", {"keywords": ["app"]}, "runtime",
            ("pid", "cwd", "full cmdline"), "refresh",
        ),
        "pid=stale",
    ))
    store = OperationalContextStore(
        tmp_path, user_id="lzl", project_id="v4e2e",
    )
    store.save(OperationalContextSnapshot(
        resolved_goal="帮我重启 v4e2e 的 master 和 worker",
        active_plan_id="priv-ops-draft",
        workflow_intent="mutating_action",
        goal_kind="execution",
        operation="restart",
        scope="component",
        components=["master", "worker"],
        output_locale="zh-CN",
        target_roots=["/srv/app"],
        evidence=bundle,
    ))

    restored = store.load()

    assert restored is not None
    assert restored.resolved_goal == "帮我重启 v4e2e 的 master 和 worker"
    assert restored.active_plan_id == "priv-ops-draft"
    assert restored.workflow_intent == "mutating_action"
    assert restored.goal_kind == "execution"
    assert restored.operation == "restart"
    assert restored.scope == "component"
    assert restored.components == ["master", "worker"]
    process = next(
        item for item in restored.evidence.records
        if item.request.probe == "process"
    )
    assert process.request.required_facts == ("pid", "cwd", "full cmdline")
    assert process.request.freshness == "refresh"
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


@pytest.mark.parametrize("query", [
    "重新查看当前服务器上有哪些 Klonet 平台在运行",
    "盘点一下现在活着的 Klonet 实例",
    "列出当前后端健康的平台",
])
def test_new_runtime_inventory_goal_cannot_be_hijacked_by_active_change_plan(query):
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    bundle, conclusion = _evidence()
    snapshot = OperationalContextSnapshot(
        resolved_goal="帮我重启 v4_e2e 平台",
        active_plan_id="priv-ops-old-restart",
        evidence=EvidenceBundle(goal="帮我重启 v4_e2e 平台"),
    )

    class Store:
        def __init__(self):
            self.saved = []

        def load(self):
            return snapshot

        def save(self, value):
            self.saved.append(value)

    class NoPlanLookup(NoMutationWorkflow):
        def render_plan_status(self, plan_id):
            raise AssertionError("environment inspection must not read a plan")

    discovery = StubDiscovery(bundle)
    store = Store()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "readonly_action", goal_relation="new",
            goal_kind="health_check", operation="inspect",
        ),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=NoPlanLookup(),
        verifier=StubGoalVerifier(),
        context_store=store,
    )

    result = coordinator.handle(query)

    assert result.kind == "completed"
    assert discovery.calls[0][0] == query
    assert store.saved[-1].resolved_goal == ""
    assert store.saved[-1].base_goal == ""
    assert store.saved[-1].decision_history == []
    assert store.saved[-1].active_plan_id == ""


def test_correction_supersedes_old_plan_and_runs_replacement_readonly_goal():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    bundle, conclusion = _evidence()
    snapshot = OperationalContextSnapshot(
        resolved_goal="帮我重启 v4_e2e 平台",
        active_plan_id="priv-ops-old-restart",
        evidence=EvidenceBundle(goal="帮我重启 v4_e2e 平台"),
    )

    class Store:
        def __init__(self):
            self.saved = []

        def load(self):
            return snapshot

        def save(self, value):
            self.saved.append(value)

    class AbortableWorkflow(NoMutationWorkflow):
        def __init__(self):
            super().__init__()
            self.aborted = []

        def abort_plan(self, plan_id):
            self.aborted.append(plan_id)
            return True

    mutation = AbortableWorkflow()
    discovery = StubDiscovery(bundle)
    store = Store()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "readonly_action", goal_relation="supersede_previous",
            goal_kind="health_check", operation="inspect",
        ),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
        context_store=store,
    )

    request = "不是重启平台，只是帮我看看有哪些平台在运行"
    result = coordinator.handle(request)

    assert result.kind == "completed"
    assert mutation.aborted == ["priv-ops-old-restart"]
    assert discovery.calls[0][0] == request
    assert store.saved[-1].resolved_goal == ""
    assert store.saved[-1].base_goal == ""
    assert store.saved[-1].decision_history == []
    assert store.saved[-1].active_plan_id == ""


def test_bare_rejection_aborts_old_plan_without_discovery_or_verifier():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="帮我重启 v4_e2e 平台",
        active_plan_id="priv-ops-old-restart",
        evidence=EvidenceBundle(goal="帮我重启 v4_e2e 平台"),
    )

    class Store:
        def __init__(self):
            self.saved = []

        def load(self):
            return snapshot

        def save(self, value):
            self.saved.append(value)

    class AbortableWorkflow(NoMutationWorkflow):
        def __init__(self):
            super().__init__()
            self.aborted = []

        def abort_plan(self, plan_id):
            self.aborted.append(plan_id)
            return True

    class ForbiddenDiscovery:
        def collect(self, *args, **kwargs):
            raise AssertionError("bare rejection must not enter Discovery")

    mutation = AbortableWorkflow()
    store = Store()
    coordinator = PrivilegedOpsCoordinator(
        classifier=StubClassifier(
            "conversation", goal_relation="supersede_previous",
        ),
        discovery=ForbiddenDiscovery(),
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=mutation,
        verifier=StubGoalVerifier(),
        context_store=store,
    )

    result = coordinator.handle("不是重启平台啊")

    assert result.kind == "aborted"
    assert mutation.aborted == ["priv-ops-old-restart"]
    assert store.saved[-1].resolved_goal == ""
    assert store.saved[-1].active_plan_id == ""


def test_unclear_supersession_never_aborts_before_user_confirms_meaning():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="帮我重启 v4_e2e 平台",
        active_plan_id="priv-ops-old-restart",
        evidence=EvidenceBundle(goal="帮我重启 v4_e2e 平台"),
    )
    classifier = StubClassifier(
        "ambiguous", goal_relation="supersede_previous",
    )
    classifier.decision.should_clarify = True
    classifier.decision.clarification_question = "你是要取消旧计划吗？"

    class Store:
        def load(self):
            return snapshot

    class NoAbortWorkflow(NoMutationWorkflow):
        def abort_plan(self, plan_id):
            raise AssertionError("unclear intent must not abort a plan")

    coordinator = PrivilegedOpsCoordinator(
        classifier=classifier,
        discovery=StubDiscovery(EvidenceBundle(goal="")),
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=NoAbortWorkflow(),
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle("这个不要了，换一下")

    assert result.kind == "clarification"
    assert snapshot.active_plan_id == "priv-ops-old-restart"


@pytest.mark.parametrize(
    "payload, error",
    [
        ({
            "intent": "resume_plan", "operation": "inspect",
            "plan_reference": "",
        }, "resume_plan requires plan_reference"),
        ({
            "intent": "conversation", "goal_kind": "status_query",
        }, "invalid goal_kind"),
        ({
            "intent": "conversation", "operation": "inspect",
        }, "conversation cannot request an operation"),
    ],
)
def test_intent_contract_rejects_plan_and_environment_status_overlap(payload, error):
    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    with pytest.raises(ValueError, match=error):
        PrivilegedIntentClassifier._decision(payload)


@pytest.mark.parametrize("intent", ["readonly_action", "mutating_action"])
def test_non_resume_intent_discards_redundant_plan_reference(intent):
    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    decision = PrivilegedIntentClassifier._decision({
        "intent": intent,
        "goal_relation": "refine_previous",
        "goal_kind": "health_check" if intent == "readonly_action" else "execution",
        "operation": "inspect" if intent == "readonly_action" else "restart",
        "plan_reference": "latest",
    })

    assert decision.intent == intent
    assert decision.goal_relation == "refine_previous"
    assert decision.plan_reference == ""


def test_resume_plan_keeps_its_explicit_reference():
    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    decision = PrivilegedIntentClassifier._decision({
        "intent": "resume_plan",
        "goal_relation": "continue_previous",
        "operation": "inspect",
        "plan_reference": "priv-ops-existing",
    })

    assert decision.plan_reference == "priv-ops-existing"


def test_intent_contract_accepts_semantic_supersession_without_new_decision_type():
    from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier

    decision = PrivilegedIntentClassifier._decision({
        "intent": "readonly_action",
        "goal_relation": "supersede_previous",
        "goal_kind": "health_check",
        "operation": "inspect",
        "plan_reference": "",
    })

    assert decision.goal_relation == "supersede_previous"
    assert decision.intent == "readonly_action"


def test_intent_prompt_routes_observation_discrepancy_to_existing_readonly_loop():
    from klonet_agent.ops.privileged.intent import INTENT_CLASSIFIER_SYSTEM_PROMPT

    assert "challenges a previous operational conclusion" in (
        INTENT_CLASSIFIER_SYSTEM_PROMPT
    )
    assert "readonly_action" in INTENT_CLASSIFIER_SYSTEM_PROMPT
    assert "causal_diagnosis" in INTENT_CLASSIFIER_SYSTEM_PROMPT


def test_intent_prompt_treats_revision_history_question_as_plan_reconciliation():
    from klonet_agent.ops.privileged.intent import INTENT_CLASSIFIER_SYSTEM_PROMPT

    assert "我之前不是修订过这个计划了吗" in INTENT_CLASSIFIER_SYSTEM_PROMPT
    assert "reconcile conversation history with persisted plan state" in (
        INTENT_CLASSIFIER_SYSTEM_PROMPT
    )


def test_classifier_receives_authoritative_plan_context_before_routing():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
    from klonet_agent.ops.privileged.workflow.operational_context import (
        OperationalContextSnapshot,
    )

    snapshot = OperationalContextSnapshot(
        resolved_goal="重启 test 平台",
        active_plan_id="priv-ops-old",
        evidence=EvidenceBundle(goal="重启 test 平台"),
    )

    class Store:
        def load(self):
            return snapshot

    class Workflow(NoMutationWorkflow):
        def plan_context(self, **kwargs):
            assert kwargs == {
                "active_plan_id": "priv-ops-old", "goal": "重启 test 平台",
            }
            return "plan_id=priv-ops-old status=awaiting_confirmation contract=invalid"

    classifier = StubClassifier("conversation", goal_relation="refine_previous")
    coordinator = PrivilegedOpsCoordinator(
        classifier=classifier,
        discovery=StubDiscovery(EvidenceBundle(goal="unused")),
        synthesis=StubSynthesis(None),
        response=StubResponse(),
        mutation_workflow=Workflow(),
        verifier=StubGoalVerifier(),
        context_store=Store(),
    )

    result = coordinator.handle(
        "我之前不是修订过这个计划了吗",
        conversation_context="之前讨论过三个修订要求",
    )

    assert result.handled is False
    classifier_context = classifier.calls[0][1]
    assert "之前讨论过三个修订要求" in classifier_context
    assert "status=awaiting_confirmation contract=invalid" in classifier_context
