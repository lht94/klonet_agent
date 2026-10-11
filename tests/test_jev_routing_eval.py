"""Metrics and report tests for the Jev routing comparison."""

from __future__ import annotations

import json

import pytest


def test_routing_benchmark_reports_accuracy_stability_latency_and_safety(tmp_path):
    from klonet_agent.evals.jev_routing import RoutingBenchmark

    cases = [
        {
            "id": "P1", "kind": "privileged",
            "expected": {"intent": "mutating_action", "operation": "restart"},
        },
        {
            "id": "P2", "kind": "privileged",
            "expected": {"intent": "conversation", "operation": "none"},
        },
    ]
    observations = {
        "legacy": [
            {"case_id": "P1", "repeat": 0, "answers": {"intent": "mutating_action", "operation": "restart"}, "latency_seconds": 1.0, "failed": False, "fallback": False, "input_tokens": 100, "estimated_cost_usd": 0.01},
            {"case_id": "P1", "repeat": 1, "answers": {"intent": "readonly_action", "operation": "inspect"}, "latency_seconds": 1.2, "failed": False, "fallback": False, "input_tokens": 100, "estimated_cost_usd": 0.01},
            {"case_id": "P2", "repeat": 0, "answers": {"intent": "conversation", "operation": "none"}, "latency_seconds": 0.8, "failed": False, "fallback": False, "input_tokens": 90, "estimated_cost_usd": 0.009},
            {"case_id": "P2", "repeat": 1, "answers": {"intent": "conversation", "operation": "none"}, "latency_seconds": 0.9, "failed": False, "fallback": False, "input_tokens": 90, "estimated_cost_usd": 0.009},
        ],
        "jev": [
            {"case_id": "P1", "repeat": 0, "answers": {"intent": "mutating_action", "operation": "restart"}, "latency_seconds": 0.2, "failed": False, "fallback": False, "input_tokens": 20, "estimated_cost_usd": 0.000001},
            {"case_id": "P1", "repeat": 1, "answers": {"intent": "mutating_action", "operation": "restart"}, "latency_seconds": 0.22, "failed": False, "fallback": False, "input_tokens": 20, "estimated_cost_usd": 0.000001},
            {"case_id": "P2", "repeat": 0, "answers": {"intent": "conversation", "operation": "none"}, "latency_seconds": 0.18, "failed": False, "fallback": False, "input_tokens": 18, "estimated_cost_usd": 0.000001},
            {"case_id": "P2", "repeat": 1, "answers": {"intent": "conversation", "operation": "none"}, "latency_seconds": 0.19, "failed": False, "fallback": False, "input_tokens": 18, "estimated_cost_usd": 0.000001},
        ],
    }

    benchmark = RoutingBenchmark(cases, observations)
    report = benchmark.build_report(gray_percent=5)
    json_path, markdown_path = benchmark.write_report(tmp_path, gray_percent=5)

    assert report["jev"]["exact_match_rate"] == 1.0
    assert report["jev"]["route_consistency_rate"] == 1.0
    assert report["legacy"]["route_consistency_rate"] == 0.5
    assert report["jev"]["dangerous_action_misses"] == 0
    assert report["performance_comparison"]["p95_speedup_ratio"] > 0.7
    assert report["config"]["gray_percent"] == 5
    assert json.loads(json_path.read_text(encoding="utf-8"))["jev"]["exact_match_rate"] == 1.0
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "危险动作漏判" in markdown
    assert "P95" in markdown


