"""Real Hybrid-RAG retrieval quality for Jev store routes (plan 05 §7.3).

Reports both *routing* quality (which store was selected) and *retrieval*
quality (Recall@K / MRR@5), and — critically — records the semantic channel
state (``retrieval_mode`` / ``vector_status`` / ``rerank_status``) for every
row.  When the embedding endpoint is degraded the Recall/MRR numbers only
describe a BM25-only pipeline and must not be used as a promotion gate.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Iterable

from klonet_agent.evals.jev_routing import (
    mrr_at_k,
    store_selection_accuracy,
)


DEFAULT_DATASET = Path(__file__).with_name("data") / "jev_routing_v05.jsonl"


def _normalize(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip()


def _matches(actual_path: str, expected: str) -> bool:
    actual = _normalize(actual_path)
    wanted = _normalize(expected)
    if not wanted:
        return False
    return actual == wanted or actual.endswith(wanted) or wanted in actual


def ranked_paths(outcome: Any) -> list[str]:
    return [
        _normalize(getattr(chunk, "path", ""))
        for chunk in (getattr(outcome, "results", None) or [])
    ]


def planned_stores(outcome: Any) -> tuple[str, ...]:
    plan = getattr(outcome, "retrieval_plan", None)
    if plan is None:
        return ()
    return tuple(
        str(getattr(task, "store", ""))
        for task in (getattr(plan, "retrieval_tasks", ()) or ())
    )


def channel_state(outcome: Any) -> dict:
    """Semantic-channel state so degraded runs cannot masquerade as real ones."""

    vector_status = str(getattr(outcome, "vector_status", "") or "unknown")
    rerank_status = str(getattr(outcome, "rerank_status", "") or "unknown")
    degraded = vector_status not in {"ok", "ready", "loaded", "available"}
    return {
        "retrieval_mode": str(getattr(outcome, "retrieval_mode", "") or "unknown"),
        "vector_status": vector_status,
        "rerank_status": rerank_status,
        "semantic_channel": "degraded" if degraded else "available",
    }


def evaluate_case(case: dict, retriever: Callable[[str], Any]) -> dict:
    expected_route = str(case.get("expected_route") or "")
    expected_sources = [str(item) for item in (case.get("expected_sources") or [])]
    row = {
        "case_id": case.get("id"),
        "category": case.get("category", ""),
        "expected_route": expected_route,
        "expected_sources": expected_sources,
    }
    try:
        outcome = retriever(case["prompt"])
    except Exception as exc:
        row.update({
            "failed": True,
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
            "planned_stores": [], "ranked": [], "hit_at_3": False,
            "hit_at_5": False, "reciprocal_rank_at_5": 0.0,
            "empty": True, "store_correct": False,
            "planner_violation": False,
            "retrieval_mode": "error", "vector_status": "error",
            "rerank_status": "error", "semantic_channel": "degraded",
        })
        return row

    plan = getattr(outcome, "retrieval_plan", None)
    stores = planned_stores(outcome)
    ranked = ranked_paths(outcome)
    state = channel_state(outcome)
    top3 = ranked[:3]
    top5 = ranked[:5]
    row.update({
        "failed": False,
        "planned_stores": list(stores),
        "ranked": ranked,
        "hit_at_3": any(
            any(_matches(path, wanted) for wanted in expected_sources)
            for path in top3
        ),
        "hit_at_5": any(
            any(_matches(path, wanted) for wanted in expected_sources)
            for path in top5
        ),
        "reciprocal_rank_at_5": _first_hit_rank(top5, expected_sources),
        "empty": not ranked,
        "store_correct": _store_correct(expected_route, stores),
        "planner_violation": str(getattr(plan, "detail", "")) ==
        "planner_store_violation",
        **state,
    })
    return row


def _store_correct(expected_route: str, stores: Iterable[str]) -> bool:
    if not expected_route or expected_route == "legacy":
        return True
    wanted = {
        "no_retrieval": set(),
        "public_docs": {"public_docs"},
        "source_code": {"source_code"},
        "both": {"public_docs", "source_code"},
    }.get(expected_route)
    if wanted is None:
        return True
    return set(stores) == wanted


def _first_hit_rank(ranked: list[str], expected_sources: list[str]) -> float:
    for index, path in enumerate(ranked, start=1):
        if any(_matches(path, wanted) for wanted in expected_sources):
            return round(1.0 / index, 6)
    return 0.0


def build_report(rows: list[dict]) -> dict:
    scored = [row for row in rows if row.get("expected_sources")]
    routed = [row for row in rows if row.get("expected_route")]
    channel_counts = Counter(row.get("semantic_channel") for row in rows)
    vector_counts = Counter(row.get("vector_status") for row in rows)
    return {
        "case_count": len(rows),
        "scored_case_count": len(scored),
        "semantic_channel": (
            "degraded" if channel_counts.get("degraded") else "available"
        ),
        "channel_counts": dict(channel_counts),
        "vector_status_counts": dict(vector_counts),
        "store_selection_accuracy": store_selection_accuracy(
            [row["expected_route"] for row in routed],
            [_route_label(row["planned_stores"]) for row in routed],
        ),
        "recall_at_3": round(
            mean([1.0 if row["hit_at_3"] else 0.0 for row in scored]), 6,
        ) if scored else 0.0,
        "recall_at_5": round(
            mean([1.0 if row["hit_at_5"] else 0.0 for row in scored]), 6,
        ) if scored else 0.0,
        "mrr_at_5": mrr_at_k(
            [(row["ranked"], row["expected_sources"]) for row in scored], 5,
        ),
        "empty_result_rate": round(
            mean([1.0 if row["empty"] else 0.0 for row in rows]), 6,
        ) if rows else 0.0,
        "irrelevant_store_call_rate": round(
            mean([0.0 if row["store_correct"] else 1.0 for row in routed]), 6,
        ) if routed else 0.0,
        "cross_store_coverage": _cross_store_coverage(rows),
        "planner_store_violations": sum(
            1 for row in rows if row.get("planner_violation")
        ),
        "rows": rows,
    }


def _route_label(stores: Iterable[str]) -> str:
    selected = {str(store) for store in stores}
    if not selected:
        return "no_retrieval"
    if selected == {"public_docs", "source_code"}:
        return "both"
    if "public_docs" in selected:
        return "public_docs"
    if "source_code" in selected:
        return "source_code"
    return "unknown"


def _cross_store_coverage(rows: list[dict]) -> float:
    both = [row for row in rows if row.get("expected_route") == "both"]
    if not both:
        return 0.0
    covered = 0
    for row in both:
        stores = set(row.get("planned_stores") or ())
        if stores == {"public_docs", "source_code"}:
            covered += 1
    return round(covered / len(both), 6)


def write_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "jev-retrieval-report.json"
    markdown_path = output_dir / "jev-retrieval-report.md"
    slim = {key: value for key, value in report.items() if key != "rows"}
    json_path.write_text(
        json.dumps(slim, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    markdown_path.write_text(_render(report), encoding="utf-8")
    return json_path, markdown_path


def _render(report: dict) -> str:
    lines = [
        "# Jev v0.5 真实检索评测",
        "",
        "> ⚠️ 语义通道状态：**%s**（vector_status=%s）。降级时 Recall/MRR 仅为 "
        "BM25 口径，不得作为晋级依据。" % (
            report["semantic_channel"],
            json.dumps(report["vector_status_counts"], ensure_ascii=False),
        ),
        "",
        "- 案例数：%s（含 expected_sources 的计分案例：%s）" % (
            report["case_count"], report["scored_case_count"],
        ),
        "- Store selection accuracy：%s" % report["store_selection_accuracy"],
        "- Recall@3：%s / Recall@5：%s / MRR@5：%s" % (
            report["recall_at_3"], report["recall_at_5"], report["mrr_at_5"],
        ),
        "- 空结果率：%s" % report["empty_result_rate"],
        "- 无关知识库调用率：%s" % report["irrelevant_store_call_rate"],
        "- 双库跨库证据覆盖率：%s" % report["cross_store_coverage"],
        "- planner store violation：%s" % report["planner_store_violations"],
        "",
        "| case_id | 期望路由 | 实际 store | 语义通道 | 命中@5 | MRR@5 |",
        "|---|---|---|---|---:|---:|",
    ]
    for row in report.get("rows", []):
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            row.get("case_id"),
            row.get("expected_route"),
            ",".join(row.get("planned_stores") or []) or "-",
            row.get("semantic_channel"),
            "Y" if row.get("hit_at_5") else "N",
            row.get("reciprocal_rank_at_5"),
        ))
    return "\n".join(lines) + "\n"


def default_retriever() -> Callable[[str], Any]:
    from klonet_agent.knowledge.multi_stage import MultiStageRetriever
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    retriever = MultiStageRetriever(
        public_retriever=KnowledgeRetriever(),
        source_retriever=KnowledgeRetriever(),
    )

    def call(query: str) -> Any:
        return retriever.search(query, top_k=5, task_type="auto")

    return call


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--output", "--output-dir", dest="output_dir", type=Path,
        default=Path(__file__).with_name("results") / "jev_v05_retrieval",
    )
    args = parser.parse_args()
    cases = _load_cases(args.dataset)
    if args.limit:
        cases = cases[: args.limit]
    retriever = default_retriever()
    rows = [evaluate_case(case, retriever) for case in cases]
    report = build_report(rows)
    json_path, markdown_path = write_report(report, args.output_dir)
    print("json_report=%s" % json_path)
    print("markdown_report=%s" % markdown_path)
    return 0


def _load_cases(path: Path) -> list[dict]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    return [
        json.loads(line)
        for line in file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


if __name__ == "__main__":
    raise SystemExit(main())
