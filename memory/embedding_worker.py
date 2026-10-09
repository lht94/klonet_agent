"""embedding outbox 的消费者：给已经提交的正文版本补上向量。

对应计划阶段 4 的「worker 幂等生成 embedding，记录模型与版本」。三个刻意的取舍：

1. **worker 按租户运行，不是系统级特权组件。** outbox 的 RLS 沿
   ``memory_versions → memory_records`` 回溯到租户，所以一个 worker 实例只在
   一个租户的可见范围内工作。"后台补向量"这条路径因此不可能成为跨租户泄漏的
   缺口；代价是调用方要按租户调度，而不是写一个能扫全库的守护进程。
2. **崩溃恢复靠租约，不靠清理进程。** 领取任务时把 ``next_attempt_at`` 推到租约
   到期时刻；进程中途死掉，租约过期后任务自动可被重新领取。少一个必然写错的
   超时清理逻辑。
3. **失败分"值得重试"和"重试无意义"。** 网络/限流退避后重试；空向量和维度不符
   直接进终态——前者会让向量通道静默失效（schema 上却看起来"已闭环"），
   后者是配置错误，重试多少次结果都一样。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from klonet_agent.memory.repository import (
    DEFAULT_EMBEDDING_PROFILE_ID,
    EMBEDDING_DIMENSIONS,
    EmbeddingOutboxStats,
    MemoryRepository,
    PendingEmbedding,
)

__all__ = [
    "EmbeddingContentError",
    "EmbeddingRunStats",
    "EmbeddingWorker",
    "backoff_delay",
    "is_permanent_failure",
]


class EmbeddingContentError(RuntimeError):
    """正文本身无法生成可用的向量（空结果、空向量、维度不符）。

    带 ``permanent`` 标记，让「要不要再试」的判定只有一处口径：这类问题
    重试多少次都不会变。
    """

    permanent = True


@dataclass(frozen=True)
class EmbeddingRunStats:
    """一次 worker 运行的可观测结果。

    ``errors`` 只放异常的"类型 + 消息"，不放正文——向量生成失败经常发生在
    含敏感内容的记忆上，日志不该成为绕过脱敏的旁路。

    ``skipped``（04 计划阶段 3）是"领取之后发现目标已经不该写"的条数：记录被
    删除、outbox 行已被删除。它不是失败——把它记成 failed 会让正常的删除
    在监控里显示成 embedding 故障。
    """

    claimed: int = 0
    embedded: int = 0
    retried: int = 0
    abandoned: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()

    @property
    def failed(self) -> int:
        return self.retried + self.abandoned

    @property
    def ok(self) -> bool:
        return self.failed == 0


def is_permanent_failure(exc: BaseException) -> bool:
    """一次失败是否"重试无意义"。

    只看异常自带的 ``permanent`` 标记，不 import 具体客户端模块——embedder 是
    注入的，它抛什么类型不由 worker 决定。客户端想表达"别重试"就带上这个属性
    （例如 ``llm.embeddings.EmbeddingDimensionMismatch``）。
    """

    return bool(getattr(exc, "permanent", False))


def backoff_delay(attempt: int, *, base_seconds: float, max_seconds: float) -> float:
    """第 ``attempt`` 次尝试失败后的等待秒数。

    ``attempt`` 从 1 开始，用 ``2 ** (attempt - 1)``：第一次失败等
    ``base_seconds`` 而不是 ``2 * base_seconds``——重试的第一段间隔应该最短。
    """

    if attempt < 1:
        attempt = 1
    delay = float(base_seconds) * (2 ** (attempt - 1))
    return float(min(delay, float(max_seconds)))


class EmbeddingWorker:
    """按租户消费 embedding outbox。"""

    def __init__(
        self,
        repository: MemoryRepository,
        embedder: Any,
        *,
        profile_id: str = DEFAULT_EMBEDDING_PROFILE_ID,
        batch_size: int = 20,
        max_attempts: int = 5,
        backoff_base_seconds: float = 30.0,
        backoff_max_seconds: float = 3600.0,
        lease_seconds: float = 300.0,
        expected_dimensions: int | None = EMBEDDING_DIMENSIONS,
        model: str | None = None,
        model_version: str | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须为正整数")
        self._repository = repository
        self._embedder = embedder
        self._profile_id = (
            str(profile_id or "").strip() or DEFAULT_EMBEDDING_PROFILE_ID
        )
        self._batch_size = int(batch_size)
        self._max_attempts = int(max_attempts)
        self._backoff_base = float(backoff_base_seconds)
        self._backoff_max = float(backoff_max_seconds)
        self._lease_seconds = float(lease_seconds)
        self._expected_dimensions = expected_dimensions
        self._clock = now or (lambda: datetime.now(timezone.utc))
        self._model, self._model_version = self._resolve_identity(
            model, model_version
        )

    # ------------------------------------------------------------- 只读 --

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def identity(self) -> tuple[str, str]:
        """写进库的 ``(embedding_model, embedding_version)``。"""

        return (self._model, self._model_version)

    def stats(self) -> EmbeddingOutboxStats:
        """当前队列快照。``coverage`` 是判断能否依赖向量通道的依据。"""

        return self._repository.embedding_outbox_stats(profile_id=self._profile_id)

    # ------------------------------------------------------------- 运行 --

    def run(
        self,
        *,
        limit: int | None = None,
        deadline: datetime | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> EmbeddingRunStats:
        """处理至多 ``limit`` 条待办任务（默认一批）。

        不抛异常：这是后台任务，任何一条失败都只影响它自己。调用方从返回的
        统计与 ``stats()`` 观察结果。

        ``deadline`` / ``should_stop``（04 计划阶段 3）：本方法是"批内循环"，
        一批 20 条、每条可能打一次网络。没有这两个闸门，一个慢供应商会把
        Worker 的单次 tick 拖过 lease 甚至拖过整个 grace period。检查点放在
        **每个批次的边界**和**每条任务之前**——前者避免无谓地再领一批，
        后者避免已经超时还继续跑完手里的一整批。
        """

        budget = self._batch_size if limit is None else max(0, int(limit))
        claimed_total = embedded = retried = abandoned = skipped = 0
        errors: list[str] = []

        def _stopping() -> bool:
            if should_stop is not None and should_stop():
                return True
            if deadline is not None and self._clock() >= deadline:
                return True
            return False

        while budget > 0:
            if _stopping():
                break
            batch = self._repository.claim_pending_embeddings(
                limit=min(self._batch_size, budget),
                lease_seconds=self._lease_seconds,
                profile_id=self._profile_id,
                now=self._clock(),
            )
            if not batch:
                # 剩下的都还在退避里或已闭环，本轮到此为止。
                break
            budget -= len(batch)
            claimed_total += len(batch)
            for item in batch:
                if _stopping():
                    # 手里这条不跑了：它仍是 processing，租约到期后会被重领。
                    break
                outcome, message = self._process(item)
                if outcome == "embedded":
                    embedded += 1
                elif outcome == "skipped":
                    skipped += 1
                elif outcome == "retried":
                    retried += 1
                    errors.append(message)
                else:
                    abandoned += 1
                    errors.append(message)
        return EmbeddingRunStats(
            claimed=claimed_total,
            embedded=embedded,
            retried=retried,
            abandoned=abandoned,
            skipped=skipped,
            errors=tuple(errors),
        )

    # ------------------------------------------------------------- 内部 --

    def _process(self, item: PendingEmbedding) -> tuple[str, str]:
        try:
            vector = self._embed(item.content)
        except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整批
            return self._record_failure(item, exc)
        try:
            written = self._write_back(item, vector)
        except Exception as exc:  # noqa: BLE001 - 同上；写回失败与生成失败分开归因
            return self._record_failure(item, exc)
        if written is False:
            # 记录已被删除（或 outbox 行已不在）：跳过，不算失败。
            return ("skipped", "")
        return ("embedded", "")

    def _write_back(self, item: PendingEmbedding, vector: tuple[float, ...]) -> bool:
        """把向量写回存储。default 走 ``set_embedding``（memory_versions 单列）；
        非 default profile 由 :class:`ReembeddingWorker` 覆盖，写多 profile 表
        （04 计划 §6.5 双 profile）。"""
        return self._repository.set_embedding(
            item.version_id,
            vector,
            embedding_model=self._model,
            embedding_version=self._model_version,
        )

    def _embed(self, content: str) -> tuple[float, ...]:
        raw = self._embedder(str(content or ""))
        if raw is None:
            raise EmbeddingContentError("embedding 服务返回了空结果")
        vector = tuple(float(value) for value in raw)
        if not vector:
            # 空向量绝不当成功：写进去等于让语义通道静默失效，而 outbox 上
            # 看起来"已闭环"，没有任何地方会报错。
            raise EmbeddingContentError("embedding 服务返回了空向量")
        expected = self._expected_dimensions
        if expected is not None and len(vector) != expected:
            raise EmbeddingContentError(
                f"embedding 返回 {len(vector)} 维，期望 {expected} 维"
            )
        return vector

    def _record_failure(
        self, item: PendingEmbedding, exc: BaseException
    ) -> tuple[str, str]:
        """记一次失败并决定要不要再试。"""

        message = f"{type(exc).__name__}: {exc}"
        permanent = is_permanent_failure(exc)
        exhausted = item.attempt_count >= self._max_attempts
        if permanent or exhausted:
            retry_at = None
            outcome = "abandoned"
        else:
            retry_at = self._clock() + timedelta(
                seconds=backoff_delay(
                    item.attempt_count,
                    base_seconds=self._backoff_base,
                    max_seconds=self._backoff_max,
                )
            )
            outcome = "retried"
        try:
            self._repository.mark_embedding_failed(
                item.version_id,
                message,
                retry_at=retry_at,
                profile_id=item.embedding_profile_id or self._profile_id,
            )
        except Exception:  # noqa: BLE001
            # 连"失败"都写不进去（数据库不可用）时也不能向上抛：这是后台路径，
            # 不该影响任何用户请求。任务仍是 processing，租约到期后会被重领。
            pass
        return (outcome, message)

    def _resolve_identity(
        self, model: str | None, model_version: str | None
    ) -> tuple[str, str]:
        """决定写进 ``memory_versions`` 的模型与版本身份。

        优先用显式参数，其次读 embedder 自己的 ``identity``（如
        ``EmbeddingClient.identity``），最后退回一个可辨识的占位串——宁可记一个
        不精确的名字，也不能让"有向量但不知道是谁算的"落库：schema 上的
        ``memory_versions_embedding_identified`` 约束会直接拒掉这种写入。
        """

        resolved_model = str(model or "").strip()
        resolved_version = str(model_version or "").strip()
        if not resolved_model or not resolved_version:
            identity = getattr(self._embedder, "identity", None)
            if isinstance(identity, (tuple, list)) and len(identity) == 2:
                resolved_model = resolved_model or str(identity[0] or "").strip()
                resolved_version = resolved_version or str(identity[1] or "").strip()
        if not resolved_model:
            resolved_model = type(self._embedder).__name__ or "unknown"
        if not resolved_version:
            resolved_version = resolved_model
        return resolved_model, resolved_version