def test_routing_benchmark_counts_failures_fallbacks_and_mutation_misses():
    from klonet_agent.evals.jev_routing import RoutingBenchmark

    cases = [{
        "id": "P1", "kind": "privileged",
        "expected": {"intent": "mutating_action"},
    }]
    observations = {
        "legacy": [{"case_id": "P1", "repeat": 0, "answers": {"intent": "mutating_action"}, "latency_seconds": 1, "failed": False, "fallback": False}],
        "jev": [
            {"case_id": "P1", "repeat": 0, "answers": {"intent": "conversation"}, "latency_seconds": 0.1, "failed": False, "fallback": True},
            {"case_id": "P1", "repeat": 1, "answers": {}, "latency_seconds": 0.2, "failed": True, "fallback": False},
        ],
    }

    report = RoutingBenchmark(cases, observations).build_report(gray_percent=25)

    assert report["jev"]["dangerous_action_misses"] == 1
    assert report["jev"]["failure_rate"] == 0.5
    assert report["jev"]["fallback_rate"] == 0.5


# --- calibration metrics (plan 05 §7.2) ------------------------------------

def test_calibration_metric_values_are_exact_on_a_fixed_sample():
    from klonet_agent.evals.jev_routing import (
        accuracy_coverage_curve,
        brier_score,
        expected_calibration_error,
    )

    pairs = [(1.0, True), (1.0, True), (0.0, False), (0.0, False)]
    assert brier_score(pairs) == 0.0
    assert brier_score([(0.0, True), (1.0, False)]) == 1.0
    assert expected_calibration_error(pairs, bins=10) == 0.0
    assert expected_calibration_error([(0.9, True), (0.9, False)], bins=10) == 0.4

    curve = accuracy_coverage_curve([(0.9, True), (0.5, False)], thresholds=(0.5, 0.9))
    assert curve == [
        {"threshold": 0.5, "coverage": 1.0, "accuracy": 0.5},
        {"threshold": 0.9, "coverage": 0.5, "accuracy": 1.0},
    ]


def test_recall_mrr_and_store_selection_helpers():
    from klonet_agent.evals.jev_routing import (
        mrr_at_k,
        recall_at_k,
        reciprocal_rank,
        store_selection_accuracy,
    )

    ranked = ["a.md", "b.py", "c.py"]

    assert recall_at_k(ranked, ["b.py"], 3) == 1.0
    assert recall_at_k(ranked, ["z.py"], 3) == 0.0
    assert recall_at_k(ranked, ["b.py", "z.py"], 3) == 0.5
    assert reciprocal_rank(ranked, ["b.py"]) == 0.5
    assert mrr_at_k([(ranked, ["b.py"]), (ranked, ["z.py"])], 5) == 0.25
    assert store_selection_accuracy(
        ["public_docs", "source_code"], ["public_docs", "both"],
    ) == 0.5


def test_route_metrics_report_coverage_selective_accuracy_and_fallbacks():
    from klonet_agent.evals.jev_routing import RoutingBenchmark

    cases = [
        {"id": "C1", "kind": "turn", "expected": {"requires_retrieval": False},
         "expected_route": "no_retrieval"},
        {"id": "C2", "kind": "turn", "expected": {"requires_retrieval": True},
         "expected_route": "public_docs"},
    ]
    observations = {
        "legacy": [
            {"case_id": "C1", "repeat": 0, "answers": {"requires_retrieval": False},
             "failed": False, "fallback": False, "latency_seconds": 1.0},
            {"case_id": "C2", "repeat": 0, "answers": {"requires_retrieval": True},
             "failed": False, "fallback": False, "latency_seconds": 1.0},
        ],
        "jev": [
            {"case_id": "C1", "repeat": 0,
             "answers": {"requires_retrieval": False},
             "failed": False, "fallback": False, "adopted": True,
             "knowledge_route": "no_retrieval",
             "confidences": {"requires_retrieval": 0.9}, "latency_seconds": 0.2,
             "reason_codes": ["accepted"]},
            {"case_id": "C2", "repeat": 0, "answers": {},
             "failed": False, "fallback": True, "adopted": False,
             "knowledge_route": "legacy",
             "confidences": {"requires_retrieval": 0.4},
             "latency_seconds": 0.3,
             "reason_codes": ["below_field_threshold"]},
        ],
    }

    report = RoutingBenchmark(cases, observations).build_report(gray_percent=5)
    jev = report["jev"]

    assert jev["coverage"] == 0.5
    assert jev["selective_accuracy"] == 1.0
    assert jev["fallback_reason_counts"] == {
        "accepted": 1, "below_field_threshold": 1,
    }
    assert jev["knowledge_route_confusion"] == {
        "no_retrieval -> no_retrieval": 1,
        "public_docs -> legacy": 1,
    }
    # C1: (0.9-1)^2 = 0.01（答对）；C2: (0.4-0)^2 = 0.16（答错）→ 均值 0.085
    assert jev["brier_score"] == pytest.approx(0.085)
    # ECE：bin(0.9)=|0.9-1.0|*0.5=0.05；bin(0.4)=|0.4-0.0|*0.5=0.20 → 0.25
    assert jev["expected_calibration_error"] == pytest.approx(0.25)
    assert len(jev["failure_details"]) == 1
    assert "below_field_threshold" in jev["failure_details"][0]["error"]


