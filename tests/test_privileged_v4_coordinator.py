from __future__ import annotations

from types import SimpleNamespace


class StubClassifier:
    def __init__(self, intent, command="", goal_clarity="clear"):
        self.decision = SimpleNamespace(
            intent=intent,
            command=command,
            goal_clarity=goal_clarity,
            should_clarify=False,
            clarification_question="",
            reason="test",
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


class NoMutationWorkflow:
    def __init__(self):
        self.calls = []

    def submit(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("readonly request must not enter mutation workflow")


def _evidence():
    from klonet_agent.ops.privileged.v4.contracts import (
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
    from klonet_agent.ops.privileged.v4.coordinator import PrivilegedOpsV4Coordinator

    bundle, conclusion = _evidence()
    classifier = StubClassifier("readonly_action")
    discovery = StubDiscovery(bundle)
    synthesis = StubSynthesis(conclusion)
    response = StubResponse()
    mutation = NoMutationWorkflow()
    coordinator = PrivilegedOpsV4Coordinator(
        classifier=classifier,
        discovery=discovery,
        synthesis=synthesis,
        response=response,
        mutation_workflow=mutation,
    )

    result = coordinator.handle("检查下现在服务器上有哪些平台")

    assert result.handled is True
    assert result.kind == "completed"
    assert result.message == "发现 3 个候选平台。"
    assert discovery.calls == [("检查下现在服务器上有哪些平台", "", "")]
    assert synthesis.calls == [("检查下现在服务器上有哪些平台", bundle)]
    assert response.calls == [("检查下现在服务器上有哪些平台", conclusion)]
    assert mutation.calls == []


def test_readonly_command_is_collected_as_evidence_not_executed_as_plan():
    from klonet_agent.ops.privileged.v4.coordinator import PrivilegedOpsV4Coordinator

    bundle, conclusion = _evidence()
    discovery = StubDiscovery(bundle)
    coordinator = PrivilegedOpsV4Coordinator(
        classifier=StubClassifier("readonly_action", command="python3 -V"),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
    )

    result = coordinator.handle("查看 Python 版本")

    assert result.kind == "completed"
    assert discovery.calls == [("查看 Python 版本", "python3 -V", "")]


def test_conversation_bypasses_privileged_discovery_and_execution():
    from klonet_agent.ops.privileged.v4.coordinator import PrivilegedOpsV4Coordinator

    bundle, conclusion = _evidence()
    discovery = StubDiscovery(bundle)
    coordinator = PrivilegedOpsV4Coordinator(
        classifier=StubClassifier("conversation"),
        discovery=discovery,
        synthesis=StubSynthesis(conclusion),
        response=StubResponse(),
        mutation_workflow=NoMutationWorkflow(),
    )

    result = coordinator.handle("什么是 Klonet？")

    assert result.handled is False
    assert result.kind == "conversation"
    assert discovery.calls == []


def test_v4_coordinator_handles_exact_control_before_classifier_or_discovery():
    from klonet_agent.ops.privileged.v4.coordinator import (
        PrivilegedOpsV4Coordinator,
        V4WorkflowResult,
    )

    class NoCall:
        def __getattr__(self, name):
            raise AssertionError("component must not be called: %s" % name)

    class Controls:
        def __init__(self):
            self.calls = []

        def handle_control(self, text):
            self.calls.append(text)
            return V4WorkflowResult(True, "completed", "confirmed")

    controls = Controls()
    coordinator = PrivilegedOpsV4Coordinator(
        classifier=NoCall(),
        discovery=NoCall(),
        synthesis=NoCall(),
        response=NoCall(),
        mutation_workflow=controls,
    )

    result = coordinator.handle_with_context(
        "confirm-priv-v4 priv-v4-flow " + "a" * 64,
        environment_context="ignored",
        conversation_context="recent",
    )

    assert result.kind == "completed"
    assert controls.calls == ["confirm-priv-v4 priv-v4-flow " + "a" * 64]


def test_v4_coordinator_applies_goal_guard_before_any_discovery():
    from klonet_agent.ops.privileged.v4.coordinator import PrivilegedOpsV4Coordinator

    class NoCall:
        def __getattr__(self, name):
            raise AssertionError("component must not be called: %s" % name)

    coordinator = PrivilegedOpsV4Coordinator(
        classifier=NoCall(),
        discovery=NoCall(),
        synthesis=NoCall(),
        response=NoCall(),
        mutation_workflow=NoCall(),
    )

    result = coordinator.handle("rm -rf / and delete all system files")

    assert result.kind == "denied"
    assert result.handled is True
