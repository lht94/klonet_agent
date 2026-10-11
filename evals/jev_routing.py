"""Reproducible metrics for legacy-versus-Jev routing evaluations."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


def load_observations(path: Path | str) -> dict[str, list[dict]]:
    """Read a frozen observations JSONL grouped by route.

    Each line is one observation with a ``route`` key (``legacy``/``jev``).
    """

    grouped: dict[str, list[dict]] = {}
    file_path = Path(path)
    if not file_path.exists():
        return grouped
    for line in file_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        route = str(record.pop("route", "legacy"))
        grouped.setdefault(route, []).append(record)
    return grouped


def dump_observations(observations: dict[str, list[dict]], path: Path | str) -> Path:
    """Persist observations so reports can be regenerated offline."""

    lines = []
    for route, rows in observations.items():
        for row in rows:
            payload = dict(row)
            payload["route"] = route
            lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return file_path


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
        exact_pairs: list[tuple[dict, bool]] = []
        adopted_exact: list[bool] = []
        dangerous_misses = 0
        field_pairs: dict[str, list[tuple[str, str]]] = defaultdict(list)
        route_pairs: list[tuple[str, str]] = []
        probability_pairs: list[tuple[float, bool]] = []
        fallback_reasons: Counter = Counter()
        failures: list[dict] = []
        for row in successful:
            case = self._case_by_id.get(str(row.get("case_id")))
            if case is None:
                continue
            expected = dict(case.get("expected") or {})
            actual = dict(row.get("answers") or {})
            matched = all(actual.get(key) == value for key, value in expected.items())
            exact_pairs.append((row, matched))
            if _is_adopted(row):
                adopted_exact.append(matched)
            for key, expected_value in expected.items():
                field_pairs[key].append((str(expected_value), str(actual.get(key))))
                if isinstance(expected_value, bool):
                    probability_pairs.extend(
                        _probability_pair(row, key, expected_value)
                    )
            if case.get("expected_route"):
                route_pairs.append((
                    str(case["expected_route"]),
                    str(row.get("knowledge_route") or ""),
                ))
            if (
                expected.get("intent") == "mutating_action"
                and actual.get("intent") != "mutating_action"
            ):
                dangerous_misses += 1
        for row in rows:
            for code in row.get("reason_codes") or ():
                fallback_reasons[str(code)] += 1
            if row.get("failed"):
                failures.append({
                    "case_id": str(row.get("case_id")),
                    "repeat": row.get("repeat"),
                    "error": str(row.get("error") or "unknown")[:200],
                })
            elif row.get("fallback"):
                failures.append({
                    "case_id": str(row.get("case_id")),
                    "repeat": row.get("repeat"),
                    "error": "fallback:%s" % (
                        ",".join(str(c) for c in (row.get("reason_codes") or ()))
                        or "unspecified"
                    ),
                })
        total = len(rows)
        latencies = [float(row.get("latency_seconds") or 0.0) for row in rows]
        token_values = [int(row.get("input_tokens") or 0) for row in rows]
        costs = [float(row.get("estimated_cost_usd") or 0.0) for row in rows]
        exact_values = [matched for _row, matched in exact_pairs]
        return {
            "observation_count": total,
            "exact_match_rate": round(_rate(exact_values), 6),
            "coverage": round(
                (len(adopted_exact) / len(successful)) if successful else 0.0, 6,
            ),
            "selective_accuracy": round(_rate(adopted_exact), 6),
            "field_metrics": {
                key: _classification_metrics(pairs)
                for key, pairs in sorted(field_pairs.items())
            },
            "knowledge_route_confusion": {
                "%s -> %s" % pair: count
                for pair, count in Counter(route_pairs).most_common()
            },
            "brier_score": brier_score(probability_pairs),
            "expected_calibration_error": expected_calibration_error(
                probability_pairs,
            ),
            "fallback_reason_counts": dict(sorted(fallback_reasons.items())),
            "failure_details": failures,
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


def _is_adopted(row: dict) -> bool:
    """Whether this observation's route was actually adopted by the policy."""

    if row.get("failed"):
        return False
    if "adopted" in row:
        return bool(row.get("adopted"))
    return not bool(row.get("fallback"))


def _probability_pair(
    row: dict,
    key: str,
    expected_value: Any,
) -> list:
    """``(confidence, correct)`` for one answered field, or ``[]``."""

    confidences = row.get("confidences")
    if not isinstance(confidences, dict) or key not in confidences:
        return []
    try:
        confidence = max(0.0, min(float(confidences[key]), 1.0))
    except (TypeError, ValueError):
        return []
    actual = (row.get("answers") or {}).get(key)
    return [(confidence, 1.0 if actual == expected_value else 0.0)]


def brier_score(pairs: list) -> float:
    """Mean squared error between stated confidence and correctness."""

    if not pairs:
        return 0.0
    return round(
        sum((confidence - correct) ** 2 for confidence, correct in pairs)
        / len(pairs),
        6,
    )


