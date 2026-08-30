from __future__ import annotations

import pytest


def test_probe_request_cache_key_is_stable_across_argument_order():
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest

    left = ProbeRequest("ports", {"ports": [45551], "host": "127.0.0.1"}, "check")
    right = ProbeRequest("ports", {"host": "127.0.0.1", "ports": [45551]}, "again")

    assert left.cache_key == right.cache_key


def test_evidence_gap_identity_does_not_change_with_probe_wording():
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest

    left = ProbeRequest(
        "process_detail", {"pid": 42}, "locate runtime",
        ("worker cwd",), "refresh", "gap-worker-runtime",
        ("restart-worker",),
    )
    right = ProbeRequest(
        "screen_session", {"session": "test_w"}, "locate same runtime",
        ("worker process tree and executable",), "refresh",
        "gap-worker-runtime", ("restart-worker",),
    )

    assert left.cache_key != right.cache_key
    assert left.need_key == right.need_key == "gap-worker-runtime"


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


def test_goal_outcome_is_the_only_user_level_transition_contract():
    from klonet_agent.ops.privileged.workflow.contracts import GoalOutcome

    assert GoalOutcome("achieved").status == "achieved"
    with pytest.raises(ValueError, match="need_execution requires plan"):
        GoalOutcome("need_execution")
    with pytest.raises(ValueError, match="needs_user_decision requires"):
        GoalOutcome("needs_user_decision")


def test_change_plan_does_not_duplicate_user_decision_state():
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    step = ChangeStep(
        step_id="restart-component",
        title="restart component",
        objective="restart one component",
        risk="medium",
        expected_changes=["component restarts"],
        postconditions=[{"checker": "process_running", "args": {"pattern": "worker"}}],
    )

    with pytest.raises(ValueError, match="invalid change plan status"):
        ChangePlan(
            plan_id="priv-ops-invariant",
            goal="restart worker",
            risk="medium",
            steps=[step],
            status="awaiting_user_decision",
        )


def test_change_plan_merges_equivalent_resource_declarations():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    step = ChangeStep(
        step_id="restart-worker", title="restart worker",
        objective="restart worker", risk="medium",
        expected_changes=["worker restarts"],
        postconditions=[{"checker": "process_running", "args": {"pattern": "worker"}}],
    )
    shared = dict(
        name="project_root", kind="path", status="frozen",
        role="project_root", value="/srv/test", source="ev-runtime",
    )
    plan = ChangePlan(
        plan_id="priv-ops-resource-merge", goal="restart worker",
        risk="medium", steps=[step], resources=[
            PlanResource(**shared, consumers=["restart-worker.project_root"]),
            PlanResource(**shared, consumers=["restart-worker.cwd"]),
        ],
    )

    assert len(plan.resources) == 1
    assert plan.resources[0].consumers == [
        "restart-worker.project_root", "restart-worker.cwd",
    ]


def test_change_plan_rejects_conflicting_duplicate_resource_values():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    step = ChangeStep(
        step_id="restart-worker", title="restart worker",
        objective="restart worker", risk="medium",
        expected_changes=["worker restarts"],
        postconditions=[{"checker": "process_running", "args": {"pattern": "worker"}}],
    )
    with pytest.raises(ValueError, match="conflicting duplicate plan resource"):
        ChangePlan(
            plan_id="priv-ops-resource-conflict", goal="restart worker",
            risk="medium", steps=[step], resources=[
                PlanResource(
                    name="worker_port", kind="port", status="frozen",
                    role="worker_port", value=45555, source="ev-a",
                ),
                PlanResource(
                    name="worker_port", kind="port", status="frozen",
                    role="worker_port", value=47002, source="ev-a",
                ),
            ],
        )


