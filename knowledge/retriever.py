"""知识检索流程。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from klonet_agent.config import DEFAULT_RAG_TOP_K, KNOWLEDGE_INDEX_FILE
from klonet_agent.knowledge.indexer import KnowledgeIndexer


@dataclass
class RetrievedChunk:
    """检索返回的一条证据。"""

    source: str
    path: str
    title: str
    snippet: str
    score: float


class KnowledgeRetriever:
    """第一版关键词检索器。"""

    def __init__(self, index_file: Path = KNOWLEDGE_INDEX_FILE):
        self.index_file = index_file

    def search(self, query: str, top_k: int = DEFAULT_RAG_TOP_K) -> list[RetrievedChunk]:
        """检索相关知识片段。"""

        if not self.index_file.exists():
            KnowledgeIndexer(index_file=self.index_file).build()
        terms = _tokenize(query)
        if not terms:
            return []

        results = []
        for row in self._iter_rows():
            content = row.get("content", "")
            path = row.get("path", "")
            source = row.get("source", "local")
            score = _score(terms, content, path, query=query, source=source)
            matched_terms = _matched_term_count(terms, content, path)
            required_matches = min(3, max(1, math.ceil(len(terms) * 0.25)))
            if score < 2 or matched_terms < required_matches:
                continue
            results.append(
                RetrievedChunk(
                    source=source,
                    path=path,
                    title=row.get("title", path),
                    snippet=_make_snippet(content, terms),
                    score=score,
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:top_k]

    def _iter_rows(self):
        """逐行读取 JSONL 索引。"""

        with self.index_file.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _tokenize(text: str) -> list[str]:
    """中英文混合的轻量分词。"""

    lowered = text.lower()
    words = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]{2,}", lowered)
    return list(dict.fromkeys(words))


def _score(
    terms: list[str],
    content: str,
    path: str,
    *,
    query: str = "",
    source: str = "local",
) -> float:
    """简单关键词打分，路径命中权重更高。"""

    haystack = content.lower()
    path_text = path.lower()
    score = 0.0
    for term in terms:
        score += haystack.count(term)
        if term in path_text:
            score += 3

    normalized_query = query.strip().lower()
    if normalized_query and (
        normalized_query in haystack or normalized_query in path_text
    ):
        score += 5

    if source == "machine_index":
        routes = [
            token.strip("，。；;")
            for token in query.split()
            if token.startswith("/") and token.count("/") >= 2
        ]
        score += 100 * sum(route.lower() in haystack for route in routes)
    return score


def _matched_term_count(terms: list[str], content: str, path: str) -> int:
    """统计实际命中的查询词数量，用于过滤偶然命中。"""

    haystack = content.lower()
    path_text = path.lower()
    return sum(term in haystack or term in path_text for term in terms)


def _make_snippet(content: str, terms: list[str], width: int = 500) -> str:
    """围绕第一个命中词生成摘要。"""

    lowered = content.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    if not positions:
        return content[:width]
    center = min(positions)
    start = max(center - width // 3, 0)
    return content[start : start + width].strip()
