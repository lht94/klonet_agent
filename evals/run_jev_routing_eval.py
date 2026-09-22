"""Run alternating legacy-versus-Jev routing evaluations against live models."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any

from klonet_agent.config import (
    JEV_API_KEY_ENV, JEV_BASE_URL, JEV_MODEL, JEV_TIMEOUT_SECONDS,
)
from klonet_agent.evals.jev_routing import RoutingBenchmark
from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer
from klonet_agent.llm import LLMClient
from klonet_agent.llm.decision import JevDecisionModel, TypeSafeJevClient
from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def _answers(value: Any, kind: str) -> dict:
    if kind == "turn":
        intent = value.intent
        return {
            "scope": intent.scope,
            "task_type": intent.task_type,
            "operation": intent.operation,
            "requires_retrieval": intent.requires_retrieval,
            "requires_environment_diagnosis": intent.requires_environment_diagnosis,
            "clarification_required": intent.clarification_required,
            "is_correction": intent.is_correction,
        }
    return {
        "intent": value.intent,
        "goal_clarity": value.goal_clarity,
        "goal_relation": value.goal_relation,
        "goal_kind": value.goal_kind,
        "operation": value.operation,
        "scope": value.scope,
        "requires_execution": value.requires_execution,
    }


def _observe(route: str, case: dict, repeat: int, call) -> dict:
    started = perf_counter()
    try:
        value = call()
        result = {
            "case_id": case["id"], "repeat": repeat,
            "answers": _answers(value, case["kind"]),
            "failed": False,
            "fallback": bool(getattr(value, "decision_fallback_reason", "")),
            "input_tokens": int(getattr(value, "token_usage", 0) or 0),
            "estimated_cost_usd": 0.0,
        }
    except Exception as exc:
        result = {
            "case_id": case["id"], "repeat": repeat, "answers": {},
            "failed": True, "fallback": False,
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            "input_tokens": 0, "estimated_cost_usd": 0.0,
        }
    result["latency_seconds"] = perf_counter() - started
    result["route"] = route
    return result


def run(cases: list[dict], *, repeats: int, min_confidence: float) -> dict[str, list[dict]]:
    key = os.getenv(JEV_API_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError("%s is not configured" % JEV_API_KEY_ENV)
    llm = LLMClient()
    legacy_turn = IntentAnalyzer(llm)
    legacy_privileged = PrivilegedIntentClassifier(llm)
    jev = JevDecisionModel(TypeSafeJevClient(
        api_key=key, base_url=JEV_BASE_URL, model=JEV_MODEL,
        timeout=JEV_TIMEOUT_SECONDS,
    ))
    jev_turn = IntentAnalyzer(
        llm, decision_model=jev, min_decision_confidence=min_confidence,
    )
    jev_privileged = PrivilegedIntentClassifier(
        llm, decision_model=jev, min_decision_confidence=min_confidence,
    )
    observations = {"legacy": [], "jev": []}
    for repeat in range(repeats):
        for case in cases:
            def legacy_call(case=case):
                if case["kind"] == "turn":
                    return legacy_turn.analyze(case["prompt"])
                return legacy_privileged.classify(case["prompt"])

            def jev_call(case=case):
                if case["kind"] == "turn":
                    return jev_turn.analyze(case["prompt"])
                return jev_privileged.classify(case["prompt"])

            ordered = (("legacy", legacy_call), ("jev", jev_call))
            if repeat % 2:
                ordered = tuple(reversed(ordered))
            for route, call in ordered:
                observations[route].append(_observe(route, case, repeat, call))
    return observations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases", type=Path,
        default=Path(__file__).with_name("jev_routing_cases.jsonl"),
    )
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--gray-percent", type=int, default=0)
    parser.add_argument("--min-confidence", type=float, default=0.85)
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(__file__).with_name("results"),
    )
    args = parser.parse_args()
    cases = load_cases(args.cases)
    observations = run(
        cases, repeats=max(1, args.repeats),
        min_confidence=max(0.0, min(args.min_confidence, 1.0)),
    )
    benchmark = RoutingBenchmark(cases, observations)
    json_path, markdown_path = benchmark.write_report(
        args.output_dir, gray_percent=args.gray_percent,
    )
    print("json_report=%s" % json_path)
    print("markdown_report=%s" % markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