def test_component_port_contract_is_role_generic_without_fixed_port_aliases():
    from klonet_agent.ops.privileged.contracts import component_port_arg

    assert component_port_arg({"future_role_port": "54321"}, "future_role") == 54321
    assert component_port_arg({"master_port": 45551}, "master") == 45551
    assert component_port_arg({"port_47001": 47001}, "master") is None
    assert component_port_arg({"future_role_port": 70000}, "future_role") is None


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
            option_id="provide_direction",
            label="调整目标或处理范围",
            description="补充边界并返回原工作流",
            action="provide_direction",
            recommended=True,
        )],
        selected_option_id="provide_direction",
        user_direction="保留 master 和 worker，排除 web_terminal",
        goal="重启平台",
        goal_kind="execution",
        plan_id="priv-ops-plan1",
        missing_decisions=["是否允许停止并重启 worker"],
    )

    restored = FailureRecord.from_dict(failure.to_dict())

    assert restored == failure
    assert restored.options[0].recommended is True
    assert restored.user_direction == "保留 master 和 worker，排除 web_terminal"
    assert restored.missing_decisions == ["是否允许停止并重启 worker"]


def test_recovery_contract_rejects_planner_strategy_as_user_control():
    from klonet_agent.ops.privileged.workflow.contracts import RecoveryOption

    with pytest.raises(ValueError, match="invalid recovery option action"):
        RecoveryOption(
            option_id="component_restart",
            label="逐组件重启",
            description="不应成为失败恢复状态",
            action="component_restart",
        )


def test_evidence_contract_round_trips_only_structured_fact_requirements():
    from klonet_agent.ops.privileged.workflow.contracts import ProbeRequest

    request = ProbeRequest(
        "project_layout",
        {"project_roots": ["/srv/source"]},
        "inspect the frozen source",
        ({
            "fact_id": "fact-source-exists",
            "predicate": "path.exists",
            "expected": True,
            "comparison": "equals",
            "freshness": "cached",
        },),
        gap_id="gap-source-layout",
        affected_steps=("prepare-source",),
        subject={"kind": "path", "value": "/srv/source"},
        scope=("/srv/source",),
        exclusions=("/etc/nginx",),
    )

    payload = request.to_dict()
    restored = ProbeRequest(
        payload["probe"], payload["args"], payload["purpose"],
        tuple(payload["required_facts"]), payload["freshness"],
        payload["gap_id"], tuple(payload["affected_steps"]),
        payload["subject"], tuple(payload["scope"]),
        tuple(payload["exclusions"]),
    )

    assert restored == request
    assert payload["covers"] == ["fact-source-exists"]
    assert isinstance(payload["required_facts"][0], dict)
    assert payload["subject"] == {"kind": "path", "value": "/srv/source"}


def test_model_fact_id_is_canonicalized_once_at_contract_ingress():
    from klonet_agent.ops.privileged.workflow.contracts import FactRequirement

    requirement = FactRequirement.from_value({
        "fact_id": "source_dir_exists",
        "predicate": "path.exists",
        "expected": True,
        "comparison": "equals",
    })

    assert requirement.fact_id == "fact-source-dir-exists"
    assert FactRequirement.from_value(requirement) is requirement


def test_probe_contract_normalizes_provider_scalar_lists_and_contains_all():
    from klonet_agent.ops.privileged.workflow.contracts import (
        FactRequirement, normalize_probe_request,
    )

    probe, args = normalize_probe_request(
        "ports", {"ports": "45553, 45554"},
    )
    requirement = FactRequirement.from_value({
        "fact_id": "fact-ports-used",
        "predicate": "port.in_use",
        "expected": "45553,45554",
        "comparison": "contains_all",
    })

    assert probe == "ports"
    assert args == {"ports": ["45553", "45554"]}
    assert requirement.expected == [45553, 45554]

    one_port = FactRequirement.from_value({
        "fact_id": "fact-one-port",
        "predicate": "port.available",
        "expected": "45556",
        "comparison": "contains",
    })
    assert one_port.expected == 45556

    uid_requirement = FactRequirement.from_value({
        "fact_id": "fact-owner-uid",
        "predicate": "path.uid",
        "expected": "1000",
        "comparison": "contains",
    })
    assert uid_requirement.expected == 1000


def test_evidence_subject_rejects_semantically_invalid_set_values():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceSubject

    with pytest.raises(ValueError, match="port_set requires list"):
        EvidenceSubject("port_set", True)
    with pytest.raises(ValueError, match="integer members"):
        EvidenceSubject("port_set", ["not-a-port"])


