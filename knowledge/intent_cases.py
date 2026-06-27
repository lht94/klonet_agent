"""Intent-case examples used as dynamic few-shot context for intent analysis."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from klonet_agent.llm.embeddings import build_default_embedding_provider
from klonet_agent.knowledge.tokenizer import DEFAULT_TOKENIZER


DEFAULT_INTENT_CASE_ROOT = Path(__file__).resolve().parent / "intent_cases"
EmbeddingProvider = Callable[[str], Sequence[float]]


@dataclass(frozen=True)
class IntentCase:
    """A curated example of how to understand an intent turn."""

    case_id: str
    tag: str
    history_pattern: str = ""
    latest_query: str = ""
    normalized_query: str = ""
    handling_rule: str = ""
    semantic_frame: Mapping[str, Any] | None = None
    intent: Mapping[str, Any] | None = None
    slots: Mapping[str, Any] | None = None
    safety: Mapping[str, Any] | None = None
    score: float = 0.0
    keyword_score: float = 0.0
    semantic_score: float = 0.0
    retrieval_mode: str = "keyword"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "IntentCase | None":
        case_id = str(value.get("case_id") or "").strip()
        tag = str(value.get("tag") or "").strip()
        if not case_id or tag not in {"intent_parse", "direct_answer", "safety_boundary"}:
            return None
        return cls(
            case_id=case_id,
            tag=tag,
            history_pattern=str(value.get("history_pattern") or "").strip(),
            latest_query=str(value.get("latest_query") or "").strip(),
            normalized_query=str(value.get("normalized_query") or "").strip(),
            handling_rule=str(value.get("handling_rule") or "").strip(),
            semantic_frame=_mapping(value.get("semantic_frame")),
            intent=_mapping(value.get("intent")),
            slots=_mapping(value.get("slots")),
            safety=_mapping(value.get("safety")),
        )

    def searchable_text(self) -> str:
        parts = [
            self.history_pattern,
            self.latest_query,
            self.normalized_query,
            self.handling_rule,
            json.dumps(self.semantic_frame or {}, ensure_ascii=False),
            json.dumps(self.intent or {}, ensure_ascii=False),
            json.dumps(self.slots or {}, ensure_ascii=False),
        ]
        return "\n".join(part for part in parts if part)

    def with_score(
        self,
        score: float,
        *,
        keyword_score: float = 0.0,
        semantic_score: float = 0.0,
        retrieval_mode: str = "keyword",
    ) -> "IntentCase":
        return IntentCase(
            case_id=self.case_id,
            tag=self.tag,
            history_pattern=self.history_pattern,
            latest_query=self.latest_query,
            normalized_query=self.normalized_query,
            handling_rule=self.handling_rule,
            semantic_frame=self.semantic_frame,
            intent=self.intent,
            slots=self.slots,
            safety=self.safety,
            score=round(score, 4),
            keyword_score=round(keyword_score, 4),
            semantic_score=round(semantic_score, 4),
            retrieval_mode=retrieval_mode,
        )


class IntentCaseRetriever:
    """Retrieve intent examples before the LLM performs semantic parsing."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        keyword_weight: float = 0.35,
        semantic_weight: float = 0.65,
    ):
        self.root = Path(root) if root is not None else DEFAULT_INTENT_CASE_ROOT
        self.cases = load_intent_cases(root=self.root)
        self.embedding_provider = embedding_provider
        self.keyword_weight = keyword_weight
        self.semantic_weight = semantic_weight
        self._case_embeddings: dict[str, tuple[float, ...]] = {}

    def search(
        self,
        query: str,
        *,
        top_k: int = 4,
        min_score: float = 0.62,
    ) -> tuple[IntentCase, ...]:
        if not self.cases:
            return ()
        query_tokens = _tokens(query)
        query_embedding = self._embed(query)
        if not query_tokens and query_embedding is None:
            return ()

        scored = []
        for case in self.cases:
            keyword_score = _similarity(query_tokens, _tokens(case.searchable_text()))
            semantic_score = _cosine_similarity(
                query_embedding,
                self._case_embedding(case),
            )
            score = self._combined_score(keyword_score, semantic_score)
            if score >= min_score:
                scored.append(
                    case.with_score(
                        score,
                        keyword_score=keyword_score,
                        semantic_score=semantic_score,
                        retrieval_mode=(
                            "hybrid" if self.embedding_provider is not None else "keyword"
                        ),
                    )
                )
        scored.sort(key=lambda item: item.score, reverse=True)
        return tuple(scored[:top_k])

    def search_for_prompt(
        self,
        query: str,
        *,
        top_k: int = 4,
        min_score: float = 0.62,
        conflict_margin: float = 0.05,
    ) -> tuple[IntentCase, ...]:
        """Return only cases safe enough to inject as few-shot prompt context."""

        matches = self.search(query, top_k=max(top_k, 6), min_score=min_score)
        if not matches:
            return ()
        if _has_near_tie_conflict(matches, conflict_margin=conflict_margin):
            return ()
        return tuple(
            case
            for case in matches[:top_k]
            if case.tag == "intent_parse"
            and not _safety_flag(case, "block_prompt_injection")
        )

    def _embed(self, text: str) -> tuple[float, ...] | None:
        if self.embedding_provider is None:
            return None
        try:
            values = self.embedding_provider(text)
        except Exception:
            return None
        return _vector(values)

    def _case_embedding(self, case: IntentCase) -> tuple[float, ...] | None:
        if self.embedding_provider is None:
            return None
        if case.case_id not in self._case_embeddings:
            embedding = self._embed(case.searchable_text())
            if embedding is None:
                return None
            self._case_embeddings[case.case_id] = embedding
        return self._case_embeddings.get(case.case_id)

    def _combined_score(self, keyword_score: float, semantic_score: float) -> float:
        if self.embedding_provider is None:
            return keyword_score
        return (
            self.keyword_weight * keyword_score
            + self.semantic_weight * semantic_score
        )


