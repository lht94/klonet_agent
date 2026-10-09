"""记忆生命周期维护 Job 协议（04 计划 §5.1 / 阶段 1）。

**目的**：让 scheduler / repository 不关心具体 Job 的实现细节，只看四个契约：
``name`` + ``run(context, cursor)`` + 共享的 ``JobContext`` + ``JobResult``。

设计取舍：

1. **纯协议，不依赖 PG / psycopg**——本模块是测试与单元层最稳定的契约层；
   任何 Job 都可以 import 它而不触发数据库驱动加载。
2. **``cursor`` 透传**——repository 不解析 cursor 形态；它只负责"成功后推进"。
   cursor 是 Job 自己的私有数据（一般是 ``(column, id)`` 复合），schema 层不校验。
3. **``details: tuple[str, ...]`` 而不是 dict**——``details`` 用于人类阅读的
   "这次跑了什么"摘要，避免 schema 层与 Job 实现层各自约定 JSON 形态；
   真要结构化信息就放 ``JobResult.changed / proposed / failed`` 计数。
4. **``error_code`` 不暴露给健康快照以外的渠道**——Job 实现层应当 catch
   异常并转成 reason_code（稳定短串），不要把 traceback 字符串塞进
   ``details``。正文/token/连接密码永不出现在 JobResult 任何字段。
5. **``deadline`` 必传**——Job 必须在耗时超过 deadline 之前主动放弃；这不是
   "建议"，是"超期即视为该 Job 不健康"。但 Job 实现层可以选择 **不**自己
   监听（worker 主循环负责整体超时控制）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "JobContext",
    "JobResult",
    "MaintenanceJob",
]


@dataclass(frozen=True)
class JobContext:
    """一次 Job 执行的环境参数。

    ``run_id`` 与 ``job_name`` 一一对应一组（``runs`` 表）账本。
    ``worker_id`` 用来诊断"哪个 worker 拿了这批"——多实例部署下尤其重要。
    ``started_at`` 与 ``deadline`` 都是调用方在创建 ``JobContext`` 时
    按 wall clock 设定；Job 实现层在用 ``started_at`` 计算耗时前应
    优先用 ``JobResult.details`` 里的真实起止（避免 worker 本机漂移）。
    """

    job_name: str
    run_id: str
    worker_id: str
    started_at: datetime
    deadline: datetime
    batch_limit: int
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not self.job_name:
            raise ValueError("job_name 不能为空")
        if not self.run_id:
            raise ValueError("run_id 不能为空")
        if not self.worker_id:
            raise ValueError("worker_id 不能为空")
        if self.batch_limit <= 0:
            raise ValueError(f"batch_limit 必须为正整数，实际 {self.batch_limit}")


@dataclass(frozen=True)
class JobResult:
    """一次 Job 执行的返回结果。

    字段语义：

    * ``scanned``  本批扫过的记录数（含没改的）
    * ``changed``  本批实际写入 / 改动的记录数
    * ``proposed`` 本批产出的"提案"数（如 ConsolidationJob 的 proposal）
    * ``failed``   本批失败的记录数（不是 Job 整体失败；后者由 scheduler 改
      ``runs.status='failed'``，不是这个数字）
    * ``next_cursor`` 本批成功完成后可推进的 cursor；**Job 实现层只在
      真正完成一个 batch 后再设这个值**——``runs.cursor`` 在 Job 整体
      失败时**不**回退到旧值（详见 repository.complete / fail 注释）。
    * ``details``  人类可读的简短摘要（最长几十字符），用于 audit / log。
    """

    scanned: int = 0
    changed: int = 0
    proposed: int = 0
    failed: int = 0
    next_cursor: str | None = None
    details: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name, value in (
            ("scanned", self.scanned),
            ("changed", self.changed),
            ("proposed", self.proposed),
            ("failed", self.failed),
        ):
            if value < 0:
                raise ValueError(f"{name} 不能为负数，实际 {value}")


@runtime_checkable
class MaintenanceJob(Protocol):
    """所有 Job 必须实现这两个契约。

    ``name`` 与 ``memory_maintenance_jobs.job_name`` 一致；register 时
    不允许改写大小写或加空白。

    ``run`` 接受 ``(context, cursor)`` 返回 ``JobResult``。**不抛异常**——
    Job 自身应 catch 一切异常并转换成 ``JobResult.failed`` 计数 + ``details``
    摘要；只有"无法继续执行、必须让 worker 退出"这种致命错才允许冒泡。
    """

    name: str

    def run(self, context: JobContext, cursor: str | None) -> JobResult: ...