def test_observations_round_trip_through_jsonl(tmp_path):
    from klonet_agent.evals.jev_routing import (
        dump_observations,
        load_observations,
    )

    observations = {
        "legacy": [{"case_id": "A", "repeat": 0, "answers": {}, "failed": False}],
        "jev": [{"case_id": "A", "repeat": 0, "answers": {}, "failed": False}],
    }
    path = dump_observations(observations, tmp_path / "obs.jsonl")
    restored = load_observations(path)

    assert set(restored) == {"legacy", "jev"}
    assert restored["jev"][0]["case_id"] == "A"
    assert "route" not in restored["jev"][0]


# --- threshold calibration (plan 05 §7.2) ---------------------------------

def test_calibration_recommends_max_coverage_under_the_error_gate():
    from klonet_agent.evals.jev_calibration import (
        build_calibration_report,
        recommend_threshold,
        scan_thresholds,
    )

    rows = [
        {"adopted": True, "exact": True, "confidences": {"scope": 0.95},
         "failed": False},
        {"adopted": True, "exact": False, "confidences": {"scope": 0.65},
         "failed": False},
    ]
    curve = scan_thresholds(rows, legacy_error=0.5)
    assert all(item["meets_gate"] for item in curve), curve

    recommendation = recommend_threshold(curve)
    assert recommendation["reason"] == "max_coverage_under_error_gate"
    # 0.60 与 0.65 的 coverage 都是 1.0，取更高的阈值（同样覆盖、更严格）。
    assert recommendation["threshold"] == 0.65
    assert recommendation["coverage"] == 1.0

    no_eligible = recommend_threshold(scan_thresholds([
        {"adopted": True, "exact": False, "confidences": {"scope": 0.95},
         "failed": False},
    ], legacy_error=0.0))
    assert no_eligible["reason"] == "no_eligible_threshold"

    report = build_calibration_report({
        "legacy": [{"failed": False, "exact": True}],
        "jev": [{
            "failed": False, "exact": True, "adopted": True,
            "answers": {"requires_retrieval": True},
            "confidences": {"requires_retrieval": 0.9},
        }],
    })
    assert report["legacy_route_error"] == 0.0
    assert "requires_retrieval" in report["field_reliability"]
    assert report["field_reliability"]["requires_retrieval"]["brier_score"] == (
        pytest.approx(0.01)
    )
    assert "不修改线上配置" in report["note"]


def test_calibration_writes_json_and_markdown(tmp_path):
    from klonet_agent.evals.jev_calibration import (
        build_calibration_report,
        write_calibration_report,
    )

    report = build_calibration_report({"jev": [], "legacy": []})
    json_path, markdown_path = write_calibration_report(report, tmp_path)

    assert json.loads(json_path.read_text(encoding="utf-8"))["expected_key"]
    assert "阈值曲线" in markdown_path.read_text(encoding="utf-8")


# --- retrieval quality (plan 05 §7.3) -------------------------------------