def build_default_intent_case_retriever(
    root: Path | str | None = None,
) -> IntentCaseRetriever:
    """Build intent-case retrieval with real embeddings when configured."""

    return IntentCaseRetriever(
        root=root,
        embedding_provider=build_default_embedding_provider(),
    )


def load_intent_cases(root: Path | str | None = None) -> tuple[IntentCase, ...]:
    """Load all JSONL intent-case examples below root."""

    case_root = Path(root) if root is not None else DEFAULT_INTENT_CASE_ROOT
    if not case_root.exists():
        return ()
    cases: list[IntentCase] = []
    for path in sorted(case_root.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, Mapping):
                case = IntentCase.from_mapping(raw)
                if case is not None:
                    cases.append(case)
    return tuple(cases)


def build_intent_case_query(user_input: str, recent_history: list[dict] | None) -> str:
    """Build a compact retrieval query from history signals and latest input."""

    history_lines = []
    for message in (recent_history or [])[-6:]:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if _looks_intent_relevant(content):
            history_lines.append(f"recent_{role}: {content[:500]}")

    parts = history_lines + [f"latest_query: {user_input.strip()}"]
    return "\n".join(parts)


def _looks_intent_relevant(text: str) -> bool:
    terms = (
        "A：",
        "B：",
        "场景一",
        "场景二",
        "第一种",
        "第二种",
        "部署",
        "启动",
        "环境",
        "电脑",
        "服务器",
        "虚拟机",
        "不是",
        "Klonet",
    )
    return any(term in text for term in terms)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _tokens(text: str) -> set[str]:
    tokens = set(DEFAULT_TOKENIZER.tokenize(text or ""))
    for char in ("A", "B", "a", "b"):
        if char in text:
            tokens.add(char.lower())
    return {token.lower() for token in tokens if token.strip()}


def _similarity(query_tokens: set[str], case_tokens: set[str]) -> float:
    if not query_tokens or not case_tokens:
        return 0.0
    overlap = len(query_tokens & case_tokens)
    if overlap == 0:
        return 0.0
    return overlap / math.sqrt(len(query_tokens) * len(case_tokens))


def _vector(values: Sequence[float] | None) -> tuple[float, ...] | None:
    if values is None:
        return None
    vector = tuple(float(value) for value in values)
    return vector if any(value != 0.0 for value in vector) else None


def _cosine_similarity(
    left: tuple[float, ...] | None,
    right: tuple[float, ...] | None,
) -> float:
    if left is None or right is None or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = sum(left_value * right_value for left_value, right_value in zip(left, right))
    return dot / (left_norm * right_norm)


def _has_near_tie_conflict(
    cases: tuple[IntentCase, ...],
    *,
    conflict_margin: float,
) -> bool:
    if len(cases) < 2:
        return False
    top_score = cases[0].score
    close_cases = [
        case for case in cases if top_score - case.score <= conflict_margin
    ]
    signatures = {_intent_signature(case) for case in close_cases}
    signatures.discard(())
    return len(signatures) > 1


def _intent_signature(case: IntentCase) -> tuple[str, ...]:
    semantic_frame = case.semantic_frame or {}
    intent = case.intent or {}
    return tuple(
        str(value)
        for value in (
            intent.get("task_type"),
            intent.get("operation"),
            semantic_frame.get("deployment_phase"),
            semantic_frame.get("machine_role"),
            semantic_frame.get("action_goal"),
        )
        if value
    )


def _safety_flag(case: IntentCase, key: str) -> bool:
    safety = case.safety or {}
    return safety.get(key) is True
