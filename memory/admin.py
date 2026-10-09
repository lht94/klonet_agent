"""记忆的管理接口：查看、删除、归档与物理清理（计划阶段 7）。

"删除权"要落实，三件事必须一起做到，缺一条就会出现**删了但还能召回**：

1. **逻辑删除**——记录状态转 ``deleted``、当前版本结束有效期。这两件在仓库的
   :meth:`delete_memory` 里与第 3 件同事务完成，这里不重复实现。
2. **向量同步清除**——只删正文而留着向量，语义通道仍然能召回它。
3. **保留期后物理清理**——真正删除行与版本，且**只能**发生在明确删除过的记录上。

所以本模块刻意做得很薄：它负责调度、门槛与审计，删除的原子性留给仓库。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from klonet_agent.memory.domain import (
    MemoryDomainError,
    MemoryQuery,
    MemoryRecord,
    MemoryType,
    Scope,
)
from klonet_agent.memory.repository import DeletedBacklog

__all__ = [
    "ExpiredArchiveReport",
    "ForgetOutcome",
    "MemoryAdmin",
    "PurgeReport",
    "RetentionPolicy",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RetentionPolicy:
    """保留策略。

    默认值刻意保守：**物理清理只碰已被明确删除的记录**，而且必须等过保留期；
    归档不等于删除。任何"自动删除用户没删过的东西"的设计都不在这里。
    """

    # 逻辑删除到物理清理之间的保留期。
    soft_delete_retention_days: int = 30
    # 单次清理的上限，避免一个定时任务把整张表锁住。
    purge_batch_limit: int = 1000
    # 部署合规下限（天）。**这是本文件唯一一处"钳制"**，而且方向是保守的：
    # 配置把保留期写短了（例如 1 天）时，实际按这个下限执行。
    # 与 ``MaintenanceConfig`` 的"非法即拒绝启动"不矛盾——那里防的是"启动一个
    # 参数错的服务"，这里防的是"一次错误的配置把还没到期的数据删掉"，后者
    # 不可逆，宁可静默保守。
    compliance_floor_days: int = 0

    def purge_cutoff(self, *, now: datetime | None = None) -> datetime:
        moment = now or _now()
        days = max(
            max(0, int(self.compliance_floor_days)),
            max(0, int(self.soft_delete_retention_days)),
        )
        return moment - timedelta(days=days)


@dataclass(frozen=True)
class ExpiredArchiveReport:
    """一次过期归档的明细。

    ``cursor`` 是**下一批的起点**；调用方（ExpirationJob）把它写进
    ``memory_maintenance_jobs.cursor``，下一个 tick 从断点继续。
    """

    archived: int = 0
    outbox_deferred: int = 0
    batches: int = 0
    cursor: str | None = None
    exhausted: bool = False


@dataclass(frozen=True)
class PurgeReport:
    """一次物理清理的明细。"""

    dry_run: bool = False
    removed: int = 0
    batches: int = 0
    backlog: "DeletedBacklog | None" = None
    recall_leaks: tuple[str, ...] = ()
    stopped_early: bool = False


@dataclass(frozen=True)
class ForgetOutcome:
    """一次删除请求的结果（供审计）。"""

    memory_id: str
    deleted: bool
    reason: str | None = None


class MemoryAdmin:
    """面向"用户要查看/删掉一条画像"的管理入口。"""

    def __init__(self, repository: Any, *, tracer: Any | None = None):
        self._repository = repository
        self._tracer = tracer

    # ------------------------------------------------------------- 查看 --

    def list_memories(
        self,
        *,
        limit: int = 100,
        memory_type: MemoryType | None = None,
        scope: Scope | None = None,
        include_deleted: bool = True,
    ) -> list[MemoryRecord]:
        """列出当前租户的记忆。

        ``include_deleted=True`` 是有意的默认：删除是**可审计**的动作，
        管理界面必须能看到"这条被删过"，而不是让它凭空消失。
        """

        records = self._repository.list_records(
            limit=limit, memory_type=memory_type, scope=scope
        )
        if include_deleted:
            return records
        return [record for record in records if record.status.value != "deleted"]

    def explain(self, memory_id: str) -> dict[str, Any]:
        """给出一条记忆的完整视图：记录、版本时间线、来源、关系。

        删除权要能被验证，就得让人看见"这条记忆从哪来、现在是什么状态"。
        """

        record = self._repository.get_record(memory_id)
        if record is None:
            return {"memory_id": memory_id, "found": False}
        versions = self._repository.list_versions(memory_id)
        sources: list[str] = []
        for version in versions:
            sources.extend(
                f"{item.source_type.value}:{item.source_id}"
                for item in self._repository.list_sources(version.id)
            )
        relations = [
            f"{item.relation_type.value}:{item.to_memory_id}"
            for item in self._repository.list_relations(memory_id)
        ]
        return {
            "memory_id": memory_id,
            "found": True,
            "status": record.status.value,
            "memory_type": record.memory_type.value,
            "scope": record.scope.value,
            "subject_key": record.subject_key,
            "versions": len(versions),
            "has_embedding": any(item.embedding for item in versions),
            "valid_to": (
                versions[-1].valid_to.isoformat() if versions and versions[-1].valid_to else None
            ),
            "sources": sorted(set(sources)),
            "relations": relations,
        }

    # ------------------------------------------------------------- 删除 --

    def forget(self, memory_id: str, *, reason: str | None = None) -> ForgetOutcome:
        """删除一条记忆（逻辑删除 + 向量同步清除），并留下审计。"""

        try:
            self._repository.delete_memory(memory_id, reason=reason)
        except Exception as exc:  # noqa: BLE001 - 审计失败不改变"已尝试删除"这个事实
            self._trace(
                "memory_forget_failed",
                {"memory_id": memory_id, "error": type(exc).__name__},
            )
            raise
        self._trace("memory_forgotten", {"memory_id": memory_id, "reason": reason})
        return ForgetOutcome(memory_id=memory_id, deleted=True, reason=reason)

    def purge(
        self,
        *,
        policy: RetentionPolicy | None = None,
        now: datetime | None = None,
        dry_run: bool = False,
        batch_size: int | None = None,
        max_batches: int | None = None,
        recall_sample: int = 3,
    ) -> int:
        """物理清理已过保留期的逻辑删除记录。返回删除条数。

        保留旧签名（返回 ``int``）不变；需要明细（backlog 规模、抽查结果、
        是否提前停止）的调用方用 :meth:`purge_report`。
        """

        return self.purge_report(
            policy=policy,
            now=now,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
            recall_sample=recall_sample,
        ).removed

    def purge_report(
        self,
        *,
        policy: RetentionPolicy | None = None,
        now: datetime | None = None,
        dry_run: bool = False,
        batch_size: int | None = None,
        max_batches: int | None = None,
        recall_sample: int = 3,
    ) -> PurgeReport:
        """物理清理的完整实现（04 计划 §6.3）。

        四件事，缺一不可：

        1. **只处理显式 ``deleted``**——``purge_deleted`` 的谓词里写死了
           ``status='deleted'``；expired / superseded 永远不会被这里碰到。
        2. **tombstone 先写**——每批删除**之前**落一条不含正文的审计事件
           （数量、最老删除时间、这批的 id）。审计写在删除后，一旦事务崩了
           就既没删掉、也没有记录，看不出"当时打算删什么"。
        3. **多批次循环**——单次 1000 条的上限不该等于"一天只清 1000 条"。
        4. **删完抽查召回**——抽样 ``recall_check``，只要还有一条能被检索到
           就立刻停下并留痕。"删除完成"的判定标准是"检索不到"，不是"状态字段
           变了"。
        """

        active_policy = policy or RetentionPolicy()
        cutoff = active_policy.purge_cutoff(now=now)
        limit = int(batch_size or active_policy.purge_batch_limit)
        if limit <= 0:
            raise MemoryDomainError("purge 的 batch_size 必须为正整数")

        if dry_run:
            backlog = self._repository.inspect_deleted_backlog(
                older_than=cutoff, limit=limit
            )
            self._trace(
                "memory_purge_dry_run",
                {
                    "would_remove": backlog.count,
                    "oldest_deleted_at": (
                        backlog.oldest_deleted_at.isoformat()
                        if backlog.oldest_deleted_at
                        else None
                    ),
                    "estimated_bytes": backlog.estimated_bytes,
                    "retention_floor_days": max(
                        int(active_policy.compliance_floor_days),
                        int(active_policy.soft_delete_retention_days),
                    ),
                },
            )
            return PurgeReport(dry_run=True, removed=backlog.count, backlog=backlog)

        removed = 0
        batches = 0
        leaks: list[str] = []
        stopped_early = False
        while max_batches is None or batches < max_batches:
            backlog = self._repository.inspect_deleted_backlog(
                older_than=cutoff, limit=limit
            )
            if backlog.count == 0:
                break
            # tombstone：先记后删。payload 只放数量/时间/id，绝不放正文。
            self._trace(
                "memory_purge_tombstone",
                {
                    "count": backlog.count,
                    "oldest_deleted_at": (
                        backlog.oldest_deleted_at.isoformat()
                        if backlog.oldest_deleted_at
                        else None
                    ),
                    "estimated_bytes": backlog.estimated_bytes,
                    "batch": batches,
                },
            )
            deleted = self._repository.purge_deleted(older_than=cutoff, limit=limit)
            removed += deleted
            batches += 1

            if recall_sample > 0 and backlog.memory_ids:
                leaks = [
                    memory_id
                    for memory_id in backlog.memory_ids[:recall_sample]
                    if self.recall_check(memory_id)
                ]
                if leaks:
                    # 还能召回说明级联没删干净（或检索走了别的通道）。
                    # 继续下一批只会扩大污染面，立刻停。
                    self._trace("memory_purge_recall_leak", {"memory_ids": leaks})
                    stopped_early = True
                    break

            if deleted == 0:
                # 谓词匹配得到但一条没删掉：说明有并发把它改回去了，避免空转。
                break

        if removed:
            self._trace("memory_purged", {"removed": removed, "batches": batches})
        return PurgeReport(
            removed=removed,
            batches=batches,
            recall_leaks=tuple(leaks),
            stopped_early=stopped_early,
        )

    def expired_preview(self, *, now: datetime | None = None, batch_size: int = 500) -> int:
        """dry-run：本租户当前有多少条"``valid_to`` 已过但仍 active"。

        与 :meth:`archive_expired_report` 走**同一个** repository 谓词，所以
        这个数字就是"真跑一批会归档多少"。
        """

        moment = now or _now()
        page = self._repository.list_expired_candidates(
            cutoff=moment, cursor=None, batch_size=max(1, int(batch_size))
        )
        return len(page.items)

    def archive_expired(
        self,
        *,
        now: datetime | None = None,
        batch_size: int | None = None,
        max_batches: int | None = None,
        outbox_defer_seconds: int = 3600,
    ) -> int:
        """把"有效期已过但状态还是 active"的记录归档成 expired。返回条数。

        保留旧签名（返回 ``int``）；需要 cursor 的调用方用
        :meth:`archive_expired_report`。
        """

        return self.archive_expired_report(
            now=now,
            batch_size=batch_size,
            max_batches=max_batches,
            outbox_defer_seconds=outbox_defer_seconds,
        ).archived

    def archive_expired_report(
        self,
        *,
        now: datetime | None = None,
        batch_size: int | None = None,
        max_batches: int | None = None,
        outbox_defer_seconds: int = 3600,
        cursor: str | None = None,
    ) -> ExpiredArchiveReport:
        """过期归档的完整实现（04 计划 §6.2）。

        正常情况下 ``mark_expired`` 会同时改状态与 ``valid_to``，不该出现
        "``valid_to`` 已过但状态还是 active"；这里是**防御性清理**：历史遗留
        会一直占着 "active" 的名字，让审计数字对不上。

        与旧实现的区别（旧版 ``list_records(limit=1000)`` 全量拉取 +
        单条 ``mark_expired``）：

        * **keyset 分页**而不是 offset——归档会把行移出结果集，offset 必然漏行；
        * **条件更新**（``archive_expired_batch`` 里带 ``status='active'
          AND valid_to <= cutoff``），并发下最多各改一半，不会覆盖写；
        * **同事务联动 outbox**：归档后把这些记录未闭环的 embedding 任务
          推到 ``outbox_defer_seconds`` 之后，避免把算力花在刚失效的内容上。
        """

        moment = now or _now()
        size = int(batch_size or 500)
        if size <= 0:
            raise MemoryDomainError("archive_expired 的 batch_size 必须为正整数")
        defer_until = moment + timedelta(seconds=max(0, int(outbox_defer_seconds)))

        archived = 0
        deferred = 0
        batches = 0
        current = cursor
        while max_batches is None or batches < max_batches:
            page = self._repository.list_expired_candidates(
                cutoff=moment, cursor=current, batch_size=size
            )
            if not page.items:
                return ExpiredArchiveReport(
                    archived=archived,
                    outbox_deferred=deferred,
                    batches=batches,
                    cursor=None,
                    exhausted=True,
                )
            result = self._repository.archive_expired_batch(
                memory_ids=[item.memory_id for item in page.items],
                cutoff=moment,
                outbox_retry_after=defer_until,
            )
            archived += result.archived
            deferred += result.outbox_deferred
            batches += 1
            current = page.next_cursor
            if current is None:
                break

        if archived:
            self._trace(
                "memory_archived",
                {"archived": archived, "batches": batches, "outbox_deferred": deferred},
            )
        return ExpiredArchiveReport(
            archived=archived,
            outbox_deferred=deferred,
            batches=batches,
            cursor=current,
            exhausted=current is None,
        )

    # ------------------------------------------------------------- 召回校验 --

    def recall_check(self, memory_id: str, *, text: str = "Python 部署 端口 中文") -> bool:
        """删除之后做一次召回校验：还能不能被检索到。

        这是"删除请求完成后正文、向量和导出视图均不可再召回"这条完成标准的
        可执行形式——测试与运维都用它，而不是靠肉眼看状态字段。
        """

        hits = self._repository.search(MemoryQuery(text=text, limit=500))
        return any(hit.record.id == memory_id for hit in hits)

    # ------------------------------------------------------------- 内部 --

    def _trace(self, event: str, payload: dict[str, Any]) -> None:
        if self._tracer is None:
            return
        try:
            self._tracer.record_privileged_event(
                user_id=getattr(self._repository.tenant, "user_id", ""),
                project_id=getattr(self._repository.tenant, "project_id", None),
                mode="memory-admin",
                event=event,
                payload=payload,
            )
        except Exception:  # noqa: BLE001 - trace 失败不影响管理动作
            pass
