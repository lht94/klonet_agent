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

from klonet_agent.memory.domain import MemoryQuery, MemoryRecord, MemoryType, Scope

__all__ = [
    "ForgetOutcome",
    "MemoryAdmin",
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

    def purge_cutoff(self, *, now: datetime | None = None) -> datetime:
        moment = now or _now()
        return moment - timedelta(days=max(0, int(self.soft_delete_retention_days)))


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
        self, *, policy: RetentionPolicy | None = None, now: datetime | None = None
    ) -> int:
        """物理清理已过保留期的逻辑删除记录。返回删除条数。"""

        active_policy = policy or RetentionPolicy()
        removed = self._repository.purge_deleted(
            older_than=active_policy.purge_cutoff(now=now),
            limit=active_policy.purge_batch_limit,
        )
        if removed:
            self._trace("memory_purged", {"removed": removed})
        return removed

    def archive_expired(self, *, now: datetime | None = None) -> int:
        """把"有效期已过但状态还是 active"的记录归档成 expired。

        正常情况下 ``mark_expired`` 会同时改状态与 ``valid_to``，不该出现这种分歧；
        这里是**防御性清理**：万一有历史遗留（直接改过 ``valid_to`` 的写入路径），
        它们会一直占着"active"的名字，让审计数字对不上。
        """

        moment = now or _now()
        archived = 0
        for record in self._repository.list_records(limit=1000, status="active"):
            version = record.active_version
            if version is None or version.valid_to is None:
                continue
            if version.valid_to <= moment:
                self._repository.mark_expired(record.id, version.valid_to)
                archived += 1
        if archived:
            self._trace("memory_archived", {"archived": archived})
        return archived

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
