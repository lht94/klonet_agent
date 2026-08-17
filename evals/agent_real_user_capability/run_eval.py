"""Run the real-user capability checklist with isolated and live evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ids", default="")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--live-timeout", type=int, default=180)
    return parser.parse_args()


def run_contract(case, root: Path, python: str) -> dict:
    started = time.monotonic()
    basetemp = root / "pytest" / case["id"]
    # pytest creates the final basetemp itself, but does not create missing
    # ancestors.  Keep each scenario isolated without turning fixture setup
    # failures into false product failures.
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    command = [
        python, "-m", "pytest", "-q", *case["tests"],
        "--basetemp", str(basetemp),
    ]
    completed = subprocess.run(
        command, cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "return_code": completed.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "command": command,
        "output": (completed.stdout + completed.stderr)[-12000:],
    }


def run_live(case, root: Path, python: str, timeout: int) -> dict | None:
    contract = case.get("live")
    if not isinstance(contract, dict):
        return None
    started = time.monotonic()
    command = [
        python, "-m", "klonet_agent.agent", "--mode", "ops-privilege",
        "--user-id", "real-capability-eval-" + case["id"].lower(),
        "--project-id", "round",
    ]
    try:
        completed = subprocess.run(
            command, cwd=str(Path(__file__).resolve().parents[2]),
            input=case["prompt"] + "\n", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        output = completed.stdout + completed.stderr
        missing = [item for item in contract.get("contains", []) if item not in output]
        forbidden = [item for item in contract.get("excludes", []) if item in output]
        status = "passed" if completed.returncode == 0 and not missing and not forbidden else "failed"
        error = ""
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        missing = list(contract.get("contains", []))
        forbidden = []
        status = "failed"
        error = "timeout"
    destination = root / "live" / (case["id"] + ".txt")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(output, encoding="utf-8")
    return {
        "status": status,
        "duration_seconds": round(time.monotonic() - started, 3),
        "missing": missing,
        "forbidden": forbidden,
        "error": error,
        "transcript": str(destination),
    }


def render_report(rows: list[dict], destination: Path) -> None:
    passed = sum(row["overall_status"] == "passed" for row in rows)
    lines = [
        "# Agent 真实用户需求能力评测报告",
        "",
        "- 生成时间：%s" % datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "- 用例数：%s" % len(rows),
        "- 通过：%s" % passed,
        "- 失败：%s" % (len(rows) - passed),
        "",
        "| ID | 场景 | 隔离合同 | 真实会话 | 总结果 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        live = row.get("live")
        lines.append(
            "| %s | %s | %s | %s | %s |" % (
                row["id"], row["title"], row["contract"]["status"],
                live["status"] if live else "未要求（隔离验收）",
                row["overall_status"],
            )
        )
    failures = [row for row in rows if row["overall_status"] != "passed"]
    lines.extend(["", "## 失败详情", ""])
    if not failures:
        lines.append("无。")
    for row in failures:
        lines.extend(["### %s %s" % (row["id"], row["title"]), ""])
        if row["contract"]["status"] != "passed":
            lines.append("- 隔离合同失败：`%s`" % row["contract"]["output"].replace("`", "'")[-1000:])
        if row.get("live") and row["live"]["status"] != "passed":
            lines.append("- 真实会话缺失：%s" % (row["live"]["missing"] or "无"))
            lines.append("- 真实会话禁止项：%s" % (row["live"]["forbidden"] or "无"))
            lines.append("- 原始输出：`%s`" % row["live"]["transcript"])
        lines.append("")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    args = parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    selected = {item for item in args.ids.split(",") if item}
    if selected:
        cases = [case for case in cases if case["id"] in selected]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases:
        contract = run_contract(case, args.output_dir, args.python)
        live = run_live(case, args.output_dir, args.python, args.live_timeout) if args.live else None
        overall = contract["status"]
        if live is not None and live["status"] != "passed":
            overall = "failed"
        row = {
            "id": case["id"], "title": case["title"], "prompt": case["prompt"],
            "contract": contract, "live": live, "overall_status": overall,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    (args.output_dir / "results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    render_report(rows, args.output_dir / "REPORT.md")
    return 0 if all(row["overall_status"] == "passed" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
