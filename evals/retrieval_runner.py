"""检索路由、召回、拒答和性能评估。"""

from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

from klonet_agent.config import PROJECT_ROOT
from klonet_agent.knowledge.models import SearchRequest
from klonet_agent.knowledge.retriever import KnowledgeRetriever
from klonet_agent.knowledge.router import DEFAULT_QUERY_ROUTER, QueryRouter


class RetrievalEvalRunner:
    """使用固定 case 计算可重复的检索指标。"""

    def __init__(
        self,
        case_file: Path = PROJECT_ROOT / "evals" / "retrieval_cases.jsonl",
        output_file: Path = PROJECT_ROOT / "evals" / "retrieval_summary.md",
        retriever: KnowledgeRetriever | None = None,
        router: QueryRouter | None = None,
    ):
        self.case_file = case_file
        self.output_file = output_file
        self.retriever = retriever or KnowledgeRetriever()
        self.router = router or DEFAULT_QUERY_ROUTER

    def run(self, write_summary: bool = True) -> dict:
        """执行离线评估并返回指标。"""

        cases = self._load_cases()
        rows = [self._evaluate(case) for case in cases]
        total = len(rows)
        retrieval_rows = [row for row in rows if row["expected_paths"]]
        abstention_rows = [row for row in rows if not row["should_retrieve"]]

        metrics = {
            "total": total,
            "scope_accuracy": _ratio(rows, "scope_correct"),
            "task_type_accuracy": _ratio(rows, "task_type_correct"),
            "recall_at_3": _ratio(retrieval_rows, "hit_at_3"),
            "recall_at_10": _ratio(retrieval_rows, "hit_at_10"),
            "mrr": _average([row["reciprocal_rank"] for row in retrieval_rows]),
            "abstention_accuracy": _ratio(abstention_rows, "abstention_correct"),
            "general_rag_false_positive_rate": _average(
                [
                    0.0 if row["abstention_correct"] else 1.0
                    for row in abstention_rows
                ]
            ),
            "avg_latency_ms": round(
                _average([row["latency_ms"] for row in rows]),
                3,
            ),
            "rows": rows,
        }
        if write_summary:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            self.output_file.write_text(
                self._render_summary(metrics),
                encoding="utf-8",
            )
        return metrics

    def _load_cases(self) -> list[dict]:
        cases = []
        with self.case_file.open("r", encoding="utf-8") as file:
            for line_no, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                case = json.loads(line)
                _validate_case(case, line_no)
                cases.append(case)
        return cases

    def _evaluate(self, case: dict) -> dict:
        route = self.router.route(case["query"])
        start = perf_counter()

        skip_rag = (
            route.hard_disable_rag
            or (route.scope == "general" and route.confidence >= 0.8)
        )
        if skip_rag:
            results = []
            status = "none"
        else:
            outcome = self.retriever.search_request(
                SearchRequest(
                    query=case["query"],
                    task_type=route.task_type,
                    domains=route.domains or None,
                    top_k=10,
                )
            )
            results = outcome.results
            status = outcome.status

        latency_ms = (perf_counter() - start) * 1000
        paths = [result.path for result in results]
        expected_paths = case.get("expected_paths", [])
        relevant_ranks = [
            index
            for index, path in enumerate(paths, start=1)
            if path in expected_paths
        ]
        first_rank = min(relevant_ranks) if relevant_ranks else None
        should_retrieve = bool(case["should_retrieve"])

        return {
            "id": case["id"],
            "query": case["query"],
            "expected_paths": expected_paths,
            "should_retrieve": should_retrieve,
            "scope": route.scope,
            "task_type": route.task_type,
            "status": status,
            "paths": paths,
            "scope_correct": route.scope == case["expected_scope"],
            "task_type_correct": route.task_type == case["expected_task_type"],
            "hit_at_3": bool(first_rank and first_rank <= 3),
            "hit_at_10": bool(first_rank and first_rank <= 10),
            "reciprocal_rank": 1 / first_rank if first_rank else 0.0,
            "abstention_correct": (not should_retrieve and not results),
            "latency_ms": round(latency_ms, 3),
        }

    def _render_summary(self, metrics: dict) -> str:
        lines = [
            "# Klonet Retrieval Eval",
            "",
            f"- cases: {metrics['total']}",
            f"- scope_accuracy: {metrics['scope_accuracy']:.3f}",
            f"- task_type_accuracy: {metrics['task_type_accuracy']:.3f}",
            f"- recall_at_3: {metrics['recall_at_3']:.3f}",
            f"- recall_at_10: {metrics['recall_at_10']:.3f}",
            f"- mrr: {metrics['mrr']:.3f}",
            f"- abstention_accuracy: {metrics['abstention_accuracy']:.3f}",
            (
                "- general_rag_false_positive_rate: "
                f"{metrics['general_rag_false_positive_rate']:.3f}"
            ),
            f"- avg_latency_ms: {metrics['avg_latency_ms']:.3f}",
            "",
            "## Cases",
        ]
        for row in metrics["rows"]:
            lines.extend(
                [
                    "",
                    f"### {row['id']}",
                    f"- scope: {row['scope']}",
                    f"- task_type: {row['task_type']}",
                    f"- status: {row['status']}",
                    f"- paths: {row['paths'][:3]}",
                    f"- latency_ms: {row['latency_ms']}",
                ]
            )
        return "\n".join(lines) + "\n"


def _validate_case(case: dict, line_no: int):
    required = {
        "id",
        "query",
        "expected_scope",
        "expected_task_type",
        "expected_paths",
        "should_retrieve",
    }
    missing = sorted(required - set(case))
    if missing:
        raise ValueError(f"retrieval case line {line_no} 缺少字段：{missing}")


def _ratio(rows: list[dict], key: str) -> float:
    if not rows:
        return 0.0
    return sum(bool(row[key]) for row in rows) / len(rows)


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def main():
    metrics = RetrievalEvalRunner().run()
    print(json.dumps(
        {key: value for key, value in metrics.items() if key != "rows"},
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
