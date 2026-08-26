"""LLM-backed query planning for the multi-stage Klonet retriever."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, Mapping, Protocol

from klonet_agent.config import (
    RAG_QUERY_PLANNER_MODEL, RAG_QUERY_PLANNER_TIMEOUT_SECONDS,
)
from klonet_agent.knowledge.conversation_state import ConversationState
from klonet_agent.knowledge.models import RetrievalPlan, RetrievalTask
from klonet_agent.llm.client import LLMClient


_VALID_STORES = {"public_docs", "source_code"}
_CODE_TOKEN_RE = re.compile(
    r"(?:/[A-Za-z0-9_.<>-]+){2,}/?"
    r"|(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
    r"|(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)"
    r"|(?:[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)"
    r"|(?:[A-Z][A-Z0-9_]{2,})"
    r"|(?:\b\d{3,5}\b)"
)
_MAX_QUERY_LENGTH = 500
_MAX_PURPOSE_LENGTH = 200

_SYSTEM_PROMPT = """\
你是 Klonet RAG 的检索规划器，不回答用户问题，只输出 JSON。
一次规划同时决定去哪个知识库、用什么查询检索，禁止输出独立 intent 分类。

知识库：
- public_docs：Klonet 概念、使用、部署、运维、排障经验和规范。
- source_code：Klonet/VEMU 的函数、API、配置、调用链和当前源码实现。

要求：
1. original query 会被系统始终保留；你只生成补充查询。
2. 口语、指代和省略可在 standalone_query 中补全，但不得改变否定条件。
3. 函数名、路径、配置键、错误码只能来自用户问题或给定上下文，不得虚构。
4. retrieval_tasks 为 1~2 个；复杂开发/排障问题通常同时检索两个库。
5. 每个任务的 keyword_queries 和 semantic_queries 各最多 2 条，短而聚焦。
6. exact_terms 只放需要字面匹配的原始标识符。

