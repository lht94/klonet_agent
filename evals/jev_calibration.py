"""Threshold calibration for the Jev routing policy (plan 05 §7.2).

Consumes frozen observations (never the live config) and reports per-field
reliability plus candidate thresholds.  Gate:

    among candidate thresholds where adopted-route error <= legacy error,
    pick the one with the highest coverage.

This module **never** writes configuration; it only recommends.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Iterable

from klonet_agent.evals.jev_routing import (
    brier_score,
    expected_calibration_error,
    load_observations,
)


DEFAULT_THRESHOLDS = (0.60, 0.65, 0.70, 0.75, 0.78, 0.80, 0.82, 0.85, 0.88, 0.90)


def reliability_buckets(
    pairs: list[tuple[float, bool]], *, bins: int = 10,
) -> list[dict]:
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in pairs:
        buckets[min(int(confidence * bins), bins - 1)].append((confidence, correct))
    result = []
    for index, bucket in enumerate(buckets):
        result.append({
            "bin": "[%.1f, %.1f)" % (index / bins, (index + 1) / bins),
            "count": len(bucket),
            "mean_confidence": round(
                mean([item[0] for item in bucket]), 6,
            ) if bucket else 0.0,
            "mean_accuracy": round(
                mean([item[1] for item in bucket]), 6,
            ) if bucket else 0.0,
        })
    return result


def _field_pairs(observations: dict, expected_key: str) -> dict[str, list]:
    """``{field: [(confidence, correct)]}`` from Jev observations."""

    fields: dict[str, list] = defaultdict(list)
    for rows in observations.values():
        for row in rows:
            if row.get("failed"):
                continue
            answers = row.get("answers") or {}
            if expected_key not in answers:
                continue
            expected_value = answers[expected_key]
            confidences = row.get("confidences") or {}
            for name, confidence in confidences.items():
                if name != expected_key:
                    continue
                try:
                    value = max(0.0, min(float(confidence), 1.0))
                except (TypeError, ValueError):
                    continue
                fields[name].append((value, 1.0 if expected_value else 0.0))
    return fields


def scan_thresholds(
    rows: list[dict],
    *,
    legacy_error: float,
    candidates: Iterable[float] = DEFAULT_THRESHOLDS,
) -> list[dict]:
    """accuracy-coverage curve over ``adopted && min_confidence >= t``."""

    curve = []
    for threshold in candidates:
        adopted = [
            row for row in rows
            if row.get("adopted")
            and _min_confidence(row) >= float(threshold)
            and not row.get("failed")
        ]
        correct = [row for row in adopted if row.get("exact")]
        coverage = (len(adopted) / len(rows)) if rows else 0.0
        error = (
            1.0 - (len(correct) / len(adopted)) if adopted else 1.0
        )
        curve.append({
            "threshold": round(float(threshold), 4),
            "coverage": round(coverage, 6),
            "adopted_route_error": round(error, 6),
            "meets_gate": error <= legacy_error + 1e-9,
        })
    return curve


def _min_confidence(row: dict) -> float:
    confidences = row.get("confidences") or {}
    values = []
    for value in confidences.values():
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return min(values) if values else 0.0


def recommend_threshold(curve: list[dict]) -> dict:
    """Highest coverage among thresholds that satisfy the gate."""

    eligible = [item for item in curve if item["meets_gate"] and item["coverage"] > 0]
    if not eligible:
        return {"threshold": None, "coverage": 0.0, "reason": "no_eligible_threshold"}
    best = max(eligible, key=lambda item: (item["coverage"], item["threshold"]))
    return {
        "threshold": best["threshold"],
        "coverage": best["coverage"],
        "adopted_route_error": best["adopted_route_error"],
        "reason": "max_coverage_under_error_gate",
    }


def build_calibration_report(
    observations: dict,
    *,
    expected_key: str = "requires_retrieval",
) -> dict:
    jev_rows = list(observations.get("jev") or ())
    legacy_rows = list(observations.get("legacy") or ())
    fields = _field_pairs(observations, expected_key)
    legacy_error = _error_rate(legacy_rows)
    curve = scan_thresholds(jev_rows, legacy_error=legacy_error)
    return {
        "expected_key": expected_key,
        "observation_count": {
            "jev": len(jev_rows), "legacy": len(legacy_rows),
        },
        "legacy_route_error": round(legacy_error, 6),
        "field_reliability": {
            name: {
                "count": len(pairs),
                "brier_score": brier_score(pairs),
                "expected_calibration_error": expected_calibration_error(pairs),
                "buckets": reliability_buckets(pairs),
            }
            for name, pairs in sorted(fields.items())
        },
        "threshold_curve": curve,
        "recommendation": recommend_threshold(curve),
        "note": "仅建议，不修改线上配置；阈值需人工审核后写入部署环境变量。",
    }


def _error_rate(rows: list[dict]) -> float:
    successful = [row for row in rows if not row.get("failed")]
    if not successful:
        return 1.0
    wrong = [row for row in successful if not row.get("exact")]
    return len(wrong) / len(successful)


def write_calibration_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "jev-calibration-report.json"
    markdown_path = output_dir / "jev-calibration-report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    markdown_path.write_text(_render(report), encoding="utf-8")
    return json_path, markdown_path


def _render(report: dict) -> str:
    lines = [
        "# Jev v0.5 阈值校准报告",
        "",
        "- 校准字段：`%s`" % report["expected_key"],
        "- 观测数：%s" % json.dumps(report["observation_count"], ensure_ascii=False),
        "- legacy 路由错误率：%s" % report["legacy_route_error"],
        "",
        "## 字段可靠性",
        "",
        "| 字段 | 样本 | Brier | ECE |",
        "|---|---:|---:|---:|",
    ]
    for name, data in report["field_reliability"].items():
        lines.append("| %s | %s | %s | %s |" % (
            name, data["count"], data["brier_score"],
            data["expected_calibration_error"],
        ))
    lines.extend([
        "",
        "## 阈值曲线（门禁：采用后错误率 <= legacy 错误率）",
        "",
        "| 阈值 | Coverage | 采用后错误率 | 满足门禁 |",
        "|---:|---:|---:|---|",
    ])
    for item in report["threshold_curve"]:
        lines.append("| %s | %s | %s | %s |" % (
            item["threshold"], item["coverage"],
            item["adopted_route_error"], "是" if item["meets_gate"] else "否",
        ))
    lines.extend([
        "",
        "## 推荐",
        "",
        "```json",
        json.dumps(report["recommendation"], ensure_ascii=False, indent=2),
        "```",
        "",
        report["note"],
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--expected-key", default="requires_retrieval")
    parser.add_argument(
        "--output", "--output-dir", dest="output_dir", type=Path,
        default=Path(__file__).with_name("results") / "jev_v05_calibration",
    )
    args = parser.parse_args()
    observations = load_observations(args.observations)
    report = build_calibration_report(
        observations, expected_key=args.expected_key,
    )
    json_path, markdown_path = write_calibration_report(report, args.output_dir)
    print("json_report=%s" % json_path)
    print("markdown_report=%s" % markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
