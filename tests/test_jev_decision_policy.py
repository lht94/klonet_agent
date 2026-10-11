"""Field-level policy tests for Jev knowledge-store routing (plan 05 §3–§6)."""

from __future__ import annotations

import pytest


def _result(answers, *, confidences=None, probabilities=None, fallback=False):
    from klonet_agent.llm.decision import DecisionResult

    return DecisionResult(
        answers=dict(answers),
        confidences=dict(confidences or {}),
        probabilities=dict(probabilities or {}),
        fallback=fallback,
        provider="typesafe",
    )


def _turn_answers(**overrides):
    answers = {
        "scope": "klonet",
        "task_type": "concept",
        "operation": "unknown",
        "deployment_phase": "unknown",
        "action_goal": "explain_concept",
        "requires_retrieval": True,
        "requires_public_docs": True,
        "requires_source_code": False,
        "requires_environment_diagnosis": False,
        "clarification_required": False,
        "is_correction": False,
    }
    answers.update(overrides)
    return answers


def _confident(answers, value=0.95):
    return {key: value for key in answers}


def _decide(result, request_text="Klonet 的拓扑部署流程是什么？", mode="enforced"):
    from klonet_agent.llm.decision_policy import decide_knowledge_route

    return decide_knowledge_route(result, request_text=request_text, mode=mode)


@pytest.mark.parametrize(
    ("overrides", "route"),
    [
        ({"requires_public_docs": True, "requires_source_code": False}, "public_docs"),
        ({"requires_public_docs": False, "requires_source_code": True}, "source_code"),
        ({"requires_public_docs": True, "requires_source_code": True}, "both"),
    ],
)
def test_route_composition_covers_single_store_and_both(overrides, route):
    answers = _turn_answers(**overrides)
    decision = _decide(_result(answers, confidences=_confident(answers)))

    assert decision.disposition == "constrain"
    assert decision.knowledge_route == route
    assert decision.reason_codes == ("accepted",)
    assert decision.adopted is True
    assert decision.allows_llm_skip is False
    assert decision.allowed_stores == {
        "public_docs": ("public_docs",),
        "source_code": ("source_code",),
        "both": ("public_docs", "source_code"),
    }[route]


def test_general_request_without_retrieval_is_accepted_and_skips_llm():
    answers = _turn_answers(
        scope="general",
        task_type="general",
        requires_retrieval=False,
        requires_public_docs=False,
        requires_source_code=False,
    )
    decision = _decide(
        _result(answers, confidences=_confident(answers)),
        request_text="Python 的 GIL 是什么？",
    )

    assert decision.disposition == "accept"
    assert decision.knowledge_route == "no_retrieval"
    assert decision.allows_llm_skip is True
    assert decision.allowed_stores == ()


def test_klonet_scope_without_retrieval_is_constrained_not_accepted():
    answers = _turn_answers(requires_retrieval=False,
                            requires_public_docs=False,
                            requires_source_code=False)
    decision = _decide(_result(answers, confidences=_confident(answers)))

    assert decision.disposition == "constrain"
    assert decision.knowledge_route == "no_retrieval"


def test_below_field_threshold_falls_back_with_exact_reason_code():
    answers = _turn_answers()
    confidences = _confident(answers)
    confidences["requires_source_code"] = 0.10
    decision = _decide(_result(answers, confidences=confidences))

    assert decision.disposition == "fallback"
    assert decision.knowledge_route == "legacy"
    assert "below_field_threshold" in decision.reason_codes
    blocked = {
        item.name for item in decision.field_decisions if not item.accepted
    }
    assert blocked == {"requires_source_code"}


def test_choice_margin_too_small_rejects_the_field():
    answers = _turn_answers()
    confidences = _confident(answers)
    confidences["scope"] = 0.90
    probabilities = {"scope": {"klonet": 0.90, "general": 0.85, "mixed": 0.01}}
    decision = _decide(
        _result(answers, confidences=confidences, probabilities=probabilities)
    )

    assert decision.disposition == "fallback"
    assert "probability_margin_too_small" in decision.reason_codes


def test_missing_choice_distribution_records_margin_unavailable_not_a_rejection():
    """Provider without Choice distributions must not fake a margin rejection."""

    answers = _turn_answers()
    decision = _decide(_result(answers, confidences=_confident(answers)))

    assert decision.disposition == "constrain"
    assert decision.margin_available is False
    assert "margin_unavailable" in {
        item.reason_code for item in decision.field_decisions
    }
    assert "probability_margin_too_small" not in decision.reason_codes


def test_noul_probability_uses_max_of_true_false_as_confidence():
    """Plan §10 risk row 1: a confident False must not read as low confidence."""

    from klonet_agent.llm.decision_policy import field_confidence

    result = _result(
        {"requires_retrieval": False},
        probabilities={"requires_retrieval": {"true": 0.04, "false": 0.96}},
    )

    assert field_confidence(result, "requires_retrieval", False) == pytest.approx(0.96)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"requires_retrieval": False, "requires_public_docs": True},
         "retrieval_store_conflict"),
        ({"requires_public_docs": False, "requires_source_code": False},
         "retrieval_store_conflict"),
        ({"scope": "general"}, "general_scope_conflict"),
    ],
)
def test_consistency_conflicts_fall_back(overrides, reason):
    answers = _turn_answers(**overrides)
    decision = _decide(_result(answers, confidences=_confident(answers)))

    assert decision.disposition == "fallback"
    assert decision.knowledge_route == "legacy"
    assert reason in decision.reason_codes


