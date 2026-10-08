"""记忆召回：硬过滤 → 三通道召回 → RRF 融合 → 可选 rerank → 冲突标注。

对应计划 §6.6 与阶段 4 清单。真正的召回 SQL 在 ``memory/postgres.py``
（``search`` 与 ``_lexical_candidates`` / ``_semantic_candidates`` /
``_exact_candidates`` / ``_fuse``）；本模块负责它周围的三件事：

1. **把查询变成向量**，并在拿不到向量时明确降级，而不是让语义通道
   悄悄缺席——"这次只有关键词结果"必须是调用方看得见的事实。
2. **把数据库错误分类**。三种错误必须区别对待：pgvector 扩展缺失是部署错误
   （schema 没按迁移建好），整个数据库不可用可以诚实地说"不知道"
   （返回空结果并记录），其余异常原样抛出，不让 bug 藏进"降级"里。
3. **把互斥事实标注出来**。计划 §6.5 规则 5 要求模型不得静默合并互斥事实，
   所以召回阶段就要说明"这几条互相矛盾"，而不是让模型自己撞见。

有界注入（按 token 预算裁剪、渲染成固定的记忆块）属于阶段 5 的 MemoryPack；
本模块只做"取回哪些记忆"，不做"放多少进上下文"。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from klonet_agent.memory.domain import (
    MemoryHit,
    MemoryQuery,
    RelationType,
)
from klonet_agent.memory.repository import (
    EMBEDDING_DIMENSIONS,
    MemoryRepository,
)

__all__ = [
    "MemoryRetrievalError",
    "MemoryRetrievalReport",
    "MemoryRetriever",
    "default_rerank_document",
    "sqlstate_of",
]


class MemoryRetrievalError(RuntimeError):
    """召回无法进行下去（部署级错误）。

    目前只在一种情况下抛出：数据库报的是 schema 类错误（SQLSTATE ``42xxx``），
    例如 pgvector 扩展没装、``vector(1024)`` 列不存在。这类问题"降级成全文检索"
    是错的——它会把一个部署缺陷变成每一次召回都少一路的静默劣化。
    """


@dataclass(frozen=True)
class MemoryRetrievalReport:
    """一次召回的完整结果与可观测信息。

    ``hits`` 与 ``degraded`` / ``errors`` 一起给出，是因为降级必须可见：
    调用方既拿得到可用的关键词结果，也知道语义通道这次没有生效、原因是什么。
    """

    query: str
    hits: tuple[MemoryHit, ...] = ()
    # 已生效的降级（向量不可用等）。非空表示"结果是关键词的，不是混合的"。
    degraded: tuple[str, ...] = ()
    # 被吞掉的异常（已按 SQLSTATE 分类）。数据库整体不可用时在这里留痕。
    errors: tuple[str, ...] = ()
    rerank: str = "skipped"
    # 命中的记忆里，哪些存在 contradicts 关系（需要向模型提示不确定）。
    conflict_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and not self.degraded

    @property
    def semantic_used(self) -> bool:
        """这次召回是否真的用上了语义通道。

        ``degraded`` 只由"查询向量拿不到"写入，所以它为空就等于语义通道生效。
        """

        return not self.degraded


def sqlstate_of(exc: BaseException) -> str | None:
    """取异常的 SQLSTATE。psycopg3 是 ``sqlstate``，psycopg2 是 ``pgcode``。"""

    for attribute in ("sqlstate", "pgcode"):
        state = getattr(exc, attribute, None)
        if state:
            return str(state)
    return None


def default_rerank_document(hit: MemoryHit) -> str:
    """把一条记忆命中渲染成 reranker 的输入文本。

    与知识库 chunk 的 ``title/path/layer`` 不同：记忆最有用的字面信号是类型、
    作用域、主体键和正文。主体键必须给——``fact:project:demo:python_version``
    这类串本身就是最强的精确证据，而向量对版本号并不敏感（计划 §6.6）。
    """

    record = hit.record
    return (
        f"type: {record.memory_type.value}\n"
        f"scope: {record.scope.value}\n"
        f"subject: {record.subject_key}\n"
        f"content:\n{hit.version.content[:2000]}"
    )


class MemoryRetriever:
    """在已绑定租户的仓库之上做混合召回。"""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        embedder: Any | None = None,
        reranker: Any | None = None,
        expected_dimensions: int | None = EMBEDDING_DIMENSIONS,
        rerank_top_n: int = 10,
        document_builder: Callable[[MemoryHit], str] | None = None,
    ):
        self._repository = repository
        self._embedder = embedder
        self._reranker = reranker
        self._expected_dimensions = expected_dimensions
        self._rerank_top_n = max(1, int(rerank_top_n))
        self._document_builder = document_builder or default_rerank_document

    # ------------------------------------------------------------- 主入口 --

    def retrieve(self, query: MemoryQuery) -> MemoryRetrievalReport:
        """执行一次召回。

        不因为"召回失败"而中断用户请求：数据库整体不可用时返回空结果并把它
        记为 error；但 schema 类错误会抛 :class:`MemoryRetrievalError`，因为那
        说明部署本身是坏的，继续跑只会给出更差的答案。
        """

        vector, degrade_reason = self._query_vector(query.text)
        degraded: list[str] = []
        if degrade_reason:
            degraded.append(degrade_reason)

        try:
            hits = self._repository.search(query, query_embedding=vector)
        except Exception as exc:  # noqa: BLE001 - 分类见 _raise_or_degrade
            error = self._classify(exc)
            if error is not None:
                raise error from exc
            return MemoryRetrievalReport(
                query=query.text,
                degraded=tuple(degraded),
                errors=(f"database_unavailable:{type(exc).__name__}",),
            )

        hits, rerank_status = self._rerank(query.text, hits)
        hits, conflicts = self._annotate_conflicts(hits)
        return MemoryRetrievalReport(
            query=query.text,
            hits=tuple(hits),
            degraded=tuple(degraded),
            rerank=rerank_status,
            conflict_ids=conflicts,
        )

    # ------------------------------------------------------------- 查询向量 --

    def _query_vector(
        self, text: str
    ) -> tuple[tuple[float, ...] | None, str | None]:
        """``(向量, 降级原因)``。拿不到向量时第二个元素非空。

        刻意**不重试**：召回在用户请求路径上，等待重试只会让回答变慢，
        而关键词通道本来就能给出可用的结果。
        """

        embedder = self._embedder
        if embedder is None:
            return (None, "no_embedder:semantic_channel_skipped")
        try:
            raw = embedder(str(text))
        except Exception as exc:  # noqa: BLE001 - 降级而非失败
            return (None, f"embed_failed:{type(exc).__name__}")
        if raw is None:
            return (None, "embed_empty")
        try:
            vector = tuple(float(value) for value in raw)
        except (TypeError, ValueError):
            return (None, "embed_not_numeric")
        if not vector:
            return (None, "embed_empty")
        expected = self._expected_dimensions
        if expected is not None and len(vector) != expected:
            # 维度不符属于配置错误，但召回不该因此挂掉：退化为关键词通道，
            # 并把原因写进报告，让"语义通道其实一直没生效"这件事可见。
            return (None, f"embed_dimension_mismatch:{len(vector)}!={expected}")
        return (vector, None)

    # ------------------------------------------------------------- rerank --

    def _rerank(
        self, text: str, hits: list[MemoryHit]
    ) -> tuple[list[MemoryHit], str]:
        reranker = self._reranker
        if reranker is None:
            return (hits, "skipped:no_reranker")
        if not hits:
            return (hits, "skipped:no_candidates")
        if not getattr(reranker, "available", True):
            return (hits, "fallback:missing_credentials")

        documents = [self._document_builder(hit) for hit in hits]
        try:
            items = reranker.rerank_documents(
                text, documents, top_n=min(self._rerank_top_n, len(documents))
            )
        except Exception as exc:  # noqa: BLE001 - rerank 是可选增强
            return (hits, f"fallback:{type(exc).__name__}")
        if not items:
            return (hits, "fallback:empty_response")

        # rerank 只对部分候选给分时，没分的不丢，只是排在有分的后面：
        # 丢掉它们等于让一次不完整的 rerank 响应删掉召回结果。
        scores = {item.index: item.relevance_score for item in items}
        ranked = [
            (scores.get(position), position, hit)
            for position, hit in enumerate(hits)
        ]
        ranked.sort(key=lambda row: (row[0] is None, -(row[0] or 0.0), row[1]))
        return ([row[2] for row in ranked], "applied")

    # ------------------------------------------------------------- 冲突 --

    def _annotate_conflicts(
        self, hits: list[MemoryHit]
    ) -> tuple[list[MemoryHit], tuple[str, ...]]:
        """标注互相矛盾的记忆，但不因此改变排序。

        排序表达相关性，冲突表达可信度，混在一起两个问题都说不清。标注方式是把
        ``conflict:contradicts`` 加进命中的 ``reasons``，调用方渲染时自然会带上。

        每条命中一次查询是有意的取舍：``limit`` 默认 10，走的是主键索引，
        比"先批量取、再在 Python 里拼"少一份需要与 RLS 对齐的批量 SQL。
        """

        if not hits:
            return (hits, ())
        annotated: list[MemoryHit] = []
        conflicting: list[str] = []
        for hit in hits:
            try:
                relations = self._repository.list_relations(
                    hit.record.id, relation_type=RelationType.CONTRADICTS
                )
            except Exception:  # noqa: BLE001 - 标注失败不该毁掉召回结果
                relations = []
            if relations:
                conflicting.append(hit.record.id)
                annotated.append(
                    replace(
                        hit,
                        reasons=tuple(hit.reasons) + ("conflict:contradicts",),
                    )
                )
            else:
                annotated.append(hit)
        return (annotated, tuple(sorted(conflicting)))

    # ------------------------------------------------------------- 错误分类 --

    def _classify(self, exc: BaseException) -> MemoryRetrievalError | None:
        """把数据库异常分类。

        返回 ``None`` 表示"可以降级"（调用方返回空结果）；返回异常表示必须抛出。
        未知异常（没有 SQLSTATE、也不是已知的驱动类型）也是"原样抛出"——
        把它当成降级会把编程错误变成静默的空结果。
        """

        state = sqlstate_of(exc)
        if state is None:
            raise exc
        if state.startswith("42"):
            # 42501 权限不足、42704 对象不存在、42883 函数/算子不存在……
            # pgvector 缺失就落在 42704/42883 上：这是 schema 与代码不一致，
            # 报错才是正确的行为。
            return MemoryRetrievalError(
                f"记忆库 schema 与代码不一致（SQLSTATE {state}）："
                f"{type(exc).__name__}: {exc}。"
                "pgvector 扩展或 vector 列缺失时不能降级——那会让每次召回"
                "都少一路而没人察觉。请检查迁移是否已应用"
            )
        if state.startswith("08"):
            # connection_exception：数据库不可达。此时"没有相关记忆"是诚实的
            # 回答（我们确实不知道），但必须留下可观测的错误标记。
            return None
        raise exc
