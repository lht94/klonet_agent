"""对比测试矩阵运行器：按 tasks.json 让各实验臂跑同一批任务并落盘原始输出。

使用前提：在**测试服务器**上运行（需要模型凭据与真实环境）。
两个实验臂都必须经 `scripts/eval_env.sh` 取模型配置，本运行器通过
`evals/comparison/run_baseline.sh` 与 `scripts/run_eval_session.sh` 调用它们，
因此模型一致性由那两个启动器保证。

用法：

    # 先看会跑什么（不执行）
    python evals/comparison/run_matrix.py --arms baseline-record --dry-run

    # 对照臂只记录不执行（安全任务的取数方式）
    python evals/comparison/run_matrix.py --arms baseline-record \
        --out-dir evals/comparison/runs/matrix

    # 实验臂（ops 在审批边界停下，不会真的执行变更）
    python evals/comparison/run_matrix.py --arms ops --ids H031,H018,H042,C01

安全闸：`safe_for_real_execution=false` 的任务禁止在任何会真实执行的臂上运行，
除非显式传 `--allow-unsafe`。**不要随手传这个开关。**
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]

# 会真实执行系统操作的臂
REAL_EXECUTION_ARMS = {"baseline", "mentor", "ops"}


def load_tasks(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["tasks"]


def build_command(arm: str, task: dict, out_dir: Path) -> tuple[list[str], str | None]:
    """返回 (argv, stdin 内容)。stdin 为 None 表示不喂输入。"""

    prompt = task["prompt"]

    if arm in {"baseline", "baseline-record"}:
        argv = [
            sys.executable, "-X", "utf8", str(ROOT / "evals/comparison/baseline_agent.py"),
            "--task-id", task["id"],
            "--prompt", prompt,
            "--out-dir", str(out_dir / "baseline_artifacts"),
            # 推理模型（deepseek-v4-pro）会把推理 token 计入 completion，
            # 额度不足时会出现「无工具调用 + 空文本」的假失败，必须给足。
            "--max-tokens", "8192",
            "--quiet",
        ]
        if arm == "baseline-record":
            argv.append("--record-only")
        return argv, None

    if arm in {"mentor", "ops"}:
        argv = [
            "bash", str(ROOT / "scripts/run_eval_session.sh"),
            arm,
            # 每个任务用独立的 user_id：实测发现共用 user 时，
            # 上一个任务遗留的 FailureRecord 会被「待人工决策状态门」接住，
            # 导致下一个任务的输入根本不进意图分类
            # （H012×ops 只跑 2 秒就返回「当前会话没有可处理的失败记录」）。
            # 每个任务必须从干净状态开始，否则测的不是任务本身。
            f"eval-{task['id'].lower().replace('_', '-')}",
            f"cmp-{task['id'].lower()}",
        ]
        # CLI 把非交互 stdin 的全部内容作为一个用户回合
        return argv, prompt + "\n"

    raise ValueError(f"未知实验臂: {arm}")


def run_one(arm: str, task: dict, out_dir: Path, timeout: int) -> dict:
    argv, stdin_text = build_command(arm, task, out_dir)
    run_dir = out_dir / "runs" / task["id"] / arm
    run_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    record: dict = {
        "task_id": task["id"],
        "family": task["family"],
        "arm": arm,
        "prompt": task["prompt"],
        "expected_behavior": task.get("expected_behavior"),
        "argv": argv,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    try:
        completed = subprocess.run(
            argv, cwd=str(ROOT), input=stdin_text,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        record["exit_code"] = completed.returncode
        record["error"] = ""
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        record["exit_code"] = None
        record["error"] = "timeout"
    except Exception as exc:  # noqa: BLE001
        output = ""
        record["exit_code"] = None
        record["error"] = f"{type(exc).__name__}: {exc}"

    record["duration_seconds"] = round(time.monotonic() - started, 3)

    transcript = run_dir / "transcript.txt"
    transcript.write_text(output, encoding="utf-8")
    try:
        record["transcript"] = str(transcript.relative_to(ROOT))
    except ValueError:
        # out_dir 在仓库之外时 relative_to 会失败，记录绝对路径即可
        record["transcript"] = str(transcript)

    # 对照臂另外读一份结构化结果，便于取 llm_calls / tool_calls / tokens
    if arm in {"baseline", "baseline-record"}:
        artifact = out_dir / "baseline_artifacts" / f"{task['id']}.json"
        if artifact.exists():
            try:
                data = json.loads(artifact.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            record["baseline"] = {
                key: data.get(key)
                for key in (
                    "status", "model", "base_url", "config_source", "locked",
                    "llm_calls", "tool_calls", "total_tokens", "record_only",
                )
            }
            record["baseline_tool_audit"] = data.get("tool_audit", [])
            record["baseline_final_text"] = data.get("final_text", "")
        else:
            record["baseline"] = None

    return record


def render_summary(records: list[dict], destination: Path) -> None:
    lines = [
        "# 对比测试矩阵 — 运行汇总",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 运行数：{len(records)}",
        "",
        "| 任务 | 族 | 臂 | 状态 | 耗时(s) | LLM 调用 | 工具调用 | token | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        base = record.get("baseline") or {}
        status = record.get("error") or (base.get("status") if base else
                                         ("ok" if record.get("exit_code") == 0 else "异常"))
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                record["task_id"], record["family"], record["arm"], status,
                record["duration_seconds"],
                base.get("llm_calls", "—"), base.get("tool_calls", "—"),
                base.get("total_tokens", "—"),
                ("锁定=" + str(base.get("locked"))) if base else "—",
            )
        )
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="对比测试矩阵运行器")
    parser.add_argument("--tasks", type=Path,
                        default=ROOT / "evals/comparison/tasks.json")
    parser.add_argument("--arms", required=True,
                        help="逗号分隔：baseline-record / baseline / mentor / ops")
    parser.add_argument("--ids", default="", help="逗号分隔的任务 id，默认全部")
    parser.add_argument("--out-dir", type=Path,
                        default=ROOT / "evals/comparison/runs/matrix")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--allow-unsafe", action="store_true",
                        help="允许在真实执行臂上运行标记为危险的任务（慎用）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tasks = load_tasks(args.tasks)
    # 统一成绝对路径：下面 record["transcript"] 要用 relative_to(ROOT)，
    # 绝对与相对混用会抛 ValueError。
    args.out_dir = args.out_dir.resolve()
    selected_ids = {item for item in args.ids.split(",") if item}
    if selected_ids:
        tasks = [task for task in tasks if task["id"] in selected_ids]
        missing = selected_ids - {task["id"] for task in tasks}
        if missing:
            print(f"未知任务 id: {sorted(missing)}", file=sys.stderr)
            return 2

    arms = [item.strip() for item in args.arms.split(",") if item.strip()]

    plan: list[tuple[str, dict]] = []
    skipped: list[str] = []
    for arm in arms:
        for task in tasks:
            if arm not in task["arms"]:
                skipped.append(f"{task['id']}×{arm}（该任务不适用此臂）")
                continue
            if arm in REAL_EXECUTION_ARMS:
                risk = task.get("real_execution_risk", "none")
                # 对照臂没有工具层护栏，baseline_only 级的任务对它等同于危险；
                # ops/mentor 有审批门，可以安全跑到审批边界。
                if arm == "baseline" and risk in {"baseline_only", "critical"}:
                    skipped.append(
                        f"{task['id']}×{arm}（对照臂真实执行会产生副作用；"
                        f"请改用 baseline-record）"
                    )
                    continue
                if risk == "critical" and not args.allow_unsafe:
                    skipped.append(
                        f"{task['id']}×{arm}（不可逆任务，需 --allow-unsafe）"
                    )
                    continue
            plan.append((arm, task))

    if not plan:
        print("没有可运行的任务/臂组合。", file=sys.stderr)
        for item in skipped:
            print(f"  跳过 {item}", file=sys.stderr)
        return 2

    print(f"计划运行 {len(plan)} 次：")
    for arm, task in plan:
        print(f"  {task['id']:>16} [{task['family']}] × {arm}")
    if skipped:
        print(f"\n跳过 {len(skipped)} 项：")
        for item in skipped:
            print(f"  {item}")

    if args.dry_run:
        print("\n--dry-run，未执行。")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    matrix_file = args.out_dir / "matrix.json"

    # 与已有记录合并，而不是覆盖：不同批次（如先跑 baseline-record，再跑真实执行臂）
    # 是同一实验的组成部分，覆盖会让先跑那批的结构化数据消失。
    # 同 (task_id, arm) 以本次为准。
    previous: dict[tuple[str, str], dict] = {}
    if matrix_file.exists():
        try:
            for old in json.loads(matrix_file.read_text(encoding="utf-8")):
                previous[(old.get("task_id"), old.get("arm"))] = old
        except json.JSONDecodeError:
            print("警告：已有 matrix.json 无法解析，将重新开始。", file=sys.stderr)

    records: list[dict] = []
    for index, (arm, task) in enumerate(plan, start=1):
        print(f"\n[{index}/{len(plan)}] {task['id']} × {arm} ...", flush=True)
        record = run_one(arm, task, args.out_dir, args.timeout)
        records.append(record)
        print(
            "    -> exit=%s 耗时=%ss %s" % (
                record.get("exit_code"), record["duration_seconds"],
                record.get("error") or "",
            ),
            flush=True,
        )
        # 每跑完一条就落盘（合并旧记录），长批次中断也不丢证据
        merged = dict(previous)
        for item in records:
            merged[(item["task_id"], item["arm"])] = item
        matrix_file.write_text(
            json.dumps(list(merged.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 本次批次的原始记录另存一份，便于追溯是哪一批跑出来的
    batch_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (args.out_dir / f"batch-{batch_stamp}.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    render_summary(list(previous.values()) + records, args.out_dir / "SUMMARY.md")
    print(f"\n完成。汇总：{args.out_dir / 'SUMMARY.md'}")
    print(f"原始记录（含历史批次）：{matrix_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
