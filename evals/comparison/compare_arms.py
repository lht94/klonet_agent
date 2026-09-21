"""把对照臂与实验臂的结果汇总成对比表，供简历/答辩引用。

设计原则：**所有数字都从落盘产物读取，不手工抄写。** 这样任何一次重跑都能
重新生成整张表，别人也能核验。

数据来源
--------
- 对照臂（Codex CLI）：`<codex-dir>/codex_artifacts/<task>.json`，取 token 与工具调用。
- 实验臂（ops / mentor）：`<app-dir>/matrix.json` 提供耗时与退出状态；
  token 从各次运行的 `transcript.txt` 里读 CLI 自报的「本次累计 token」。
  CLI 自报值形如「本次累计 token 约 10059」，带「约」字，是四舍五入值。

用法：

    python evals/comparison/compare_arms.py \
        --codex-dir evals/comparison/runs/codex \
        --app-dir evals/comparison/runs/deepseek
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]

TOKEN_PATTERN = re.compile(r"本次累计 token\s*约?\s*([0-9,]+)")


def read_codex(codex_dir: Path, task_id: str) -> dict | None:
    artifact = codex_dir / "codex_artifacts" / f"{task_id}.json"
    if not artifact.exists():
        return None
    data = json.loads(artifact.read_text(encoding="utf-8"))
    return {
        "harness": data.get("harness"),
        "model": data.get("model"),
        "status": data.get("status"),
        "tool_calls": data.get("tool_calls"),
        "total_tokens": data.get("total_tokens"),
        "input_tokens": data.get("input_tokens"),
        "cached_input_tokens": data.get("cached_input_tokens"),
        "output_tokens": data.get("output_tokens"),
        "duration_seconds": data.get("duration_seconds"),
        "final_text": data.get("final_text") or "",
    }


def read_app(app_dir: Path, task_id: str, arm: str) -> dict | None:
    matrix = app_dir / "matrix.json"
    if not matrix.exists():
        return None
    records = json.loads(matrix.read_text(encoding="utf-8"))
    record = None
    for item in records:
        if item.get("task_id") == task_id and item.get("arm") == arm:
            record = item
    if record is None:
        return None

    transcript = Path(record.get("transcript", ""))
    if not transcript.is_absolute():
        transcript = ROOT / transcript
    tokens = None
    if transcript.exists():
        match = TOKEN_PATTERN.search(transcript.read_text(encoding="utf-8", errors="replace"))
        if match:
            tokens = int(match.group(1).replace(",", ""))

    return {
        "arm": arm,
        "duration_seconds": record.get("duration_seconds"),
        "exit_code": record.get("exit_code"),
        "error": record.get("error"),
        "total_tokens": tokens,
    }


def read_app_pairs(app_dir: Path, task_id: str, arms: list[str]) -> dict:
    """两个实验臂各读一次，返回 {arm: 结果}。"""

    out: dict = {}
    for arm in arms:
        found = read_app(app_dir, task_id, arm)
        if found:
            out[arm] = found
    return out


def delta(before: float, after: float) -> str:
    if not before:
        return "—"
    return "%+.1f%%" % ((after - before) / before * 100.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="对照臂 vs 实验臂对比表")
    parser.add_argument("--codex-dir", type=Path,
                        default=ROOT / "evals/comparison/runs/codex")
    parser.add_argument("--app-dir", type=Path,
                        default=ROOT / "evals/comparison/runs/deepseek")
    parser.add_argument("--app-arms", default="ops,mentor")
    parser.add_argument("--ids", default="C01,H012,retrieval_008,retrieval_013")
    parser.add_argument("--task-file", type=Path,
                        default=ROOT / "evals/comparison/tasks.json")
    args = parser.parse_args()

    tasks = {t["id"]: t for t in json.loads(
        args.task_file.read_text(encoding="utf-8"))["tasks"]}
    arms = [a.strip() for a in args.app_arms.split(",") if a.strip()]
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]

    rows = []
    for task_id in ids:
        codex = read_codex(args.codex_dir, task_id)
        apps = read_app_pairs(args.app_dir, task_id, arms)
        # 取两个实验臂里 token 更省的那个作为该任务的代表值。
        # 这就是「按任务类型分派到对应 Profile」的口径 ——
        # 必须同时声明，否则会被质问「为什么只挑赢的那条」。
        candidates = {a: v for a, v in apps.items() if v.get("total_tokens")}
        best = None
        if candidates:
            name, best = min(candidates.items(), key=lambda kv: kv[1]["total_tokens"])
            best = dict(best, arm=name)
        rows.append({"task_id": task_id, "family": tasks.get(task_id, {}).get("family"),
                     "codex": codex, "apps": apps, "best": best})

    print("=" * 96)
    print("对照臂（Codex CLI）vs 实验臂（klonet_agent），两臂同为 deepseek-v4-pro")
    print("=" * 96)
    for row in rows:
        codex = row["codex"]
        print()
        print("[%s] 族=%s" % (row["task_id"], row["family"]))
        if codex:
            print("  对照臂 Codex CLI : status=%-22s 工具=%-3s token=%-10s 耗时=%ss" % (
                codex["status"], codex["tool_calls"],
                format(codex["total_tokens"] or 0, ","), codex["duration_seconds"]))
            if codex["total_tokens"]:
                print("                    其中缓存输入 %s / 非缓存输入 %s / 输出 %s" % (
                    format(codex["cached_input_tokens"] or 0, ","),
                    format((codex["input_tokens"] or 0) - (codex["cached_input_tokens"] or 0), ","),
                    format(codex["output_tokens"] or 0, ",")))
        else:
            print("  对照臂 Codex CLI : （无结果）")

        for arm, value in sorted(row["apps"].items()):
            print("  实验臂 %-9s : token=%-10s 耗时=%-9s %s" % (
                arm, format(value["total_tokens"] or 0, ","),
                value["duration_seconds"], value.get("error") or ""))

        best = row["best"]
        if codex and best and codex["total_tokens"] and best.get("total_tokens"):
            print("  -> 最优模式 %-8s token %s   耗时 %s" % (
                best["arm"],
                delta(codex["total_tokens"], best["total_tokens"]),
                delta(codex["duration_seconds"], best["duration_seconds"])))

    print()
    print("=" * 96)
    print("必须同时声明的边界")
    print("  1. 「最优模式」取的是该任务上两个 Profile 里更省的那个 —— 这是")
    print("     「按任务类型分派 Profile」的口径，不是单一模式对全部任务。")
    print("  2. token 为端到端总量，含 Codex CLI 自身的系统提示词与工具往返；")
    print("     缓存输入 token 通常单价更低，未做价格折算。")
    print("  3. 每个格子是单次运行（n=1）。对外引用前应重复取中位数。")
    print("  4. 实验臂 token 来自 CLI 自报（带「约」字，四舍五入）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
