"""Stage-0 contract freeze: gold set schema, v0.4→v0.5 boundary, offline runner."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest


DATASET = Path(__file__).resolve().parents[1] / "evals" / "data" / "jev_routing_v05.jsonl"

KINDS = {"turn", "privileged"}
ROUTES = {"no_retrieval", "public_docs", "source_code", "both", "legacy"}
CATEGORIES = {
    "no_retrieval", "public_docs", "source_code", "both",
    "clarification", "negative_reference", "privileged",
}


def _cases():
    return [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_gold_set_exists_and_has_at_least_120_frozen_cases():
    cases = _cases()

    assert len(cases) >= 120


def test_gold_set_ids_are_unique_and_schema_is_valid():
    cases = _cases()

    ids = [case["id"] for case in cases]
    assert len(set(ids)) == len(ids), "case_id 必须唯一"

    for case in cases:
        assert case["kind"] in KINDS, case["id"]
        assert case["prompt"].strip(), case["id"]
        assert case["category"] in CATEGORIES, case["id"]
        assert case["expected_route"] in ROUTES, case["id"]
        assert isinstance(case["expected"], dict) and case["expected"], case["id"]
        assert isinstance(case.get("history", []), list), case["id"]


def test_gold_set_categories_match_the_plan_distribution():
    counts = Counter(case["category"] for case in _cases())

    assert counts["no_retrieval"] >= 15
    assert counts["public_docs"] >= 25
    assert counts["source_code"] >= 25
    assert counts["both"] >= 20
    assert counts["clarification"] >= 15
    assert counts["negative_reference"] >= 10
    assert counts["privileged"] >= 10
    assert sum(counts.values()) >= 120


def test_stability_subset_is_large_enough_for_the_20x_experiment():
    stable = [case for case in _cases() if case.get("stability")]

    assert len(stable) >= 30


def test_dangerous_cases_are_explicitly_flagged():
    cases = _cases()
    privileged = [case for case in cases if case["kind"] == "privileged"]

    assert privileged, "特权安全样例不能为空"
    assert all(isinstance(case.get("dangerous"), bool) for case in cases)
    assert any(case.get("dangerous") for case in privileged)


def test_privileged_cases_expect_a_legacy_route_and_no_retrieval_contract():
    for case in _cases():
        if case["kind"] == "privileged":
            assert case["expected_route"] == "legacy", case["id"]
            assert case["expected"]["intent"], case["id"]
            assert isinstance(case["expected"]["requires_execution"], bool)


def test_retrieval_quality_cases_carry_expected_sources():
    scored = [case for case in _cases() if case.get("expected_sources")]

    assert len(scored) >= 50
    for case in scored:
        assert isinstance(case["expected_sources"], list)
        assert all(str(item).strip() for item in case["expected_sources"])


def test_allowed_stores_is_consistent_with_the_expected_route():
    mapping = {
        "no_retrieval": [],
        "public_docs": ["public_docs"],
        "source_code": ["source_code"],
        "both": ["public_docs", "source_code"],
    }
    for case in _cases():
        if case["kind"] != "turn":
            continue
        assert case["allowed_stores"] == mapping[case["expected_route"]], case["id"]


# --- v0.4 → v0.5 contract boundary -----------------------------------------

def test_v04_result_without_store_answers_is_no_longer_trusted():
    """v0.4 only trusted scope/requires_retrieval; v0.5 requires the store answers."""

    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer
    from klonet_agent.llm.decision import DecisionResult
    from klonet_agent.llm.decision_policy import decide_knowledge_route

    v04_result = DecisionResult(
        answers={"scope": "general", "requires_retrieval": False},
        confidences={"scope": 0.96, "requires_retrieval": 0.96},
    )
    decision = decide_knowledge_route(
        v04_result, request_text="Python 的 GIL 是什么？", mode="enforced",
    )

    assert decision.disposition == "fallback"
    assert "missing_answer" in decision.reason_codes

    class NeverLLM:
        def complete(self, **_kwargs):
            raise AssertionError("fallback must run the legacy LLM path")

    class Decisions:
        def classify_turn(self, _state):
            return v04_result

    with pytest.raises(AssertionError):
        IntentAnalyzer(
            NeverLLM(),
            decision_model=Decisions(),
            routing_mode="enforced",
        ).analyze("Python 的 GIL 是什么？")


def test_shadow_mode_never_skips_the_generative_llm():
    from types import SimpleNamespace

    from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer
    from klonet_agent.llm.decision import DecisionResult

    class LLM:
        calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=(
                    '{"scope":"general","task_type":"general",'
                    '"operation":"unknown","requires_retrieval":false,'
                    '"confidence":0.9}'
                )))],
                usage=SimpleNamespace(total_tokens=5),
            )

    class Decisions:
        def classify_turn(self, _state):
            answers = {
                "scope": "general",
                "requires_retrieval": False,
                "requires_public_docs": False,
                "requires_source_code": False,
                "requires_environment_diagnosis": False,
                "clarification_required": False,
            }
            return DecisionResult(
                answers=answers, confidences={key: 0.97 for key in answers},
            )

    llm = LLM()
    analysis = IntentAnalyzer(
        llm, decision_model=Decisions(), routing_mode="shadow",
    ).analyze("Python 的 GIL 是什么？")

    assert llm.calls == 1, "shadow 模式不得跳过旧 LLM"
    assert analysis.used_decision_model is False
    assert analysis.knowledge_route == "legacy"


# --- runner CLI contract ----------------------------------------------------

def test_route_legacy_does_not_require_a_jev_key(monkeypatch):
    from klonet_agent.evals import run_jev_routing_eval as runner

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    observations = runner.run(
        [{"id": "x", "kind": "turn", "prompt": "hi"}],
        repeats=1, min_confidence=0.85, routes=("legacy",),
    )

    assert set(observations) == {"legacy"}
    assert observations["legacy"][0]["case_id"] == "x"
    assert observations["legacy"][0]["adopted"] is False


def test_route_jev_without_a_key_raises_an_actionable_error(monkeypatch):
    from klonet_agent.evals import run_jev_routing_eval as runner

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        runner.run(
            [{"id": "x", "kind": "turn", "prompt": "hi"}],
            repeats=1, min_confidence=0.85, routes=("jev",),
        )


def test_subset_selection_uses_the_stability_flag():
    from klonet_agent.evals.run_jev_routing_eval import select_cases

    cases = [
        {"id": "a", "stability": True},
        {"id": "b", "stability": False},
        {"id": "c", "stability": True},
    ]

    assert [case["id"] for case in select_cases(cases, "stability")] == ["a", "c"]
    assert len(select_cases(cases, "all")) == 3


def test_offline_mode_generates_reports_without_any_api_call(tmp_path, monkeypatch):
    from klonet_agent.evals import run_jev_routing_eval as runner
    from klonet_agent.evals.jev_routing import dump_observations

    observations = {
        "legacy": [{
            "case_id": "gen-001", "repeat": 0,
            "answers": {"scope": "general", "requires_retrieval": False},
            "failed": False, "fallback": False, "adopted": False,
            "latency_seconds": 1.0, "input_tokens": 10,
            "estimated_cost_usd": 0.0, "reason_codes": [],
            "confidences": {}, "knowledge_route": "legacy",
        }],
        "jev": [{
            "case_id": "gen-001", "repeat": 0,
            "answers": {"scope": "general", "requires_retrieval": False,
                        "knowledge_route": "no_retrieval"},
            "failed": False, "fallback": False, "adopted": True,
            "latency_seconds": 0.2, "input_tokens": 4,
            "estimated_cost_usd": 0.0, "reason_codes": ["accepted"],
            "confidences": {"scope": 0.97}, "knowledge_route": "no_retrieval",
        }],
    }
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps({
        "id": "gen-001", "kind": "turn", "category": "no_retrieval",
        "prompt": "Python 的 GIL 是什么？",
        "expected": {"scope": "general", "requires_retrieval": False},
        "expected_route": "no_retrieval",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    observations_path = dump_observations(observations, tmp_path / "obs.jsonl")
    out = tmp_path / "out"

    monkeypatch.setattr(sys, "argv", [
        "run_jev_routing_eval",
        "--dataset", str(dataset),
        "--observations", str(observations_path),
        "--output", str(out),
    ])
    assert runner.main() == 0

    report = json.loads((out / "jev-routing-report.json").read_text(encoding="utf-8"))
    assert report["jev"]["coverage"] == 1.0
    assert report["jev"]["exact_match_rate"] == 1.0
    assert report["jev"]["fallback_reason_counts"] == {"accepted": 1}
    markdown = (out / "jev-routing-report.md").read_text(encoding="utf-8")
    assert "失败与回退明细" in markdown
    assert "危险动作漏判" in markdown


def test_runner_defaults_to_the_v05_dataset():
    from klonet_agent.evals import run_jev_routing_eval as runner

    assert runner.DEFAULT_DATASET.name == "jev_routing_v05.jsonl"
    assert runner.DEFAULT_DATASET.exists()
