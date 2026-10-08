#!/usr/bin/env python
"""Markdown 记忆迁移 CLI（计划 §8、阶段 6）。

用法::

    # 只读预览：不连库、不改状态，任何环境都能跑
    python scripts/migrate_markdown_memory.py --preview

    # 实际写入（需要记忆库 DSN）
    KLONET_AGENT_MEMORY_DSN=postgresql://... \\
        python scripts/migrate_markdown_memory.py --apply

    # 推进迁移阶段（四态状态机）
    python scripts/migrate_markdown_memory.py --phase shadow
    python scripts/migrate_markdown_memory.py --phase cutover --snapshot exports/2026-10-08.json

    # 把报告落盘，便于人工核对数量与抽样
    python scripts/migrate_markdown_memory.py --preview --report /tmp/migration_preview.md

退出码：0 正常；1 有失败条目；2 参数/前置条件不满足。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT.parent))

from klonet_agent.memory.database import MemoryDatabase, memory_dsn  # noqa: E402
from klonet_agent.memory.migration import (  # noqa: E402
    MemoryMigrator,
    MigrationStateStore,
    TransitionError,
    render_report,
)


def _pipeline_factory(database: MemoryDatabase):
    """按租户构造写入管线。

    迁移**不走**候选提取器：条目是从 Markdown 文本解析出来的（``parse_document``），
    不需要模型再解读一遍。所以这里只借用管线的"策略 + consolidation + 幂等"三段。
    """

    from klonet_agent.memory.postgres import PostgresMemoryRepository
    from klonet_agent.memory.write_pipeline import (
        MemoryWritePipeline,
        NullWriteTracer,
    )
    from klonet_agent.memory.write_policy import MemoryWritePolicy

    def build(tenant):
        return MemoryWritePipeline(
            PostgresMemoryRepository(database, tenant),
            policy=MemoryWritePolicy(),
            tracer=NullWriteTracer(),
        )

    return build


def _refuse_pipeline(_tenant):
    raise RuntimeError("预览模式不应该构造写入管线")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 Markdown 记忆迁移到 PostgreSQL")
    parser.add_argument(
        "--root",
        default=str(PROJECT_ROOT / "memory"),
        help="记忆目录（默认仓库内的 memory/）",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="迁移状态文件（默认 <root>/.migration_state.json）",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="只读扫描并生成报告（默认行为，不需要 DSN）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入数据库（需要记忆库 DSN）",
    )
    parser.add_argument(
        "--phase",
        choices=["legacy", "shadow", "compare", "cutover"],
        help="推进迁移阶段（四态状态机，非法跳转会拒绝）",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="进入 cutover 所需的只读快照路径（回滚依据）",
    )
    parser.add_argument("--report", default=None, help="把报告写到该文件")
    parser.add_argument("--dsn", default=None, help="记忆库 DSN（默认读环境变量）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    state_path = (
        Path(args.state).expanduser().resolve()
        if args.state
        else root / ".migration_state.json"
    )
    store = MigrationStateStore(state_path)

    if args.phase:
        try:
            state = store.transition(args.phase, snapshot_export=args.snapshot)
        except TransitionError as exc:
            print(f"状态迁移被拒绝：{exc}", file=sys.stderr)
            return 2
        print(f"迁移阶段已切到：{state.phase}（状态文件 {state_path}）")
        return 0

    database: MemoryDatabase | None = None
    factory = _refuse_pipeline
    if args.apply:
        dsn = (args.dsn or memory_dsn() or "").strip()
        if not dsn:
            print(
                "--apply 需要记忆库 DSN：设置 KLONET_AGENT_MEMORY_DSN 或传 --dsn",
                file=sys.stderr,
            )
            return 2
        database = MemoryDatabase(dsn)
        database.open()
        factory = _pipeline_factory(database)

    try:
        migrator = MemoryMigrator(factory, root=root, state_store=store)
        report = migrator.run(apply=args.apply)
        text = render_report(report, phase=store.load().phase)
        print(text)
        if args.report:
            Path(args.report).expanduser().write_text(text, encoding="utf-8")
            print(f"报告已写入 {args.report}")
    finally:
        if database is not None:
            database.close()

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
