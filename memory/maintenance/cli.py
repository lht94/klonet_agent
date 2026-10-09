"""维护 Worker 的 CLI（04 计划 §8.1 / 阶段 2）。

命令：

```text
python -m klonet_agent.memory.maintenance run
python -m klonet_agent.memory.maintenance once --job embedding
python -m klonet_agent.memory.maintenance once --job expiration --dry-run
python -m klonet_agent.memory.maintenance status --json
```

（``proposals list|approve|reject`` 属于阶段 5，本阶段只占位返回明确错误。）

退出码（稳定，写进运维 runbook）：

* ``0`` —— 正常（``run`` 收到 SIGTERM 优雅退出、``once`` 跑完、``status`` 读成功）
* ``1`` —— 运行故障（claim/complete 之外不可恢复的错误、作业抛未捕获异常）
* ``2`` —— 参数错误（argparse 自身）
* ``3`` —— 配置错误（DSN 缺失、``MaintenanceConfig`` 非法、``run`` 时未启用）

**刻意不在 CLI 里兜底**：DSN 缺失、配置非法都必须以非零退出码暴露，而不是
"降级到某种能跑的默认值"——运维需要能区分"服务在跑但空闲"和"服务根本没起来"。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from klonet_agent.config import MaintenanceConfig, load_maintenance_config
from klonet_agent.memory.maintenance import service as service_module
from klonet_agent.memory.maintenance.service import (
    MaintenanceService,
    UnknownJobError,
    resolve_job_name,
)

__all__ = ["EXIT_OK", "EXIT_RUNTIME", "EXIT_USAGE", "EXIT_CONFIG", "main"]

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_database() -> Any:
    """构造并打开 ``MemoryDatabase``。DSN 缺失时抛 ``MemoryDatabaseError``。"""

    from klonet_agent.memory.database import MemoryDatabase

    database = MemoryDatabase.from_env(min_size=1, max_size=4)
    database.open()
    return database


def _build_jobs(database: Any, config: MaintenanceConfig) -> list[Any]:
    """加载已经实现好的 Job。

    阶段 2 时 ``memory/maintenance/jobs/`` 还没有任何 Job 实现，返回空列表是
    预期行为（Worker 起来后只空转）。阶段 3 起每加一个 Job 模块自动被发现——
    **不写死名字**，避免"实现了但忘记注册"的静默缺失。

    模块约定（两种任选其一）：

    * ``memory.maintenance.jobs.<canonical_name>`` 暴露 ``JOB`` 对象
      （无依赖的 Job）；
    * 或者暴露 ``build_job(*, database, config)`` 工厂，返回 Job 或 ``None``
      （需要数据库/凭据的 Job；返回 None 表示"这个部署不具备运行条件"，
      例如没有嵌入凭据）。

    ``build_job`` 优先。工厂返回 ``None`` 时**不注册**，这样 ``status`` 的
    ``jobs`` 列表就能如实反映"哪些 Job 真的可用"。
    """

    jobs: list[Any] = []
    module_map = _job_module_map()
    for name in service_module.KNOWN_JOB_NAMES:
        basename = module_map.get(name, name)
        module_name = f"klonet_agent.memory.maintenance.jobs.{basename}"
        try:
            module = __import__(module_name, fromlist=["JOB", "build_job"])
        except ImportError:
            continue
        builder = getattr(module, "build_job", None)
        if callable(builder):
            job = builder(database=database, config=config)
            if job is not None:
                jobs.append(job)
            continue
        job = getattr(module, "JOB", None)
        if job is not None:
            jobs.append(job)
    return jobs


def _job_module_map() -> dict[str, str]:
    """规范 Job 名 → 模块文件 basename 的映射（见 ``jobs/__init__.py``）。

    规范名是写进数据库的外部契约，模块名是内部实现细节，两者**不必**相同
    （``embedding_outbox`` → ``jobs/embedding.py``）。这里显式取映射，
    取不到才退回"模块名 == 规范名"。
    """

    try:
        from klonet_agent.memory.maintenance.jobs import JOB_MODULES

        return dict(JOB_MODULES)
    except ImportError:  # pragma: no cover - 包结构被破坏时
        return {}


def _build_service(config: MaintenanceConfig) -> tuple[MaintenanceService, Any]:
    database = _build_database()
    from klonet_agent.memory.maintenance.repository import MaintenanceRepository

    repository = MaintenanceRepository(database)
    service = MaintenanceService(repository, config, jobs=_build_jobs(database, config))
    return service, database


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def _cmd_run(args: argparse.Namespace, config: MaintenanceConfig) -> int:
    if not config.enabled and not args.force:
        print(
            "维护 Worker 未启用（KLONET_AGENT_MAINTENANCE_ENABLED=0）。"
            "确认要跑请加 --force，或先设置该环境变量为 1。",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    service, database = _build_service(config)
    try:
        service.install_signal_handlers()
        service.run_forever()
    except KeyboardInterrupt:  # pragma: no cover - 信号已接管
        service.request_stop()
    finally:
        database.close()
    return EXIT_OK


def _cmd_once(args: argparse.Namespace, config: MaintenanceConfig) -> int:
    if args.dry_run:
        # 临时覆盖 dry_run（frozen dataclass；用 dataclasses.replace）。
        import dataclasses

        config = dataclasses.replace(config, dry_run=True)

    job_name = None
    if args.job:
        try:
            job_name = resolve_job_name(args.job)
        except UnknownJobError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE

    service, database = _build_service(config)
    try:
        try:
            results = service.run_once(
                job_name=job_name, force=args.force, max_runs=args.max_runs
            )
        except UnknownJobError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
    finally:
        database.close()

    if not results:
        print("无到期的 Job（或已全部处理完）。")
        return EXIT_OK
    total = {
        "runs": len(results),
        "scanned": sum(r.scanned for r in results),
        "changed": sum(r.changed for r in results),
        "proposed": sum(r.proposed for r in results),
        "failed": sum(r.failed for r in results),
    }
    print(f"完成 {total['runs']} 个 Job：{json.dumps(total, ensure_ascii=False)}")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, config: MaintenanceConfig) -> int:
    service, database = _build_service(config)
    payload: dict[str, Any] = {}
    try:
        payload.update(service.status().as_dict())
        payload["db_open"] = bool(getattr(database, "is_open", False))
        try:
            from klonet_agent.memory.maintenance.health import collect_snapshot

            payload["health"] = dict(collect_snapshot())
        except Exception as exc:  # noqa: BLE001 - 健康快照不应让 status 失败
            payload["health_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        database.close()

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"worker_id: {payload.get('worker_id')}")
        print(f"enabled:   {payload.get('enabled')}")
        print(f"jobs:      {', '.join(payload.get('registered_jobs') or []) or '(none)'}")
        print(f"db_open:   {payload.get('db_open')}")
    return EXIT_OK


def _cmd_proposals(args: argparse.Namespace) -> int:
    print(
        "proposals 子命令属于 04 计划阶段 5（整理提案系统），尚未实现。",
        file=sys.stderr,
    )
    return EXIT_USAGE


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m klonet_agent.memory.maintenance",
        description="记忆生命周期维护 Worker（04 计划）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="常驻服务")
    run.add_argument(
        "--force",
        action="store_true",
        help="即使 KLONET_AGENT_MAINTENANCE_ENABLED=0 也启动",
    )

    once = sub.add_parser("once", help="跑一次（运维/测试）")
    once.add_argument("--job", help="只跑指定 Job（支持别名，如 embedding）")
    once.add_argument("--dry-run", action="store_true", help="不写库，只演练")
    once.add_argument(
        "--force",
        action="store_true",
        help="忽略 enabled 与调度时间，立即跑（仍尊重 lease）",
    )
    once.add_argument(
        "--max-runs", type=int, default=1, help="不带 --job 时最多跑几个 due Job"
    )

    status = sub.add_parser("status", help="打印 Worker 状态")
    status.add_argument("--json", action="store_true", help="JSON 输出")

    proposals = sub.add_parser("proposals", help="整理提案（阶段 5，占位）")
    proposals.add_argument(
        "action", nargs="?", choices=["list", "approve", "reject"], default="list"
    )
    proposals.add_argument("proposal_id", nargs="?")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_USAGE

    _configure_logging(getattr(args, "verbose", False))

    if args.command == "proposals":
        return _cmd_proposals(args)

    try:
        config = load_maintenance_config()
    except ValueError as exc:
        # 刻意 catch ``ValueError`` 而非 ``config.MaintenanceConfigError``：
        # ``MaintenanceConfigError`` 是 ``ValueError`` 子类，但**类对象身份**会
        # 被 ``importlib.reload(config)``（测试里验证 MEMORY_AUTHORITY 时会发生）
        # 换掉——本模块在导入时绑定的是旧类，reload 之后抛出的新类实例就不再是
        # 旧类的实例，`except` 匹配不到。捕获稳定的内建基类即可跨 reload 生效。
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG

    from klonet_agent.memory.database import MemoryDatabaseError

    handlers = {
        "run": _cmd_run,
        "once": _cmd_once,
        "status": _cmd_status,
    }
    try:
        return handlers[args.command](args, config)
    except MemoryDatabaseError as exc:
        print(f"数据库不可用：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except UnknownJobError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - 顶层兜底换成稳定退出码
        print(f"运行故障：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
