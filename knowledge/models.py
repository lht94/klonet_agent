"""检索架构使用的结构化数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


QueryScope = Literal["klonet", "general", "mixed"]
TaskType = Literal[
    "auto",
    "concept",
    "deployment_preparation",
    "deployment_guidance",
    "credential_boundary",
    "operation_guide",
    "troubleshooting",
    "code_lookup",
    "development",
    "project_progress",
    "general",
]
RelevanceStatus = Literal["reliable", "weak", "none"]
KnowledgeStore = Literal["public_docs", "source_code"]


@dataclass(frozen=True)
class RetrievalTask:
    """Planner 生成的一个知识库检索任务。"""

    store: KnowledgeStore
    purpose: str
    keyword_queries: tuple[str, ...] = ()
    semantic_queries: tuple[str, ...] = ()
    exact_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalPlan:
    """一次 LLM 调用生成的完整检索计划。"""

    original_query: str
    standalone_query: str
    retrieval_tasks: tuple[RetrievalTask, ...]
    excluded_terms: tuple[str, ...] = ()
    confidence: float = 0.0
    status: str = "planned"
    detail: str = ""


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
    intent: str = "unknown"
    excluded_intents: tuple[str, ...] = ()
    min_priority: str | None = None
    collections: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
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
    intent_tags: tuple[str, ...] = ()
    bm25_score: float = 0.0
    exact_score: float = 0.0
    semantic_score: float = 0.0
    metadata_score: float = 1.0
    matched_terms: tuple[str, ...] = ()
    relevance: str = "weak"
    recall_channels: tuple[str, ...] = ()
    task_purposes: tuple[str, ...] = ()
    rrf_score: float = 0.0
    rerank_score: float | None = None
    final_score: float = 0.0
    symbol: str = ""
    line_start: int | None = None
    line_end: int | None = None
    rerank_text: str = ""


@dataclass
class SearchOutcome:
    """检索结果及整体置信度。"""

    status: RelevanceStatus
    results: list[RetrievedChunk] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    retrieval_mode: str = "bm25"
    vector_status: str = "not_loaded"
    vector_status_detail: str = ""
    retrieval_plan: RetrievalPlan | None = None
    planner_status: str = "not_used"
    recall_counts: dict[str, int] = field(default_factory=dict)
    fusion_status: str = "not_used"
    rerank_status: str = "not_used"
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
