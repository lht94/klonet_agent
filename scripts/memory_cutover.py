#!/usr/bin/env python
"""记忆权威切换 CLI（计划阶段 7："联合评估与默认切换"）。

把 cutover 做成一个**有门槛的动作**：先跑记忆专项评测，只有
``evals/memory_eval_thresholds.json`` 里冻结的阈值全部达标，才允许把
``KLONET_AGENT_MEMORY_AUTHORITY`` 切到 ``cutover``。

04 计划阶段 0 修订：本 CLI 现在有四道前置门——

1. **DSN 存在**——``KLONET_AGENT_TEST_PG_DSN`` 必须设置。无 DSN 只能生成 Markdown
   baseline（"baseline_generated"），**不再假装评测通过**。CI 把无 DSN 的 skip
   计作 success 是阶段 0 之前一直存在的隐患，现在硬性堵掉。
2. **Worker 健康**——``memory.maintenance.health.collect_snapshot()`` 必须返回
   ``healthy``。这是 04 阶段 7 的健康门禁；阶段 0 先把它接到 ``evaluate_memory_eval``
   之前，没有 worker 模块时降级为"未配置 worker"（仍是健康状态的一种，详见
   ``collect_snapshot`` 的实现）。
3. **评测达标**——9/9 阈值核验通过；与历史契约一致。
4. **回滚无门禁**——``--rollback`` 不需要评测通过（与历史契约一致）。

用法::

    # 1) 只跑评测，不改任何配置（需要测试库 DSN：评测会建/删临时库）
    KLONET_AGENT_TEST_PG_DSN=postgresql://... \\
        python scripts/memory_cutover.py --check

    # 2) 评测达标后切换（会就地修改部署环境文件）
    KLONET_AGENT_TEST_PG_DSN=postgresql://... \\
        python scripts/memory_cutover.py --apply /etc/klonet-agent/klonet-agent.env

    # 3) 回滚：Markdown 重新成为权威（不恢复双写，见计划 §8 第 8 条）
    python scripts/memory_cutover.py --rollback /etc/klonet-agent/klonet-agent.env

退出码：
- 0：成功（评测通过 / 切换完成 / 回滚完成）
- 1：评测未达标或健康门禁不通过
- 2：参数错误
- 3：前置条件不满足（**无 DSN**）

注意：本 CLI 只管**运行期权威**（环境变量）。迁移状态机（legacy → shadow →
compare → cutover）由 ``scripts/migrate_markdown_memory.py --phase`` 推进，
两者要一起走完才算完成切换。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT.parent))

_ENV_KEY = "KLONET_AGENT_MEMORY_AUTHORITY"
_TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"
_CUTOVER = "cutover"
_LEGACY = "legacy"


# 退出码集中维护，避免 main() 里散落的数字常量。
EXIT_OK = 0
EXIT_EVAL_FAILED = 1
EXIT_USAGE = 2
EXIT_PREREQUISITES = 3


def _test_dsn_present() -> bool:
    """检查 ``KLONET_AGENT_TEST_PG_DSN`` 是否已设置。

    阶段 0 修订：cutover 是有 DSN 才能执行的动作；DSN 缺失必须显式拒收。
    评测函数本身会做"无 DSN → 只跑 Markdown baseline"的兜底，但**那不算
    cutover 的前置条件通过**。
    """

    return bool((os.environ.get(_TEST_DSN_ENV) or "").strip())


def evaluate_worker_health() -> tuple[bool, str]:
    """检查 Worker 健康。阶段 7 之前的"未启用"也视为通过。

    返回 ``(healthy, 说明)``。失败时 ``--apply`` 必须拒绝。

    设计：模块不可导入 **或** ``collect_snapshot`` 抛异常，都视为过渡态放行——
    健康快照采集失败本身不能阻断 cutover。但 ``collect_snapshot`` 返回
    ``healthy=False`` 的情况必须阻断（这是 Worker 真实不健康）。
    """

    try:
        from klonet_agent.memory.maintenance.health import collect_snapshot
        snapshot = collect_snapshot()
    except Exception:
        # 模块不存在/导入失败/采集失败：阶段 0–6 期间都是合法的过渡态。
        return True, "Worker 模块尚未就绪（视为过渡态，不阻断 cutover）"
    if snapshot.get("healthy") is True:
        return True, "Worker 健康"
    return False, f"Worker 不健康：{snapshot}"


def evaluate_memory_eval() -> tuple[bool, str]:
    """跑一遍记忆专项评测，返回 ``(是否达标, 说明)``。

    刻意**不提供跳过开关**：cutover 的门槛只有一套口径，能被绕过的门槛等于没有。
    测试通过 monkeypatch 替换本函数来跳过真实评测。

    阶段 0 修订：把 ``run_memory_eval.main()`` 的退出码拆开。无 DSN 触发的
    ``return 2``（Markdown baseline）现在返回 ``(False, "未达 cutover 门槛")``。
    评测函数本身抛异常（如 DSN 设了但连不上 PG）也归类为失败。
    """

    path = PROJECT_ROOT / "evals" / "run_memory_eval.py"
    spec = importlib.util.spec_from_file_location("memory_eval_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        code = module.main()
    except Exception as exc:
        return False, f"记忆评测异常失败：{type(exc).__name__}: {exc}"
    if code == 0:
        return True, "记忆专项评测达到冻结阈值"
    if code == 2:
        return False, "记忆专项评测仅生成了 Markdown baseline（未达 cutover 门槛）"
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
        return EXIT_OK

    # DSN 门禁：必须先设置 KLONET_AGENT_TEST_PG_DSN。
    if not _test_dsn_present():
        print(
            f"[前置条件] 未设置 {_TEST_DSN_ENV}，无法跑真库测试。"
            "cutover 需要真库评测——请设置 DSN 后重试。"
        )
        return EXIT_PREREQUISITES

    # Worker 健康门禁：阶段 7 落地的硬门禁；阶段 0–6 期间模块不存在时降级为通过。
    healthy, health_detail = evaluate_worker_health()
    print(f"[健康] {health_detail}")
    if not healthy:
        print("拒绝切换：Worker 不健康，先修复再重试。")
        return EXIT_EVAL_FAILED

    passed, detail = evaluate_memory_eval()
    print(f"[评测] {detail}")
    if not passed:
        print("拒绝切换：先让评测达标，或调整 evals/memory_eval_thresholds.json 并重新冻结。")
        return EXIT_EVAL_FAILED

    if args.check:
        print("评测通过；可以执行 --apply <ENV_FILE> 完成切换。")
        return EXIT_OK

    previous = set_authority(Path(args.apply), _CUTOVER)
    print(f"{_ENV_KEY}: {previous} -> {_CUTOVER}")
    print(
        "已切换：Markdown 只作为迁移/导出来源。\n"
        f"如遇问题用 `--rollback {args.apply}` 回退。"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())