def expected_calibration_error(pairs: list, *, bins: int = 10) -> float:
    """Expected Calibration Error over equal-width confidence bins."""

    if not pairs or bins <= 0:
        return 0.0
    buckets: list[list] = [[] for _ in range(bins)]
    for confidence, correct in pairs:
        index = min(int(confidence * bins), bins - 1)
        buckets[index].append((confidence, correct))
    total = len(pairs)
    error = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        mean_confidence = sum(item[0] for item in bucket) / len(bucket)
        mean_accuracy = sum(item[1] for item in bucket) / len(bucket)
        error += (len(bucket) / total) * abs(mean_confidence - mean_accuracy)
    return round(error, 6)


def accuracy_coverage_curve(
    rows: list[tuple[float, bool]], thresholds: tuple = (0.5, 0.6, 0.7, 0.8, 0.9),
) -> list:
    """``[{threshold, coverage, accuracy}]`` over (confidence, correct) rows."""

    curve = []
    for threshold in thresholds:
        selected = [correct for confidence, correct in rows if confidence >= threshold]
        curve.append({
            "threshold": round(float(threshold), 4),
            "coverage": round(len(selected) / len(rows), 6) if rows else 0.0,
            "accuracy": round(_rate(selected), 6),
        })
    return curve


def store_selection_accuracy(expected: Iterable[str], actual: Iterable[str]) -> float:
    pairs = list(zip(list(expected), list(actual)))
    return round(_rate(a == b for a, b in pairs), 6)


def recall_at_k(ranked: Iterable[str], expected: Iterable[str], k: int) -> float:
    """Fraction of expected sources found in the first ``k`` ranked results."""

    wanted = {str(item) for item in expected}
    if not wanted:
        return 0.0
    hit = wanted.intersection({str(item) for item in list(ranked)[:max(0, int(k))]})
    return round(len(hit) / len(wanted), 6)


def reciprocal_rank(ranked: Iterable[str], expected: Iterable[str]) -> float:
    wanted = {str(item) for item in expected}
    if not wanted:
        return 0.0
    for index, item in enumerate(list(ranked), start=1):
        if str(item) in wanted:
            return round(1.0 / index, 6)
    return 0.0


def mrr_at_k(rows: list, k: int) -> float:
    """``rows`` = list of ``(ranked, expected)`` tuples."""

    return round(mean([_mrr_single(ranked, expected, k) for ranked, expected in rows]), 6) \
        if rows else 0.0


def _mrr_single(ranked: Iterable[str], expected: Iterable[str], k: int) -> float:
    wanted = {str(item) for item in expected}
    if not wanted:
        return 0.0
    for index, item in enumerate(list(ranked)[:max(0, int(k))], start=1):
        if str(item) in wanted:
            return 1.0 / index
    return 0.0


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
        "| 路由 | 完全匹配率 | 采用覆盖率 | 采用后准确率 | 重复一致率 | 危险动作漏判 | 失败率 | 回退率 | P50 | P95 | P99 | 成本(USD) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("legacy", "jev"):
        data = report.get(name, {})
        latency = data.get("latency", {})
        lines.append(
            "| %s | %.2f%% | %.2f%% | %.2f%% | %.2f%% | %s | %.2f%% | %.2f%% | %.3f | %.3f | %.3f | %.6f |"
            % (
                name,
                100 * data.get("exact_match_rate", 0.0),
                100 * data.get("coverage", 0.0),
                100 * data.get("selective_accuracy", 0.0),
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
    ])

    for name in ("legacy", "jev"):
        data = report.get(name, {})
        lines.extend([
            "",
            "## %s 校准、回退与失败明细" % name,
            "",
            "- Brier score：%s" % data.get("brier_score", 0.0),
            "- ECE(10 等宽桶)：%s" % data.get("expected_calibration_error", 0.0),
            "- 知识库路由四分类混淆：`%s`" % json.dumps(
                data.get("knowledge_route_confusion", {}), ensure_ascii=False,
            ),
            "- 回退原因分布：`%s`" % json.dumps(
                data.get("fallback_reason_counts", {}), ensure_ascii=False,
            ),
            "",
        ])
        details = data.get("failure_details") or []
        lines.append("### 失败与回退明细（%d 条）" % len(details))
        lines.append("")
        if details:
            lines.append("| case_id | repeat | 原因 |")
            lines.append("|---|---:|---|")
            for item in details[:200]:
                lines.append("| %s | %s | %s |" % (
                    item.get("case_id"), item.get("repeat"), item.get("error"),
                ))
        else:
            lines.append("（无）")

    lines.extend([
        "",
        "## 不一致案例",
        "",
        "```json",
        json.dumps(report["disagreements"], ensure_ascii=False, indent=2),
        "```",
    ])
    return "\n".join(lines) + "\n"
