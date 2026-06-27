"""Clarification and safety boundary decisions for Mentor turns."""

from __future__ import annotations

from dataclasses import dataclass

from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.models import QueryRoute


@dataclass(frozen=True)
class ClarificationDecision:
    """A pre-answer decision that can stop normal retrieval/answer generation."""

    should_stop: bool = False
    reply: str = ""
    reason: str = ""


_CREDENTIAL_TERMS = (
    "\u7528\u6237\u540d",
    "\u5bc6\u7801",
    "\u53e3\u4ee4",
    "\u8d26\u53f7",
    "\u8d26\u6237",
    "\u51ed\u636e",
    "token",
    "\u5bc6\u94a5",
    "secret",
    "\u771f\u5b9e ip",
    "\u670d\u52a1\u5668 ip",
)
_DEPLOY_TERMS = ("\u90e8\u7f72",)
_DEPLOY_SPECIFIC_TERMS = (
    "\u73af\u5883",
    "\u5b89\u88c5",
    "\u542f\u52a8",
    "\u8fd0\u884c",
    "\u524d\u7aef",
    "\u540e\u7aef",
    "worker",
    "master",
    "nginx",
    "redis",
    "\u505c\u6b62",
    "\u91cd\u542f",
    "\u62d3\u6251",
    "\u5b9e\u73b0",
    "topology",
    "topo",
)


def decide_pre_llm_clarification(
    user_input: str,
    route: QueryRoute | None = None,
) -> ClarificationDecision:
    """Return a deterministic pre-LLM clarification/safety decision."""

    normalized = user_input.strip().lower()
    if _looks_like_credential_question(normalized):
        return ClarificationDecision(
            should_stop=True,
            reason="credential_boundary",
            reply=(
                "\u6211\u4e0d\u80fd\u5728\u516c\u5f00\u56de\u7b54\u91cc\u590d\u8ff0\u6216\u4fdd\u5b58"
                "\u660e\u6587\u8d26\u53f7\u3001\u5bc6\u7801\u3001token \u6216\u771f\u5b9e IP\u3002"
                "\u8bf7\u4ece\u5185\u90e8\u51ed\u636e\u6587\u6863\u3001\u5b9e\u9a8c\u8bf4\u660e"
                "\u6216\u7ba1\u7406\u5458\u5904\u786e\u8ba4\uff1b\u5982\u679c\u9700\u8981\u8bb0\u5f55\uff0c"
                "\u53ea\u4f7f\u7528 <vm_user>/<vm_password> \u8fd9\u7c7b\u5360\u4f4d\u7b26\u3002"
            ),
        )

    if route is not None and route.confidence < 0.45 and route.scope != "general":
        return ClarificationDecision(
            should_stop=True,
            reason="low_route_confidence",
            reply="\u6211\u8fd8\u4e0d\u786e\u5b9a\u4f60\u8981\u89e3\u51b3\u7684\u662f\u54ea\u4e00\u7c7b\u95ee\u9898\uff0c\u80fd\u5426\u7528\u4e00\u53e5\u8bdd\u8bf4\u660e\u76ee\u6807\u548c\u5f53\u524d\u524d\u63d0\uff1f",
        )

    return ClarificationDecision()


def decide_model_intent_clarification(
    intent: QueryIntent,
    *,
    user_input: str = "",
    recent_history: list[dict] | None = None,
) -> ClarificationDecision:
    """Honor high-signal clarification fields from the structured model intent."""

    if _context_resolves_reference(user_input, recent_history or []):
        return ClarificationDecision()

    if _context_resolves_late_supplement(user_input, recent_history or []):
        return ClarificationDecision()

    if intent.clarification_required:
        question = intent.clarification_question or (
            "\u8fd9\u4e2a\u95ee\u9898\u7684\u610f\u56fe\u8fd8\u4e0d\u591f\u660e\u786e\uff0c"
            "\u80fd\u5426\u5148\u8865\u5145\u4f60\u60f3\u8981\u7684\u76ee\u6807\uff1f"
        )
        return ClarificationDecision(
            should_stop=True,
            reason="model_requested_clarification",
            reply=question,
        )

    if intent.confidence and intent.confidence < 0.5:
        return ClarificationDecision(
            should_stop=True,
            reason="low_intent_confidence",
            reply="\u6211\u8fd8\u4e0d\u786e\u5b9a\u4f60\u8981\u7684\u662f\u54ea\u4e00\u7c7b\u7ed3\u679c\uff0c\u80fd\u5426\u5148\u8865\u5145\u4e00\u4e0b\u5177\u4f53\u76ee\u6807\uff1f",
        )

    return ClarificationDecision()


def _looks_like_credential_question(text: str) -> bool:
    return any(term in text for term in _CREDENTIAL_TERMS)


def _looks_like_ambiguous_klonet_deploy(text: str, route: QueryRoute | None) -> bool:
    if route is not None and route.hard_disable_rag:
        return False
    if "klonet" not in text.lower():
        return False
    if not any(term in text for term in _DEPLOY_TERMS):
        return False
    return not any(term in text for term in _DEPLOY_SPECIFIC_TERMS)


def _context_resolves_reference(user_input: str, recent_history: list[dict]) -> bool:
    text = user_input.strip().lower()
    reference_terms = (
        "\u7b2c\u4e00\u79cd",
        "\u7b2c\u4e8c\u79cd",
        "\u573a\u666f\u4e00",
        "\u573a\u666f\u4e8c",
        "\u4e0a\u9762\u90a3\u4e2a",
        "\u4f60\u8bf4\u7684",
        "\u521a\u8bf4\u7684",
        "\u7ee7\u7eed",
    )
    if not any(term in text for term in reference_terms):
        return False

    recent_assistant_text = "\n".join(
        str(message.get("content") or "")
        for message in recent_history[-6:]
        if message.get("role") == "assistant"
    ).lower()
    if not recent_assistant_text:
        return False

    option_markers = (
        "\u573a\u666f\u4e00",
        "\u573a\u666f\u4e8c",
        "\u7b2c\u4e00\u79cd",
        "\u7b2c\u4e8c\u79cd",
        "\u4e00\uff1a",
        "\u4e8c\uff1a",
    )
    return any(marker in recent_assistant_text for marker in option_markers)


def _context_resolves_late_supplement(user_input: str, recent_history: list[dict]) -> bool:
    text = user_input.strip().lower().replace(" ", "")
    if text not in {"klonet", "klonet平台", "平台", "这个平台", "那个平台"}:
        return False

    recent_text = "\n".join(
        str(message.get("content") or "")
        for message in recent_history[-6:]
        if message.get("role") in {"user", "assistant"}
    ).lower()
    if not recent_text:
        return False

    usage_signals = ("使用平台", "浏览器", "普通用户", "场景一", "电脑里需要下载")
    contrast_signals = ("场景二", "部署运行", "管理员", "开发者")
    return any(term in recent_text for term in usage_signals) and any(
        term in recent_text for term in contrast_signals
    )
