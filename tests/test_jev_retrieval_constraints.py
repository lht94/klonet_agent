"""Store-constraint tests: Jev route must be enforced in code (plan 05 §4.4)."""

from __future__ import annotations

import pytest


def _payload(*stores, purpose="x"):
    return {
        "standalone_query": "Klonet 拓扑部署流程",
        "retrieval_tasks": [
            {
                "store": store,
                "purpose": purpose,
                "keyword_queries": ["拓扑部署"],
                "semantic_queries": [],
                "exact_terms": [],
            }
            for store in stores
        ],
        "confidence": 0.8,
    }


def test_public_only_constraint_drops_source_code_task():
    from klonet_agent.knowledge.models import RetrievalConstraints
    from klonet_agent.knowledge.query_planner import validate_plan_with_constraints

    plan = validate_plan_with_constraints(
        "Klonet 拓扑部署流程是什么？",
        _payload("public_docs", "source_code"),
        constraints=RetrievalConstraints(
            allowed_stores=("public_docs",), requires_retrieval=True,
        ),
    )

    assert [task.store for task in plan.retrieval_tasks] == ["public_docs"]
    assert plan.detail == "planner_store_violation"
    assert plan.status == "constrained"


def test_source_only_constraint_drops_public_docs_task():
    from klonet_agent.knowledge.models import RetrievalConstraints
    from klonet_agent.knowledge.query_planner import validate_plan_with_constraints

    plan = validate_plan_with_constraints(
        "MultiStageRetriever.search 的签名是什么？",
        _payload("public_docs", "source_code"),
        constraints=RetrievalConstraints(
            allowed_stores=("source_code",), requires_retrieval=True,
        ),
    )

    assert [task.store for task in plan.retrieval_tasks] == ["source_code"]


def test_no_retrieval_constraint_produces_an_empty_plan():
    from klonet_agent.knowledge.models import RetrievalConstraints
    from klonet_agent.knowledge.query_planner import validate_plan_with_constraints

    plan = validate_plan_with_constraints(
        "Python 的 GIL 是什么？",
        _payload("public_docs", "source_code"),
        constraints=RetrievalConstraints(
            allowed_stores=(), requires_retrieval=False, source="jev",
        ),
    )

    assert plan.retrieval_tasks == ()
    assert plan.detail == "no_retrieval"
    assert plan.status == "constrained"


def test_both_constraint_keeps_a_single_task_without_duplicating():
    from klonet_agent.knowledge.models import RetrievalConstraints
    from klonet_agent.knowledge.query_planner import validate_plan_with_constraints

    plan = validate_plan_with_constraints(
        "文档和实现一致吗？",
        _payload("public_docs"),
        constraints=RetrievalConstraints(
            allowed_stores=("public_docs", "source_code"), requires_retrieval=True,
        ),
    )

    assert [task.store for task in plan.retrieval_tasks] == ["public_docs"]
    assert plan.status == "planned"


def test_store_invariant_holds_when_planner_output_is_invalid():
    """The critical P0: an out-of-constraint fallback must NOT search both stores."""

    from klonet_agent.knowledge.models import RetrievalConstraints
    from klonet_agent.knowledge.query_planner import validate_plan_with_constraints

    constraints = RetrievalConstraints(
        allowed_stores=("public_docs",), requires_retrieval=True, source="jev",
    )
    # No usable task at all → the planner would normally fail open to *both* stores.
    plan = validate_plan_with_constraints(
        "Klonet 拓扑部署流程", {"retrieval_tasks": []}, constraints=constraints,
    )

    assert [task.store for task in plan.retrieval_tasks] == ["public_docs"]
    assert all(
        task.store in constraints.allowed_stores
        for task in plan.retrieval_tasks
    )


