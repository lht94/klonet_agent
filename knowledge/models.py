"""检索架构使用的结构化数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


QueryScope = Literal["klonet", "general", "mixed"]
TaskType = Literal[
    "auto",
    "concept",
    "troubleshooting",
    "code_lookup",
    "development",
    "project_progress",
    "general",
]
RelevanceStatus = Literal["reliable", "weak", "none"]


@dataclass(frozen=True)
class QueryRoute:
    """问题范围、任务类型和路由置信度。"""

    scope: QueryScope
    confidence: float
    task_type: TaskType
    domains: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    hard_disable_rag: bool = False


@dataclass(frozen=True)
class SearchRequest:
    """一次结构化知识检索请求。"""

    query: str
    task_type: TaskType = "auto"
    layers: tuple[str, ...] | None = None
    domains: tuple[str, ...] | None = None
    min_priority: str | None = None
    exclude_sensitivity: tuple[str, ...] = ("review_required", "restricted")
    top_k: int = 3


@dataclass
class RetrievedChunk:
    """检索返回的一条带评分证据。"""

    chunk_id: str
    layer: str
    source: str
    path: str
    title: str
    snippet: str
    domain: str
    priority: str
    status: str
    quality: str
    sensitivity: str
    last_verified: str
    score: float
    bm25_score: float = 0.0
    exact_score: float = 0.0
    metadata_score: float = 1.0
    matched_terms: tuple[str, ...] = ()
    relevance: str = "weak"


@dataclass
class SearchOutcome:
    """检索结果及整体置信度。"""

    status: RelevanceStatus
    results: list[RetrievedChunk] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