def test_retrieval_eval_records_channel_state_and_metrics():
    from types import SimpleNamespace

    from klonet_agent.evals.jev_retrieval_eval import (
        build_report,
        evaluate_case,
    )

    outcome = SimpleNamespace(
        results=[
            SimpleNamespace(path="knowledge/klonet_index/topology.md"),
            SimpleNamespace(path="knowledge/other.md"),
        ],
        retrieval_plan=SimpleNamespace(
            retrieval_tasks=[SimpleNamespace(store="public_docs")],
            detail="planned",
        ),
        retrieval_mode="multi_stage",
        vector_status="not_loaded",
        rerank_status="not_used",
    )
    case = {
        "id": "doc-001", "category": "public_docs", "prompt": "拓扑部署流程？",
        "expected_route": "public_docs",
        "expected_sources": ["knowledge/klonet_index/topology.md"],
    }
    row = evaluate_case(case, lambda _query: outcome)
    report = build_report([row])

    assert row["semantic_channel"] == "degraded"
    assert row["store_correct"] is True
    assert row["hit_at_3"] is True
    assert report["store_selection_accuracy"] == 1.0
    assert report["recall_at_3"] == 1.0
    assert report["mrr_at_5"] == 1.0
    assert report["semantic_channel"] == "degraded"
    assert report["vector_status_counts"] == {"not_loaded": 1}
    assert report["planner_store_violations"] == 0


def test_retrieval_eval_marks_channel_available_when_vectors_ok():
    from types import SimpleNamespace

    from klonet_agent.evals.jev_retrieval_eval import build_report, evaluate_case

    outcome = SimpleNamespace(
        results=[SimpleNamespace(path="knowledge/multi_stage.py")],
        retrieval_plan=SimpleNamespace(
            retrieval_tasks=[
                SimpleNamespace(store="public_docs"),
                SimpleNamespace(store="source_code"),
            ],
            detail="planned",
        ),
        retrieval_mode="multi_stage",
        vector_status="ready",
        rerank_status="ok",
    )
    row = evaluate_case(
        {
            "id": "both-001", "category": "both", "prompt": "代码和文档？",
            "expected_route": "both",
            "expected_sources": ["knowledge/multi_stage.py"],
        },
        lambda _query: outcome,
    )
    report = build_report([row])

    assert report["semantic_channel"] == "available"
    assert report["cross_store_coverage"] == 1.0
    assert report["irrelevant_store_call_rate"] == 0.0


def test_retrieval_eval_reports_planner_violations_and_irrelevant_stores():
    from types import SimpleNamespace

    from klonet_agent.evals.jev_retrieval_eval import build_report, evaluate_case

    outcome = SimpleNamespace(
        results=[],
        retrieval_plan=SimpleNamespace(
            retrieval_tasks=[SimpleNamespace(store="source_code")],
            detail="planner_store_violation",
        ),
        retrieval_mode="multi_stage",
        vector_status="ready",
        rerank_status="ok",
    )
    row = evaluate_case(
        {
            "id": "doc-002", "category": "public_docs", "prompt": "部署步骤？",
            "expected_route": "public_docs",
            "expected_sources": ["knowledge/klonet_index/prerequisites.md"],
        },
        lambda _query: outcome,
    )
    report = build_report([row])

    assert report["planner_store_violations"] == 1
    assert report["irrelevant_store_call_rate"] == 1.0
    assert report["empty_result_rate"] == 1.0


def test_retrieval_eval_handles_retriever_errors_without_crashing():
    from klonet_agent.evals.jev_retrieval_eval import build_report, evaluate_case

    def boom(_query):
        raise RuntimeError("index missing")

    row = evaluate_case(
        {
            "id": "doc-003", "category": "public_docs", "prompt": "部署？",
            "expected_route": "public_docs",
            "expected_sources": ["knowledge/klonet_index/x.md"],
        },
        boom,
    )
    report = build_report([row])

    assert row["failed"] is True
    assert "RuntimeError" in row["error"]
    assert report["empty_result_rate"] == 1.0
