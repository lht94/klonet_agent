from __future__ import annotations

import pytest


def test_probe_request_cache_key_is_stable_across_argument_order():
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest

    left = ProbeRequest("ports", {"ports": [45551], "host": "127.0.0.1"}, "check")
    right = ProbeRequest("ports", {"host": "127.0.0.1", "ports": [45551]}, "again")

    assert left.cache_key == right.cache_key


def test_evidence_bundle_reuses_identical_probe_result():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )

    request = ProbeRequest("screen", {}, "discover sessions")
    bundle = EvidenceBundle(goal="list platforms")

    first = bundle.add(EvidenceRecord.from_probe(request, "screen-a"))
    second = bundle.add(EvidenceRecord.from_probe(request, "new output must not replace cached evidence"))

    assert first.evidence_id == second.evidence_id
    assert bundle.records == [first]
    assert bundle.records[0].output == "screen-a"


def test_evidence_conclusion_rejects_unknown_evidence_reference():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceClaim,
        EvidenceConclusion,
    )

    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim("three platforms", ["ev-missing"])],
    )

    with pytest.raises(ValueError, match="unknown evidence reference"):
        conclusion.validate_against(EvidenceBundle(goal="list platforms"))


def test_diagnosis_assessment_requires_complete_causal_chain():
    from klonet_agent.ops.privileged.workflow.contracts import DiagnosisAssessment

    with pytest.raises(ValueError, match="failure_point"):
        DiagnosisAssessment(
            status="cause_confirmed",
            symptom="worker 初始化失败",
            root_cause="日志目录不存在",
            evidence_refs=["ev-1"],
        )


def test_discovery_budget_rejects_too_many_or_repeated_rounds():
    from klonet_agent.ops.privileged.workflow.contracts import (
        DiscoveryBudget,
        DiscoveryBudgetExceeded,
        ProbeRequest,
    )

    budget = DiscoveryBudget(max_rounds=1, max_per_round=2, max_total_probes=2)
    first = ProbeRequest("screen", {}, "screen")
    duplicate = ProbeRequest("screen", {}, "same probe")

    assert budget.register_round([first, duplicate]) == [first]
    with pytest.raises(DiscoveryBudgetExceeded, match="round budget"):
        budget.register_round([ProbeRequest("ports", {}, "ports")])


@pytest.mark.parametrize(
    ("risk", "expected_changes", "message"),
    [
        ("readonly", ["file changes"], "cannot be readonly"),
        ("medium", [], "expected_changes"),
    ],
)
def test_change_step_requires_a_real_host_change(risk, expected_changes, message):
    from klonet_agent.ops.privileged.workflow.contracts import ChangeStep

    with pytest.raises(ValueError, match=message):
        ChangeStep(
            step_id="change-config",
            title="change config",
            objective="change config",
            risk=risk,
            expected_changes=expected_changes,
            postconditions=[{"checker": "file_exists", "args": {"path": "/srv/app"}}],
        )


def test_change_step_requires_observable_postconditions():
    from klonet_agent.ops.privileged.workflow.contracts import ChangeStep

    with pytest.raises(ValueError, match="postconditions"):
        ChangeStep(
            step_id="change-config",
            title="change config",
            objective="change config",
            risk="medium",
            expected_changes=["config changes"],
            postconditions=[],
        )


def test_workflow_package_exports_mutation_workflow_components():
    from klonet_agent.ops.privileged.workflow import (
        ChangeBinder,
        ChangePlannerAgent,
        MutationWorkflow,
        ChangePlanStore,
    )

    assert all(
        item is not None
        for item in (
            ChangeBinder,
            ChangePlannerAgent,
            MutationWorkflow,
            ChangePlanStore,
        )
    )


def test_canonical_workflow_has_no_versioned_public_api():
    import importlib.util

    from klonet_agent.ops.privileged.workflow import (
        ChangeBinder,
        ChangePlanStore,
        ChangePlannerAgent,
        MutationWorkflow,
    )

    retired_package = "klonet_agent.ops.privileged." + "v" + str(4)
    retired_marker = "V" + str(4)

    assert importlib.util.find_spec(retired_package) is None
    assert all(
        retired_marker not in component.__name__
        for component in (
            ChangeBinder,
            ChangePlanStore,
            ChangePlannerAgent,
            MutationWorkflow,
        )
    )


def test_failure_record_round_trips_user_recovery_options():
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )

    failure = FailureRecord(
        failure_id="failure-binding1",
        stage="binding",
        category="unsafe_target_scope",
        summary="停止动作缺少根目录约束",
        technical_reason="project_root consumer missing",
        options=[RecoveryOption(
            option_id="component_restart",
            label="逐组件重启",
            description="按根目录逐组件重启",
            action="component_restart",
            recommended=True,
        )],
        goal="重启平台",
        plan_id="priv-ops-plan1",
    )

    restored = FailureRecord.from_dict(failure.to_dict())

    assert restored == failure
    assert restored.options[0].recommended is True
