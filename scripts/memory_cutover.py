#!/usr/bin/env python
"""记忆权威切换 CLI（计划阶段 7："联合评估与默认切换"）。

把 cutover 做成一个**有门槛的动作**：先跑记忆专项评测，只有
``evals/memory_eval_thresholds.json`` 里冻结的阈值全部达标，才允许把
``KLONET_AGENT_MEMORY_AUTHORITY`` 切到 ``cutover``。

用法::

    # 1) 只跑评测，不改任何配置（需要测试库 DSN：评测会建/删临时库）
    KLONET_AGENT_TEST_PG_DSN=postgresql://... \
        python scripts/memory_cutover.py --check

    # 2) 评测达标后切换（会就地修改部署环境文件）
    KLONET_AGENT_TEST_PG_DSN=postgresql://... \
        python scripts/memory_cutover.py --apply /etc/klonet-agent/klonet-agent.env

    # 3) 回滚：Markdown 重新成为权威（不恢复双写，见计划 §8 第 8 条）
    python scripts/memory_cutover.py --rollback /etc/klonet-agent/klonet-agent.env

退出码：0 成功；1 评测未达标或前置条件不满足；2 参数错误。

注意：本 CLI 只管**运行期权威**（环境变量）。迁移状态机（legacy → shadow →
compare → cutover）由 ``scripts/migrate_markdown_memory.py --phase`` 推进，
两者要一起走完才算完成切换。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT.parent))

_ENV_KEY = "KLONET_AGENT_MEMORY_AUTHORITY"
_CUTOVER = "cutover"
_LEGACY = "legacy"


def evaluate_memory_eval() -> tuple[bool, str]:
    """跑一遍记忆专项评测，返回 ``(是否达标, 说明)``。

    刻意**不提供跳过开关**：cutover 的门槛只有一套口径，能被绕过的门槛等于没有。
    测试通过 monkeypatch 替换本函数来跳过真实评测。
    """

    path = PROJECT_ROOT / "evals" / "run_memory_eval.py"
    spec = importlib.util.spec_from_file_location("memory_eval_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    code = module.main()
    if code == 0:
        return True, "记忆专项评测达到冻结阈值"
    return False, "记忆专项评测未达标（详见 evals/memory_summary.md）"


def set_authority(env_file: Path, value: str) -> str:
    """就地更新环境文件里的 ``KLONET_AGENT_MEMORY_AUTHORITY``，返回旧值。"""

    env_file = Path(env_file)
    lines: list[str] = []
    previous: str | None = None
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.split("=", 1)[0].strip() == _ENV_KEY:
            previous = stripped.split("=", 1)[1].strip() if "=" in stripped else ""
            lines[index] = f"{_ENV_KEY}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{_ENV_KEY}={value}")
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return previous if previous is not None else "(未设置)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_cutover",
        description="记忆权威切换（阶段 7）：评测达标后才允许切到数据库权威",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="只跑评测，不改配置")
    mode.add_argument("--apply", metavar="ENV_FILE", help="评测达标后切到 cutover")
    mode.add_argument("--rollback", metavar="ENV_FILE", help="回滚成 legacy")
    args = parser.parse_args(argv)

    if args.rollback:
        previous = set_authority(Path(args.rollback), _LEGACY)
        print(f"{_ENV_KEY}: {previous} -> {_LEGACY}（Markdown 重新成为权威）")
        print("注意：cutover 的回滚是回到只读快照，不恢复双写。")
        return 0

    passed, detail = evaluate_memory_eval()
    print(f"[评测] {detail}")
    if not passed:
        print("拒绝切换：先让评测达标，或调整 evals/memory_eval_thresholds.json 并重新冻结。")
        return 1

    if args.check:
        print("评测通过；可以执行 --apply <ENV_FILE> 完成切换。")
        return 0

    previous = set_authority(Path(args.apply), _CUTOVER)
    print(f"{_ENV_KEY}: {previous} -> {_CUTOVER}")
    print(
        "已切换：Markdown 只作为迁移/导出来源。\n"
        f"如遇问题用 `--rollback {args.apply}` 回退。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