严格返回：
{
  "standalone_query": "string",
  "retrieval_tasks": [{
    "store": "public_docs|source_code",
    "purpose": "string",
    "keyword_queries": ["string"],
    "semantic_queries": ["string"],
    "exact_terms": ["string"]
  }],
  "excluded_terms": ["string"],
  "confidence": 0.0
}
"""


class PlannerClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any: ...


class QueryPlanner:
    """Generate and validate one retrieval plan per RAG invocation."""

    def __init__(self, client: PlannerClient | None = None):
        self.client = client if client is not None else _default_client()

    def plan(
        self,
        query: str,
        *,
        conversation_state: ConversationState | None = None,
        context_hints: Mapping[str, Any] | None = None,
    ) -> RetrievalPlan:
        original = (query or "").strip()
        if not original:
            return fallback_plan(original, detail="empty_query")
        if self.client is None:
            return fallback_plan(original, detail="missing_planner_credentials")

        context = _planner_context(conversation_state, context_hints)
        try:
            response = self.client.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"query": original, "context": context},
                            ensure_ascii=False,
                        ),
                    },
                ],
                reasoning_effort="low",
                temperature=0,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            payload = _response_payload(response)
            return validate_plan(original, payload, support_text=f"{original}\n{context}")
        except Exception as exc:
            return fallback_plan(
                original,
                detail=f"planner_error:{type(exc).__name__}",
            )


def validate_plan(
    original_query: str,
    payload: Mapping[str, Any],
    *,
    support_text: str | None = None,
) -> RetrievalPlan:
    """Validate untrusted model JSON and clamp its fan-out."""

    original = original_query.strip()
    standalone = _clean_text(payload.get("standalone_query"), _MAX_QUERY_LENGTH)
    if not standalone:
        standalone = original
    if _drops_explicit_constraint(original, standalone):
        standalone = original

    support = (support_text or original).casefold()
    protected = _protected_terms(support_text or original)
    raw_tasks = payload.get("retrieval_tasks")
    tasks: list[RetrievalTask] = []
    seen_stores: set[str] = set()
    if isinstance(raw_tasks, (list, tuple)):
        for raw_task in raw_tasks[:2]:
            if not isinstance(raw_task, Mapping):
                continue
            store = str(raw_task.get("store") or "").strip()
            if store not in _VALID_STORES or store in seen_stores:
                continue
            seen_stores.add(store)
            exact_terms = [
                term
                for term in _string_list(raw_task.get("exact_terms"), limit=12)
                if term.casefold() in support
            ]
            exact_terms = list(dict.fromkeys([*protected, *exact_terms]))
            tasks.append(
                RetrievalTask(
                    store=store,  # type: ignore[arg-type]
                    purpose=_clean_text(
                        raw_task.get("purpose"),
                        _MAX_PURPOSE_LENGTH,
                    )
                    or f"检索 {store}",
                    keyword_queries=_string_list(
                        raw_task.get("keyword_queries"),
                        limit=2,
                    ),
                    semantic_queries=_string_list(
                        raw_task.get("semantic_queries"),
                        limit=2,
                    ),
                    exact_terms=tuple(exact_terms[:12]),
                )
            )

    if not tasks:
        return fallback_plan(original, detail="invalid_or_empty_tasks")

    confidence = _clamp_confidence(payload.get("confidence"))
    excluded_terms = tuple(
        term
        for term in _string_list(payload.get("excluded_terms"), limit=12)
        if term.casefold() in support
    )
    return RetrievalPlan(
        original_query=original,
        standalone_query=standalone,
        retrieval_tasks=tuple(tasks),
        excluded_terms=excluded_terms,
        confidence=confidence,
        status="planned",
    )


def fallback_plan(query: str, *, detail: str) -> RetrievalPlan:
    """Conservative fail-open plan: search both stores with the original query."""

    original = (query or "").strip()
    exact_terms = _protected_terms(original)
    tasks = tuple(
        RetrievalTask(
            store=store,  # type: ignore[arg-type]
            purpose="Planner 不可用，使用原问题保守召回",
            exact_terms=exact_terms,
        )
        for store in ("public_docs", "source_code")
    )
    return RetrievalPlan(
        original_query=original,
        standalone_query=original,
        retrieval_tasks=tasks,
        confidence=0.0,
        status="fallback",
        detail=detail,
    )


def plan_as_dict(plan: RetrievalPlan) -> dict[str, Any]:
    """Serializable representation for trace and tests."""

    return asdict(plan)


def plan_from_mapping(
    query: str,
    value: Any,
    *,
    support_text: str | None = None,
) -> RetrievalPlan | None:
    """Deserialize an internally injected plan; return None when absent."""

    if not isinstance(value, Mapping):
        return None
    payload = dict(value)
    # ``asdict`` includes transport-only fields not consumed by validate_plan.
    if "retrieval_tasks" not in payload:
        return None
    plan = validate_plan(
        query,
        payload,
        support_text=support_text or query,
    )
    transported_status = str(value.get("status") or "").strip()
    transported_detail = str(value.get("detail") or "").strip()
    if transported_status in {"planned", "fallback"}:
        plan = RetrievalPlan(
            original_query=plan.original_query,
            standalone_query=plan.standalone_query,
            retrieval_tasks=plan.retrieval_tasks,
            excluded_terms=plan.excluded_terms,
            confidence=plan.confidence,
            status=transported_status,
            detail=transported_detail,
        )
    return plan


def _default_client() -> LLMClient | None:
    client = LLMClient(
        model=RAG_QUERY_PLANNER_MODEL,
        timeout=RAG_QUERY_PLANNER_TIMEOUT_SECONDS,
    )
    return client if client.has_credentials else None


def _response_payload(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        if "retrieval_tasks" in response:
            return response
        content = response.get("content")
    else:
        choices = getattr(response, "choices", None) or ()
        content = (
            getattr(getattr(choices[0], "message", None), "content", None)
            if choices
            else None
        )
    if isinstance(content, Mapping):
        return content
    if not isinstance(content, str):
        raise ValueError("planner response has no JSON content")
    decoded = json.loads(_strip_json_fence(content))
    if not isinstance(decoded, Mapping):
        raise ValueError("planner JSON must be an object")
    return decoded


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped


def _planner_context(
    state: ConversationState | None,
    hints: Mapping[str, Any] | None,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if state is not None:
        context["current_topic"] = state.current_topic
        context["machine_role"] = state.machine_role
        context["deployment_phase"] = state.deployment_phase
        context["confirmed_slots"] = state.confirmed_slots
        context["excluded_meanings"] = list(state.excluded_meanings)
    if hints:
        context["existing_structured_hints"] = {
            str(key): value for key, value in hints.items() if value not in (None, "", ())
        }
    return context


def _protected_terms(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0) for match in _CODE_TOKEN_RE.finditer(text)))


def _string_list(value: Any, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result = []
    for item in value:
        text = _clean_text(item, _MAX_QUERY_LENGTH)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip()


def _clamp_confidence(value: Any) -> float:
    try:
        return round(min(1.0, max(0.0, float(value))), 4)
    except (TypeError, ValueError):
        return 0.0


def _drops_explicit_constraint(original: str, rewritten: str) -> bool:
    markers = ("不需要", "不要", "不使用", "无需", "排除", "只看", "只查")
    return any(marker in original and marker not in rewritten for marker in markers)
