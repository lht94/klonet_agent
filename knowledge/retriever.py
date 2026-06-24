"""BM25、精确匹配和 metadata 分层检索。"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi

from klonet_agent.config import DEFAULT_RAG_TOP_K, KNOWLEDGE_INDEX_FILE
from klonet_agent.knowledge.indexer import INDEX_SCHEMA_VERSION, KnowledgeIndexer
from klonet_agent.knowledge.models import (
    RetrievedChunk,
    SearchOutcome,
    SearchRequest,
)
from klonet_agent.knowledge.tokenizer import DEFAULT_TOKENIZER, MixedTokenizer


_TASK_LAYER_WEIGHTS = {
    "concept": {
        "curated": 1.5,
        "experience": 1.1,
        "machine_index": 0.8,
        "local": 1.0,
    },
    "troubleshooting": {
        "experience": 1.8,
        "curated": 1.1,
        "machine_index": 1.0,
        "local": 0.9,
    },
    "code_lookup": {
        "machine_index": 2.0,
        "curated": 1.0,
        "experience": 0.8,
        "local": 0.8,
    },
    "development": {
        "machine_index": 1.4,
        "curated": 1.3,
        "experience": 1.0,
        "local": 0.9,
    },
    "operation_guide": {
        "curated": 1.7,
        "experience": 1.0,
        "machine_index": 0.7,
        "local": 0.8,
    },
    "project_progress": {
        "local": 1.5,
        "experience": 1.0,
        "curated": 1.0,
        "machine_index": 0.7,
    },
    "general": {
        "curated": 1.0,
        "experience": 1.0,
        "machine_index": 1.0,
        "local": 1.0,
    },
}
_PRIORITY_WEIGHTS = {"P0": 1.25, "P1": 1.1, "P2": 1.0, "P3": 0.85}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_QUALITY_WEIGHTS = {
    "verified": 1.2,
    "reviewed": 1.1,
    "generated": 0.95,
    "unknown": 0.9,
}
_DOMAIN_ALIASES = {
    "runtime": {"runtime", "operations", "deployment", "dependencies", "environment"},
}
_ROUTE_RE = re.compile(r"/[A-Za-z0-9_<>.-]+(?:/[A-Za-z0-9_<>.-]+)+/?")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:API|Manager|Worker|Config)\b")


class KnowledgeRetriever:
    """缓存 JSONL 索引，并执行结构化 BM25 检索。"""

    def __init__(
        self,
        index_file: Path = KNOWLEDGE_INDEX_FILE,
        tokenizer: MixedTokenizer | None = None,
    ):
        self.index_file = index_file
        self.tokenizer = tokenizer or DEFAULT_TOKENIZER
        self._mtime_ns: int | None = None
        self._rows: list[dict[str, Any]] = []
        self._corpus_tokens: list[list[str]] = []
        self._bm25: BM25Okapi | None = None

    def search(
        self,
        query: str,
        top_k: int = DEFAULT_RAG_TOP_K,
        *,
        task_type: str = "auto",
        layers: tuple[str, ...] | None = None,
        domains: tuple[str, ...] | None = None,
    ) -> list[RetrievedChunk]:
        """兼容旧调用，返回检索结果列表。"""

        return self.search_request(
            SearchRequest(
                query=query,
                task_type=task_type,
                layers=layers,
                domains=domains,
                top_k=top_k,
            )
        ).results

    def search_request(self, request: SearchRequest) -> SearchOutcome:
        """执行 BM25 召回、精确加权、metadata 过滤和置信度判断。"""

        self._ensure_loaded()
        query_tokens = self.tokenizer.tokenize(request.query)
        if not query_tokens or not self._rows or self._bm25 is None:
            return SearchOutcome(status="none", reason="empty_query_or_index")

        task_type = "concept" if request.task_type == "auto" else request.task_type
        bm25_scores = self._bm25.get_scores(query_tokens)
        candidates: list[RetrievedChunk] = []

        for index, row in enumerate(self._rows):
            metadata = _row_metadata(row)
            if not _allowed_by_request(metadata, request):
                continue
            if not _allowed_section_by_intent(row, request):
                continue

            document_tokens = set(self._corpus_tokens[index])
            matched_terms = tuple(
                token for token in query_tokens if token in document_tokens
            )
            exact_score = _exact_score(request.query, query_tokens, row)
            bm25_score = max(0.0, float(bm25_scores[index]))
            if not _has_enough_evidence(query_tokens, matched_terms, exact_score, bm25_score):
                continue

            metadata_score = _metadata_weight(metadata, task_type)
            lexical_score = min(len(matched_terms), 8) * 1.5
            final_score = (bm25_score + exact_score + lexical_score) * metadata_score
            if final_score < 2.0:
                continue

            candidates.append(
                RetrievedChunk(
                    chunk_id=str(row.get("chunk_id") or f"legacy-{index}"),
                    layer=metadata["layer"],
                    source=str(row.get("source") or metadata["layer"]),
                    path=str(row.get("path") or ""),
                    title=str(row.get("title") or row.get("path") or ""),
                    snippet=_make_snippet(str(row.get("content") or ""), query_tokens),
                    domain=metadata["domain"],
                    priority=metadata["priority"],
                    status=metadata["status"],
                    quality=metadata["quality"],
                    sensitivity=metadata["sensitivity"],
                    last_verified=metadata["last_verified"],
                    score=round(final_score, 4),
                    intent_tags=metadata["intent_tags"],
                    bm25_score=round(bm25_score, 4),
                    exact_score=round(exact_score, 4),
                    metadata_score=round(metadata_score, 4),
                    matched_terms=matched_terms,
                )
            )

        candidates.sort(key=lambda item: item.score, reverse=True)
        selected = _select_candidates(candidates, request)
        return _classify_outcome(selected)

    def _ensure_loaded(self):
        """索引文件变化时重建内存 BM25，未变化时复用缓存。"""

        if not self.index_file.exists():
            KnowledgeIndexer(index_file=self.index_file).build()
        if not self.index_file.exists():
            return

        mtime_ns = self.index_file.stat().st_mtime_ns
        if self._mtime_ns == mtime_ns:
            return

        rows = _read_index_rows(self.index_file)
        if (
            self.index_file.resolve() == KNOWLEDGE_INDEX_FILE.resolve()
            and _needs_intent_metadata_migration(rows)
        ):
            KnowledgeIndexer(index_file=self.index_file).build()
            rows = _read_index_rows(self.index_file)
            mtime_ns = self.index_file.stat().st_mtime_ns

        corpus = []
        for row in rows:
            title_tokens = self.tokenizer.tokenize(str(row.get("title") or ""))
            path_tokens = self.tokenizer.tokenize(str(row.get("path") or ""))
            content_tokens = self.tokenizer.tokenize(str(row.get("content") or ""))
            corpus.append(title_tokens * 2 + path_tokens * 2 + content_tokens)

        self._rows = rows
        self._corpus_tokens = corpus
        self._bm25 = BM25Okapi(corpus) if corpus else None
        self._mtime_ns = mtime_ns


def _read_index_rows(index_file: Path) -> list[dict[str, Any]]:
    rows = []
    with index_file.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _needs_intent_metadata_migration(rows: list[dict[str, Any]]) -> bool:
    """缺少意图字段或仍使用旧切块结构时重建默认索引。"""

    return bool(rows) and any(
        "intent_tags" not in row
        or row.get("index_schema_version") != INDEX_SCHEMA_VERSION
        for row in rows
    )


def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """兼容旧索引行，为缺失 metadata 提供稳定默认值。"""

    layer = str(row.get("layer") or row.get("source") or "local")
    return {
        "layer": layer,
        "domain": str(row.get("domain") or "general"),
        "priority": str(row.get("priority") or "P2").upper(),
        "status": str(row.get("status") or "current").lower(),
        "quality": str(row.get("quality") or "unknown").lower(),
        "sensitivity": str(row.get("sensitivity") or "public").lower(),
        "last_verified": str(row.get("last_verified") or ""),
        "intent_tags": _intent_tags(row.get("intent_tags")),
    }


def _intent_tags(value: Any) -> tuple[str, ...]:
    raw_items = value if isinstance(value, list) else str(value or "").split(",")
    return tuple(
        dict.fromkeys(
            normalized
            for item in raw_items
            if (normalized := str(item or "").strip().lower())
        )
    )


def _allowed_by_request(metadata: dict[str, Any], request: SearchRequest) -> bool:
    """执行 layer、domain、priority、状态和敏感度过滤。"""

    if metadata["status"] == "deprecated":
        return False
    if metadata["sensitivity"] in request.exclude_sensitivity:
        return False
    if request.layers and metadata["layer"] not in request.layers:
        return False
    if request.domains and not _domains_match(metadata["domain"], request.domains):
        return False
    if request.intent != "unknown" and request.intent not in metadata["intent_tags"]:
        return False
    if set(request.excluded_intents).intersection(metadata["intent_tags"]):
        return False
    if request.min_priority:
        row_rank = _PRIORITY_RANK.get(metadata["priority"], 99)
        limit_rank = _PRIORITY_RANK.get(request.min_priority.upper(), 99)
        if row_rank > limit_rank:
            return False
    return True


def _allowed_section_by_intent(row: dict[str, Any], request: SearchRequest) -> bool:
    """同一 Runbook 内按操作意图排除明显相反或故障型章节。"""

    if request.intent != "platform_start":
        return True
    segments = [
        segment.strip()
        for segment in str(row.get("title") or "").split("/")
        if segment.strip()
    ]
    if segments and "启动、停止与重启" in segments[0]:
        segments = segments[1:]
    section_title = " / ".join(segments)
    forbidden = ("常见问题", "正常停止", "正常重启", "失败", "无法")
    return not any(term in section_title for term in forbidden)


def _domains_match(row_domain: str, request_domains: tuple[str, ...]) -> bool:
    """兼容路由领域名与知识 frontmatter 领域名。"""

    for request_domain in request_domains:
        aliases = _DOMAIN_ALIASES.get(request_domain, {request_domain})
        if row_domain in aliases:
            return True
    return False


def _metadata_weight(metadata: dict[str, str], task_type: str) -> float:
    """按任务类型、优先级和质量计算可解释权重。"""

    layer_weights = _TASK_LAYER_WEIGHTS.get(
        task_type,
        _TASK_LAYER_WEIGHTS["concept"],
    )
    return (
        layer_weights.get(metadata["layer"], 1.0)
        * _PRIORITY_WEIGHTS.get(metadata["priority"], 0.9)
        * _QUALITY_WEIGHTS.get(metadata["quality"], 0.9)
    )


def _exact_score(query: str, query_tokens: list[str], row: dict[str, Any]) -> float:
    """API 路由、源码标识符、标题和路径精确命中优先。"""

    content = str(row.get("content") or "").lower()
    title = str(row.get("title") or "").lower()
    path = str(row.get("path") or "").lower()
    layer = str(row.get("layer") or row.get("source") or "local")
    score = 0.0

    routes = [match.group(0).lower() for match in _ROUTE_RE.finditer(query)]
    for route in routes:
        if route in content or route in title:
            score += 120 if layer == "machine_index" else 30

    identifiers = [match.group(0).lower() for match in _IDENTIFIER_RE.finditer(query)]
    for identifier in identifiers:
        if identifier in content or identifier in title or identifier in path:
            score += 50 if layer == "machine_index" else 20

    for token in query_tokens:
        if len(token) < 2:
            continue
        if token == title:
            score += 12
        elif token in title:
            score += 4
        if token in path:
            score += 3
    return score


def _has_enough_evidence(
    query_tokens: list[str],
    matched_terms: tuple[str, ...],
    exact_score: float,
    bm25_score: float,
) -> bool:
    """过滤只碰巧命中一个通用词的结果。"""

    if exact_score >= 20:
        return True
    required = 1 if len(query_tokens) == 1 else min(
        3,
        max(2, math.ceil(len(query_tokens) * 0.2)),
    )
    return len(matched_terms) >= required and (
        bm25_score > 0 or len(matched_terms) >= 2
    )


def _classify_outcome(results: list[RetrievedChunk]) -> SearchOutcome:
    """根据最高分、精确命中和分差判断整体可靠性。"""

    if not results:
        return SearchOutcome(status="none", reason="no_relevant_evidence")

    top = results[0]
    second_score = results[1].score if len(results) > 1 else 0.0
    margin = top.score - second_score
    reliable = (
        top.exact_score >= 20
        or (
            len(top.matched_terms) >= 2
            and top.score >= 6
            and (margin >= 0.5 or top.bm25_score >= 1.0)
        )
    )
    status = "reliable" if reliable else "weak"
    confidence = min(1.0, top.score / 20)
    for result in results:
        result.relevance = status if result is top else "candidate"
    return SearchOutcome(
        status=status,
        results=results,
        confidence=round(confidence, 4),
        reason="high_confidence_match" if reliable else "weak_match",
    )


def _make_snippet(content: str, terms: list[str], width: int = 500) -> str:
    """围绕最早命中词生成摘要。"""

    lowered = content.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    if not positions:
        return content[:width]
    center = min(positions)
    start = max(center - width // 3, 0)
    return content[start:start + width].strip()


def _select_candidates(
    candidates: list[RetrievedChunk],
    request: SearchRequest,
) -> list[RetrievedChunk]:
    limit = max(1, request.top_k)
    if request.intent != "platform_start":
        return candidates[:limit]

    stage_terms = (
        "服务器角色与启动顺序",
        "检查并启动 Redis",
        "启动 Master",
        "启动 Celery",
        "启动 Web Terminal",
        "启动 Worker",
        "配置并重载 Nginx",
        "启动后验证",
    )
    selected: list[RetrievedChunk] = []
    for stage_term in stage_terms:
        matches = [
            item
            for item in candidates
            if stage_term in item.title and item not in selected
        ]
        if not matches:
            continue
        matches.sort(key=_operation_variant_rank)
        selected.append(matches[0])
        if len(selected) >= limit:
            return selected

    for item in candidates:
        if item not in selected:
            selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def _operation_variant_rank(item: RetrievedChunk) -> tuple[int, float]:
    preferred = item.title.endswith("/ 服务器路径") or "nginx -t" in item.snippet
    return (0 if preferred else 1, -item.score)


# 保留旧模块级函数，避免已有教学代码导入失败。
def _tokenize(text: str) -> list[str]:
    return DEFAULT_TOKENIZER.tokenize(text)