def test_gap_resolution_ignores_raw_output_without_fact_observation():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    request = ProbeRequest(
        "long_tail",
        {"path": "/srv/source"},
        "find marker",
        ({
            "fact_id": "fact-source-marker",
            "predicate": "source.marker",
            "expected": "ready",
            "comparison": "equals",
        },),
        gap_id="gap-source-marker",
    )
    bundle = EvidenceBundle(goal="inspect source")
    bundle.add(EvidenceRecord.from_probe(
        request,
        "a large but unrelated nginx configuration was returned",
    ))

    resolution = bundle.resolve_gap("gap-source-marker")

    assert resolution.confirmed_fact_ids == ()
    assert resolution.contradicted_fact_ids == ()
    assert resolution.unresolved_fact_ids == ("fact-source-marker",)


def test_requirement_values_never_reparse_raw_output_and_preserve_fact_granularity():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, FactObservation, ProbeRequest,
    )

    bundle = EvidenceBundle(goal="allocate ports")
    raw_only = ProbeRequest(
        "ports", {"ports": [47001]}, "raw-only legacy result",
        ({
            "fact_id": "fact-port-available-47001",
            "predicate": "port.available",
            "expected": 47001,
            "comparison": "contains",
        },),
        gap_id="gap-port-raw-only",
    )
    bundle.add(EvidenceRecord.from_probe(
        raw_only,
        "available_ports=47001",
    ))
    confirmed_many = ProbeRequest(
        "ports", {"ports": [47002, 47003]}, "typed result",
        ({
            "fact_id": "fact-ports-available-many",
            "predicate": "port.available",
            "expected": [47002, 47003],
            "comparison": "contains_all",
        },),
        gap_id="gap-port-confirmed-many",
    )
    bundle.add(EvidenceRecord.from_probe(
        confirmed_many,
        "raw text deliberately carries no control meaning",
        observations=(FactObservation(
            "fact-ports-available-many", "confirmed", [47002, 47003],
            "ports.port.available",
        ),),
    ))
    contradicted_one = ProbeRequest(
        "ports", {"ports": [47004]}, "typed negative result",
        ({
            "fact_id": "fact-port-available-47004",
            "predicate": "port.available",
            "expected": 47004,
            "comparison": "contains",
        },),
        gap_id="gap-port-negative-one",
    )
    bundle.add(EvidenceRecord.from_probe(
        contradicted_one,
        "available_ports=47004 is misleading raw text",
        observations=(FactObservation(
            "fact-port-available-47004", "contradicted", [],
            "ports.port.available",
        ),),
    ))

    assert bundle.requirement_values(
        "port.available", observation_status="confirmed",
    ) == (47002, 47003)
    assert bundle.requirement_values(
        "port.available", observation_status="contradicted",
    ) == (47004,)


def test_gap_resolution_uses_fact_identity_and_keeps_only_unanswered_facts():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, FactObservation, ProbeRequest,
    )

    request = ProbeRequest(
        "project_layout",
        {"project_roots": ["/srv/source"]},
        "inspect exact source",
        (
            {
                "fact_id": "fact-source-exists",
                "predicate": "path.exists",
                "expected": True,
                "comparison": "equals",
            },
            {
                "fact_id": "fact-entry-files",
                "predicate": "project.entry_files",
                "expected": ["master_main.py", "worker_main.py"],
                "comparison": "contains_all",
            },
        ),
        gap_id="gap-source-layout",
    )
    bundle = EvidenceBundle(goal="inspect source")
    bundle.add(EvidenceRecord.from_probe(
        request,
        "字段名和语言可以变化；resolution 不读取这段自然语言",
        observations=(FactObservation(
            "fact-source-exists", "confirmed", True,
            "project_layout.path.exists",
        ),),
    ))

    resolution = bundle.resolve_gap("gap-source-layout")

    assert resolution.confirmed_fact_ids == ("fact-source-exists",)
    assert resolution.contradicted_fact_ids == ()
    assert resolution.unresolved_fact_ids == ("fact-entry-files",)
