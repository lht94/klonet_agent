"""Run alternating legacy-versus-Jev routing evaluations against live models.

Two input modes:

- **live** (default): call the real legacy/Jev paths and collect observations;
- **offline** (``--observations``): re-generate the report from a frozen JSONL,
  which is the only usable path when no Jev credentials are configured.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from klonet_agent.config import (
    JEV_API_KEY_ENV, JEV_BASE_URL, JEV_MODEL, JEV_TIMEOUT_SECONDS,
)
from klonet_agent.evals.jev_routing import RoutingBenchmark, load_observations
from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer
from klonet_agent.llm import LLMClient
from klonet_agent.llm.decision import JevDecisionModel, TypeSafeJevClient
from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier


DEFAULT_DATASET = Path(__file__).with_name("data") / "jev_routing_v05.jsonl"
LEGACY_DATASET = Path(__file__).with_name("jev_routing_cases.jsonl")
ROUTES = ("legacy", "jev")


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def select_cases(cases: list[dict], subset: str) -> list[dict]:
    if subset == "stability":
        return [case for case in cases if case.get("stability")]
    return list(cases)


def _answers(value: Any, kind: str) -> dict:
    if kind == "turn":
        intent = value.intent
        answers = {
            "scope": intent.scope,
            "task_type": intent.task_type,
            "operation": intent.operation,
            "requires_retrieval": intent.requires_retrieval,
            "requires_environment_diagnosis": intent.requires_environment_diagnosis,
            "clarification_required": intent.clarification_required,
            "is_correction": intent.is_correction,
        }
    else:
        answers = {
            "intent": value.intent,
            "goal_clarity": value.goal_clarity,
            "goal_relation": value.goal_relation,
            "goal_kind": value.goal_kind,
            "operation": value.operation,
            "scope": value.scope,
            "requires_execution": value.requires_execution,
        }
    route = getattr(value, "knowledge_route", None)
    if route:
        answers["knowledge_route"] = route
    return answers


def _policy_extras(value: Any) -> dict:
    """Adoption flag, reason codes and per-field confidences from the policy."""

    route_decision = getattr(value, "route_decision", None)
    if route_decision is None:
        return {
            "adopted": False,
            "reason_codes": [],
            "confidences": {},
            "knowledge_route": str(getattr(value, "knowledge_route", "") or "legacy"),
        }
    return {
        "adopted": bool(getattr(route_decision, "adopted", False)),
        "reason_codes": list(getattr(route_decision, "reason_codes", ()) or ()),
        "confidences": {
            str(item.name): float(item.confidence)
            for item in (getattr(route_decision, "field_decisions", ()) or ())
        },
        "knowledge_route": str(
            getattr(route_decision, "knowledge_route", "legacy")
        ),
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
            **_policy_extras(value),
        }
    except Exception as exc:
        result = {
            "case_id": case["id"], "repeat": repeat, "answers": {},
            "failed": True, "fallback": False,
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            "input_tokens": 0, "estimated_cost_usd": 0.0,
            "adopted": False, "reason_codes": [], "confidences": {},
            "knowledge_route": "legacy",
        }
    result["latency_seconds"] = perf_counter() - started
    result["route"] = route
    return result


def run(
    cases: list[dict],
    *,
    repeats: int,
    min_confidence: float,
    routes: Iterable[str] = ROUTES,
    routing_mode: str | None = None,
) -> dict[str, list[dict]]:
    selected = [name for name in ROUTES if name in set(routes)]
    if not selected:
        raise ValueError("no route selected")
    wants_jev = "jev" in selected
    key = os.getenv(JEV_API_KEY_ENV, "").strip()
    if wants_jev and not key:
        raise RuntimeError(
            "%s is not configured; use --route legacy or --observations"
            % JEV_API_KEY_ENV
        )
    llm = LLMClient()
    legacy_turn = IntentAnalyzer(llm)
    legacy_privileged = PrivilegedIntentClassifier(llm)
    jev_turn = jev_privileged = None
    if wants_jev:
        jev = JevDecisionModel(TypeSafeJevClient(
            api_key=key, base_url=JEV_BASE_URL, model=JEV_MODEL,
            timeout=JEV_TIMEOUT_SECONDS,
        ))
        jev_turn = IntentAnalyzer(
            llm, decision_model=jev, min_decision_confidence=min_confidence,
            routing_mode=routing_mode,
        )
        jev_privileged = PrivilegedIntentClassifier(
            llm, decision_model=jev, min_decision_confidence=min_confidence,
            routing_mode=routing_mode,
        )
    observations = {name: [] for name in selected}
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

            ordered = [("legacy", legacy_call)]
            if wants_jev:
                ordered.append(("jev", jev_call))
            ordered = [item for item in ordered if item[0] in observations]
            if repeat % 2:
                ordered = list(reversed(ordered))
            for route, call in ordered:
                observations[route].append(_observe(route, case, repeat, call))
    return observations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases", "--dataset", dest="cases", type=Path,
        default=DEFAULT_DATASET,
    )
    parser.add_argument(
        "--repeats", "--repetitions", dest="repeats", type=int, default=1,
    )
    parser.add_argument(
        "--route", choices=["legacy", "jev", "both"], default="both",
    )
    parser.add_argument(
        "--subset", choices=["all", "stability"], default="all",
    )
    parser.add_argument(
        "--alternate-order", action="store_true",
        help="兼容 flag；顺序交替已按 repeat 奇偶内建（文档 §8 阶段 0）。",
    )
    parser.add_argument(
        "--routing-mode", default=None,
        help="覆盖 JEV_ROUTING_MODE（off/shadow/canary/enforced）。",
    )
    parser.add_argument(
        "--observations", type=Path, default=None,
        help="离线模式：直接读取冻结 observations，不调用任何 API。",
    )
    parser.add_argument("--gray-percent", type=int, default=0)
    parser.add_argument("--min-confidence", type=float, default=0.85)
    parser.add_argument(
        "--output-dir", "--output", dest="output_dir", type=Path,
        default=Path(__file__).with_name("results"),
    )
    args = parser.parse_args()

    dataset = args.cases
    if args.observations is None and not Path(dataset).exists():
        # Fall back to the small legacy case file so old invocations still run.
        dataset = LEGACY_DATASET
    cases = load_cases(dataset) if Path(dataset).exists() else []
    cases = select_cases(cases, args.subset)

    if args.observations is not None:
        observations = load_observations(args.observations)
    else:
        routes = ROUTES if args.route == "both" else (args.route,)
        observations = run(
            cases,
            repeats=max(1, args.repeats),
            min_confidence=max(0.0, min(args.min_confidence, 1.0)),
            routes=routes,
            routing_mode=args.routing_mode,
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
