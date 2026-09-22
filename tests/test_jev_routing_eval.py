"""Metrics and report tests for the Jev routing comparison."""

from __future__ import annotations

import json


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