def test_legacy_fallback_plan_still_searches_both_stores_without_constraints():
    """Without a constraint the historical fail-open behaviour is unchanged."""

    from klonet_agent.knowledge.query_planner import validate_plan_with_constraints

    plan = validate_plan_with_constraints(
        "Klonet 拓扑部署流程", {"retrieval_tasks": []},
    )

    assert {task.store for task in plan.retrieval_tasks} == {
        "public_docs", "source_code",
    }
    assert plan.status == "fallback"


def test_constrained_fallback_plan_with_empty_allowed_stores_has_no_tasks():
    from klonet_agent.knowledge.query_planner import constrained_fallback_plan

    plan = constrained_fallback_plan(
        "Klonet 是什么？", allowed_stores=(), detail="planner_store_violation",
    )

    assert plan.retrieval_tasks == ()
    assert plan.status == "constrained"


def test_plan_from_mapping_keeps_the_constraint():
    from klonet_agent.knowledge.models import RetrievalConstraints
    from klonet_agent.knowledge.query_planner import (
        plan_as_dict,
        plan_from_mapping,
        validate_plan_with_constraints,
    )

    constraints = RetrievalConstraints(
        allowed_stores=("source_code",), requires_retrieval=True,
    )
    plan = validate_plan_with_constraints(
        "源码在哪？", _payload("public_docs", "source_code"), constraints=constraints,
    )
    restored = plan_from_mapping(
        "源码在哪？", plan_as_dict(plan), constraints=constraints,
    )

    assert restored is not None
    assert [task.store for task in restored.retrieval_tasks] == ["source_code"]


def test_retrieval_constraints_constrained_property_and_permits():
    from klonet_agent.knowledge.models import RetrievalConstraints

    legacy = RetrievalConstraints()
    assert legacy.constrained is False
    assert legacy.permits("public_docs") is True

    limited = RetrievalConstraints(allowed_stores=("public_docs",))
    assert limited.constrained is True
    assert limited.permits("public_docs") is True
    assert limited.permits("source_code") is False

    none_at_all = RetrievalConstraints(requires_retrieval=False)
    assert none_at_all.constrained is True


def test_route_decision_builds_provider_neutral_constraints():
    from klonet_agent.llm.decision import DecisionResult
    from klonet_agent.llm.decision_policy import decide_knowledge_route

    answers = {
        "scope": "klonet",
        "requires_retrieval": True,
        "requires_public_docs": False,
        "requires_source_code": True,
        "requires_environment_diagnosis": False,
        "clarification_required": False,
    }
    decision = decide_knowledge_route(
        DecisionResult(
            answers=answers, confidences={key: 0.95 for key in answers},
        ),
        request_text="MultiStageRetriever.search 的签名是什么？",
        mode="enforced",
    )
    constraints = decision.to_constraints()

    assert constraints.source == "jev"
    assert constraints.allowed_stores == ("source_code",)
    assert constraints.requires_retrieval is True
    assert "planner_store_violation" not in constraints.reason_codes


def test_planner_violation_is_recorded_in_outcome_for_reporting():
    from klonet_agent.knowledge.models import RetrievalConstraints
    from klonet_agent.knowledge.query_planner import validate_plan_with_constraints

    plan = validate_plan_with_constraints(
        "源码在哪？",
        _payload("public_docs", "source_code"),
        constraints=RetrievalConstraints(
            allowed_stores=("public_docs",), requires_retrieval=True,
        ),
    )

    assert plan.detail == "planner_store_violation"


@pytest.mark.parametrize("store", ["public_docs", "source_code"])
def test_single_store_plan_is_untouched_when_inside_the_constraint(store):
    from klonet_agent.knowledge.models import RetrievalConstraints
    from klonet_agent.knowledge.query_planner import validate_plan_with_constraints

    plan = validate_plan_with_constraints(
        "Klonet 用法",
        _payload(store),
        constraints=RetrievalConstraints(
            allowed_stores=(store,), requires_retrieval=True,
        ),
    )

    assert [task.store for task in plan.retrieval_tasks] == [store]
    assert plan.status == "planned"