def test_deterministic_rule_vetoes_source_code_false_for_explicit_request():
    answers = _turn_answers(requires_public_docs=True, requires_source_code=False)
    decision = _decide(
        _result(answers, confidences=_confident(answers)),
        request_text="MultiStageRetriever.search 的函数签名是什么？",
    )

    assert decision.disposition == "fallback"
    assert "deterministic_rule_conflict" in decision.reason_codes


def test_deterministic_rule_vetoes_source_only_when_user_says_no_source():
    answers = _turn_answers(requires_public_docs=False, requires_source_code=True)
    decision = _decide(
        _result(answers, confidences=_confident(answers)),
        request_text="不要看源码，只讲部署步骤。",
    )

    assert decision.disposition == "fallback"
    assert "deterministic_rule_conflict" in decision.reason_codes


def test_shadow_mode_flags_the_decision_without_changing_the_route():
    answers = _turn_answers()
    decision = _decide(_result(answers, confidences=_confident(answers)), mode="shadow")

    assert decision.knowledge_route == "public_docs"
    assert "shadow_only" in decision.reason_codes
    assert decision.applied_mode == "shadow"


@pytest.mark.parametrize(
    "result",
    [None, _result({"scope": "klonet"}, fallback=True)],
)
def test_provider_failure_and_missing_answers_fall_back(result):
    if result is None:
        decision = _decide(None)
    else:
        decision = _decide(result)

    assert decision.disposition == "fallback"
    assert "provider_error" in decision.reason_codes or (
        "missing_answer" in decision.reason_codes
    )


def test_invalid_enum_answer_is_rejected():
    answers = _turn_answers(scope="invented")
    decision = _decide(_result(answers, confidences=_confident(answers)))

    assert decision.disposition == "fallback"
    assert "invalid_answer" in decision.reason_codes


# --- privileged path -------------------------------------------------------

def _privileged(answers, value=0.97):
    from klonet_agent.llm.decision_policy import decide_knowledge_route

    return decide_knowledge_route(
        _result(answers, confidences={key: value for key in answers}),
        request_text="重启 worker",
        privileged=True,
        mode="enforced",
    )


def test_privileged_mutating_action_is_adopted_but_never_grants_execution():
    decision = _privileged({
        "intent": "mutating_action",
        "requires_execution": True,
    })

    assert decision.disposition == "constrain"
    assert decision.knowledge_route == "legacy"
    assert decision.accepted_answers == {
        "intent": "mutating_action", "requires_execution": True,
    }


def test_privileged_intent_without_execution_flag_conflicts():
    decision = _privileged({
        "intent": "mutating_action",
        "requires_execution": False,
    })

    assert decision.disposition == "fallback"
    assert "deterministic_rule_conflict" in decision.reason_codes


def test_privileged_ambiguous_intent_fails_closed():
    decision = _privileged({
        "intent": "ambiguous",
        "requires_execution": False,
    })

    assert decision.disposition == "fallback"


def test_privileged_low_confidence_operation_is_not_used_for_execution():
    answers = {"intent": "readonly_action", "requires_execution": True}
    from klonet_agent.llm.decision_policy import decide_knowledge_route

    decision = decide_knowledge_route(
        _result(answers, confidences={"intent": 0.99, "requires_execution": 0.30}),
        privileged=True,
        mode="enforced",
    )

    assert decision.disposition == "fallback"
    assert "below_field_threshold" in decision.reason_codes


def test_fallback_group_mapping_matches_report_sections():
    from klonet_agent.llm.decision_policy import fallback_group

    assert fallback_group("provider_error") == "provider_fallback"
    assert fallback_group("below_field_threshold") == "confidence_fallback"
    assert fallback_group("retrieval_store_conflict") == "consistency_fallback"
    assert fallback_group("deterministic_rule_conflict") == "rule_fallback"
    assert fallback_group("planner_store_violation") == "planner_fallback"
    assert fallback_group("shadow_only") == "shadow_ignored"


def test_thresholds_are_per_field_and_read_from_config_defaults():
    from klonet_agent.llm.decision_policy import JevDecisionThresholds

    limits = JevDecisionThresholds()
    values = {
        name: limits.for_field(name)
        for name in (
            "scope", "requires_retrieval", "requires_public_docs",
            "requires_source_code", "requires_environment_diagnosis",
            "clarification_required", "privileged_intent",
            "privileged_execution",
        )
    }
    assert len(set(values.values())) >= 5, values
    assert limits.for_field("requires_environment_diagnosis") > limits.for_field(
        "scope"
    )
    assert limits.for_field("privileged_intent") >= 0.90
    assert limits.for_field("privileged_execution") >= 0.90
