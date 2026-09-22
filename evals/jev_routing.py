"""Reproducible metrics for legacy-versus-Jev routing evaluations."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


class RoutingBenchmark:
    def __init__(self, cases: list[dict], observations: dict[str, list[dict]]):
        self.cases = list(cases)
        self.observations = {
            str(name): list(rows) for name, rows in observations.items()
        }
        self._case_by_id = {str(case["id"]): case for case in self.cases}

    def build_report(self, *, gray_percent: int) -> dict:
        routes = {
            name: self._route_metrics(rows)
            for name, rows in self.observations.items()
        }
        legacy_p95 = routes.get("legacy", {}).get("latency", {}).get("p95", 0.0)
        jev_p95 = routes.get("jev", {}).get("latency", {}).get("p95", 0.0)
        ratio = ((legacy_p95 - jev_p95) / legacy_p95) if legacy_p95 else 0.0
        multiple = (legacy_p95 / jev_p95) if jev_p95 else 0.0
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {
                "gray_percent": int(gray_percent),
                "case_count": len(self.cases),
            },
            **routes,
            "performance_comparison": {
                "p95_speedup_ratio": round(ratio, 6),
                "p95_speedup_multiple": round(multiple, 6),
            },
            "disagreements": self._disagreements(),
        }
        return report

    def write_report(self, output_dir: Path, *, gray_percent: int):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        report = self.build_report(gray_percent=gray_percent)
        json_path = output_dir / "jev-routing-report.json"
        markdown_path = output_dir / "jev-routing-report.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        markdown_path.write_text(_render_markdown(report), encoding="utf-8")
        return json_path, markdown_path

    def _route_metrics(self, rows: list[dict]) -> dict:
        successful = [row for row in rows if not row.get("failed")]
        exact = []
        dangerous_misses = 0
        field_pairs: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for row in successful:
            case = self._case_by_id.get(str(row.get("case_id")))
            if case is None:
                continue
            expected = dict(case.get("expected") or {})
            actual = dict(row.get("answers") or {})
            exact.append(all(actual.get(key) == value for key, value in expected.items()))
            for key, expected_value in expected.items():
                field_pairs[key].append((str(expected_value), str(actual.get(key))))
            if (
                expected.get("intent") == "mutating_action"
                and actual.get("intent") != "mutating_action"
            ):
                dangerous_misses += 1
        total = len(rows)
        latencies = [float(row.get("latency_seconds") or 0.0) for row in rows]
        token_values = [int(row.get("input_tokens") or 0) for row in rows]
        costs = [float(row.get("estimated_cost_usd") or 0.0) for row in rows]
        return {
            "observation_count": total,
            "exact_match_rate": round(_rate(exact), 6),
            "field_metrics": {
                key: _classification_metrics(pairs)
                for key, pairs in sorted(field_pairs.items())
            },
            "route_consistency_rate": round(
                self._consistency_rate(successful), 6,
            ),
            "dangerous_action_misses": dangerous_misses,
            "failure_rate": round(
                sum(bool(row.get("failed")) for row in rows) / total
                if total else 0.0,
                6,
            ),
            "fallback_rate": round(
                sum(bool(row.get("fallback")) for row in rows) / total
                if total else 0.0,
                6,
            ),
            "latency": _latency_metrics(latencies),
            "mean_input_tokens": round(mean(token_values), 3) if token_values else 0.0,
            "total_estimated_cost_usd": round(sum(costs), 9),
        }

    @staticmethod
    def _consistency_rate(rows: list[dict]) -> float:
        by_case: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            by_case[str(row.get("case_id"))].append(
                json.dumps(row.get("answers") or {}, sort_keys=True, ensure_ascii=False)
            )
        if not by_case:
            return 0.0
        consistent = sum(len(set(values)) == 1 for values in by_case.values())
        return consistent / len(by_case)

    def _disagreements(self) -> list[dict]:
        first: dict[str, dict[str, dict]] = defaultdict(dict)
        for route, rows in self.observations.items():
            for row in rows:
                case_id = str(row.get("case_id"))
                first[case_id].setdefault(route, row)
        result = []
        for case_id, routes in first.items():
            legacy = dict(routes.get("legacy", {}).get("answers") or {})
            jev = dict(routes.get("jev", {}).get("answers") or {})
            if legacy != jev:
                result.append({
                    "case_id": case_id,
                    "expected": self._case_by_id.get(case_id, {}).get("expected", {}),
                    "legacy": legacy,
                    "jev": jev,
                })
        return result


def _rate(values: Iterable[bool]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _classification_metrics(pairs: list[tuple[str, str]]) -> dict:
    labels = sorted({item for pair in pairs for item in pair})
    accuracy = _rate(expected == actual for expected, actual in pairs)
    f1_values = []
    for label in labels:
        tp = sum(expected == label and actual == label for expected, actual in pairs)
        fp = sum(expected != label and actual == label for expected, actual in pairs)
        fn = sum(expected == label and actual != label for expected, actual in pairs)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1_values.append(
            2 * precision * recall / (precision + recall)
            if precision + recall else 0.0
        )
    return {
        "accuracy": round(accuracy, 6),
        "macro_f1": round(mean(f1_values), 6) if f1_values else 0.0,
        "confusion": {
            "%s -> %s" % pair: count for pair, count in Counter(pairs).items()
        },
    }


def _latency_metrics(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "mean": round(mean(ordered), 6) if ordered else 0.0,
        "p50": round(_percentile(ordered, 0.50), 6),
        "p90": round(_percentile(ordered, 0.90), 6),
        "p95": round(_percentile(ordered, 0.95), 6),
        "p99": round(_percentile(ordered, 0.99), 6),
    }


def _percentile(ordered: list[float], quantile: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, min(round((len(ordered) - 1) * quantile), len(ordered) - 1))
    return ordered[index]


def _render_markdown(report: dict) -> str:
    lines = [
        "# Jev 路由对比报告",
        "",
        "- 灰度比例：%s%%" % report["config"]["gray_percent"],
        "- 案例数：%s" % report["config"]["case_count"],
        "",
        "| 路由 | 完全匹配率 | 重复一致率 | 危险动作漏判 | 失败率 | 回退率 | P50 | P95 | P99 | 成本(USD) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("legacy", "jev"):
        data = report.get(name, {})
        latency = data.get("latency", {})
        lines.append(
            "| %s | %.2f%% | %.2f%% | %s | %.2f%% | %.2f%% | %.3f | %.3f | %.3f | %.6f |"
            % (
                name,
                100 * data.get("exact_match_rate", 0.0),
                100 * data.get("route_consistency_rate", 0.0),
                data.get("dangerous_action_misses", 0),
                100 * data.get("failure_rate", 0.0),
                100 * data.get("fallback_rate", 0.0),
                latency.get("p50", 0.0),
                latency.get("p95", 0.0),
                latency.get("p99", 0.0),
                data.get("total_estimated_cost_usd", 0.0),
            )
        )
    comparison = report["performance_comparison"]
    lines.extend([
        "",
        "- P95 加速比例：%.2f%%" % (100 * comparison["p95_speedup_ratio"]),
        "- P95 加速倍数：%.3fx" % comparison["p95_speedup_multiple"],
        "",
        "## 不一致案例",
        "",
        "```json",
        json.dumps(report["disagreements"], ensure_ascii=False, indent=2),
        "```",
    ])
    return "\n".join(lines) + "\n"
