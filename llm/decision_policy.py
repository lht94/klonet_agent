"""Calibrated, field-level policy that turns a Jev result into a route.

This module never performs I/O: it only consumes a provider-neutral
:class:`~klonet_agent.llm.decision.DecisionResult` and produces a project-level
``JevRouteDecision`` with per-field thresholds, margin checks, cross-field
consistency rules and explicit reason codes.

Boundary: ``knowledge/`` must not import this module's transport, and this
module must not import anything from ``knowledge/`` at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from klonet_agent.config import (
    JEV_MIN_MARGIN,
    JEV_THRESHOLD_CLARIFICATION,
    JEV_THRESHOLD_ENV_DIAGNOSIS,
    JEV_THRESHOLD_PRIVILEGED_EXECUTION,
    JEV_THRESHOLD_PRIVILEGED_INTENT,
    JEV_THRESHOLD_PUBLIC_DOCS,
    JEV_THRESHOLD_RETRIEVAL,
    JEV_THRESHOLD_SCOPE,
    JEV_THRESHOLD_SOURCE_CODE,
)
from klonet_agent.llm.jev_questions import (
    PRIVILEGED_QUESTIONS,
    TURN_QUESTIONS,
)


# Margin (top1 - top2) is a *Choice* concept; boolean Noul answers already
# encode confidence as ``max(p, 1-p)`` and must not be margin-checked.
_FIELD_TYPE: Dict[str, str] = {
    name: str(question.get("type") or "").lower()
    for name, question in {
        **TURN_QUESTIONS, **PRIVILEGED_QUESTIONS,
    }.items()
}


def _is_choice_field(name: str) -> bool:
    return _FIELD_TYPE.get(name) == "choice"



# --- fixed vocabularies ---------------------------------------------------

KNOWLEDGE_ROUTES = (
    "no_retrieval", "public_docs", "source_code", "both", "legacy",
)
DISPOSITIONS = ("accept", "constrain", "fallback")

REASON_ACCEPTED = "accepted"
REASON_PROVIDER_ERROR = "provider_error"
REASON_MISSING_ANSWER = "missing_answer"
REASON_INVALID_ANSWER = "invalid_answer"
REASON_BELOW_FIELD_THRESHOLD = "below_field_threshold"
REASON_MARGIN_TOO_SMALL = "probability_margin_too_small"
REASON_MARGIN_UNAVAILABLE = "margin_unavailable"
REASON_RETRIEVAL_STORE_CONFLICT = "retrieval_store_conflict"
REASON_GENERAL_SCOPE_CONFLICT = "general_scope_conflict"
REASON_DETERMINISTIC_RULE_CONFLICT = "deterministic_rule_conflict"
REASON_SHADOW_ONLY = "shadow_only"
REASON_PLANNER_VIOLATION = "planner_store_violation"

# Reasons that describe *why* a decision was not adopted, grouped the way the
# report has to present them (plan §6.3).
FALLBACK_REASON_GROUP = {
    REASON_PROVIDER_ERROR: "provider_fallback",
    REASON_MISSING_ANSWER: "provider_fallback",
    REASON_INVALID_ANSWER: "provider_fallback",
    REASON_BELOW_FIELD_THRESHOLD: "confidence_fallback",
    REASON_MARGIN_TOO_SMALL: "confidence_fallback",
    REASON_RETRIEVAL_STORE_CONFLICT: "consistency_fallback",
    REASON_GENERAL_SCOPE_CONFLICT: "consistency_fallback",
    REASON_DETERMINISTIC_RULE_CONFLICT: "rule_fallback",
    REASON_PLANNER_VIOLATION: "planner_fallback",
    REASON_SHADOW_ONLY: "shadow_ignored",
}


def fallback_group(reason_code: str) -> str:
    """Map a reason code to the report group (§6.3)."""

    return FALLBACK_REASON_GROUP.get(str(reason_code), "provider_fallback")


_SCOPE_OPTIONS = ("klonet", "general", "mixed")

# Only these fields actually decide the knowledge route, so only they may veto
# it.  ``clarification_required`` and ``requires_environment_diagnosis`` are
# *informational*: their low confidence must never block store routing — the
# 2026-10-11 real sweep showed them rejecting 110/120 and 99/120 cases while
# changing nothing about which store is right.
ROUTE_GATING_FIELDS = (
    "scope",
    "requires_retrieval",
    "requires_public_docs",
    "requires_source_code",
)
INFORMATIONAL_FIELDS = (
    "requires_environment_diagnosis",
    "clarification_required",
)

# Deterministic overrides (plan §6.1 rules 7–8).  Kept deliberately narrow:
# they can only *veto* a Jev answer, never create one.
_SOURCE_CODE_REQUEST_RE = re.compile(
    r"(源码|源代码|代码实现|实现原理|调用链|函数定义|函数签名|在哪实现|哪一行|"
    r"source\s*code|call\s*chain|implementation\s+of)",
    re.IGNORECASE,
)
_SOURCE_CODE_NEGATION_RE = re.compile(
    r"(不要|不用|无需|别)\s*(看|查|检索|搜索)?\s*(源码|源代码|代码)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FieldDecision:
    """One field's threshold + margin verdict, with its reason code."""

    name: str
    value: Any
    confidence: float
    threshold: float
    accepted: bool
    reason_code: str
    gating: bool = True


@dataclass(frozen=True)
class JevDecisionThresholds:
    """Per-field acceptance thresholds (plan §3.3)."""

    scope: float = JEV_THRESHOLD_SCOPE
    requires_retrieval: float = JEV_THRESHOLD_RETRIEVAL
    requires_public_docs: float = JEV_THRESHOLD_PUBLIC_DOCS
    requires_source_code: float = JEV_THRESHOLD_SOURCE_CODE
    requires_environment_diagnosis: float = JEV_THRESHOLD_ENV_DIAGNOSIS
    clarification_required: float = JEV_THRESHOLD_CLARIFICATION
    privileged_intent: float = JEV_THRESHOLD_PRIVILEGED_INTENT
    privileged_execution: float = JEV_THRESHOLD_PRIVILEGED_EXECUTION
    min_margin: float = JEV_MIN_MARGIN

    def for_field(self, name: str) -> float:
        return float(getattr(self, name, 0.0))


@dataclass(frozen=True)
class JevRouteDecision:
    """Executable policy result for one Jev evaluation."""

    disposition: str
    knowledge_route: str
    accepted_answers: Dict[str, Any] = field(default_factory=dict)
    field_decisions: Tuple[FieldDecision, ...] = ()
    reason_codes: Tuple[str, ...] = ()
    margin_available: bool = True
    applied_mode: str = "enforced"

    @property
    def adopted(self) -> bool:
        return self.disposition in {"accept", "constrain"}

    @property
    def allows_llm_skip(self) -> bool:
        return self.disposition == "accept"

    @property
    def allowed_stores(self) -> Tuple[str, ...]:
        if not self.adopted:
            return ()
        return STORES_FOR_ROUTE[self.knowledge_route]

    def to_constraints(self):
        """Build the provider-neutral retrieval constraint for this route."""

        from klonet_agent.knowledge.models import RetrievalConstraints

        if not self.adopted:
            return RetrievalConstraints(source="legacy")
        return RetrievalConstraints(
            allowed_stores=self.allowed_stores,
            requires_retrieval=self.knowledge_route != "no_retrieval",
            source="jev",
            confidence=_min_confidence(self.field_decisions),
            reason_codes=self.reason_codes,
        )


STORES_FOR_ROUTE = {
    "no_retrieval": (),
    "public_docs": ("public_docs",),
    "source_code": ("source_code",),
    "both": ("public_docs", "source_code"),
    "legacy": (),
}


# --- public entry points --------------------------------------------------

def decide_knowledge_route(
    result: Any,
    *,
    thresholds: Optional[JevDecisionThresholds] = None,
    request_text: str = "",
    privileged: bool = False,
    mode: str = "enforced",
) -> JevRouteDecision:
    """Turn a ``DecisionResult`` into a route decision.

    ``mode`` is a runtime mode (``off``/``shadow``/``canary``/``enforced``), not
    a disposition: in ``shadow`` the computed route is still reported but the
    decision is flagged ``shadow_only`` so callers keep the legacy path.
    """

    limits = thresholds or JevDecisionThresholds()
    if result is None:
        return _fallback((REASON_PROVIDER_ERROR,), limits, mode)
    if bool(getattr(result, "fallback", False)):
        return _fallback((REASON_PROVIDER_ERROR,), limits, mode)

    answers = dict(getattr(result, "answers", None) or {})
    if privileged:
        return _decide_privileged(result, answers, limits, mode)

    required = (
        "scope",
        "requires_retrieval",
        "requires_public_docs",
        "requires_source_code",
        "requires_environment_diagnosis",
        "clarification_required",
    )
    missing = [name for name in required if name not in answers]
    if missing:
        return _fallback((REASON_MISSING_ANSWER,), limits, mode, missing=missing)
    invalid = [name for name in required if not _valid_value(name, answers[name])]
    if invalid:
        return _fallback((REASON_INVALID_ANSWER,), limits, mode, invalid=invalid)

    margin_available = True
    decisions = []
    rejected = []
    for name in required:
        gating = name in ROUTE_GATING_FIELDS
        decision = _decide_field(
            result, name, answers[name], limits.for_field(name),
            min_margin=limits.min_margin,
            check_margin=_is_choice_field(name),
            gating=gating,
        )
        decisions.append(decision)
        if decision.reason_code == REASON_MARGIN_UNAVAILABLE:
            margin_available = False
        if not decision.accepted and decision.gating:
            rejected.append(decision)
    if rejected:
        reasons = _unique_ordered(d.reason_code for d in rejected)
        return _fallback(
            tuple(reasons), limits, mode, decisions=decisions,
            margin_available=margin_available,
        )

    accepted = {name: answers[name] for name in required}
    requires_retrieval = bool(accepted["requires_retrieval"])
    public = bool(accepted["requires_public_docs"])
    source = bool(accepted["requires_source_code"])
    route = _compose_route(requires_retrieval, public, source)

    conflict = _consistency_conflict(
        accepted["scope"], requires_retrieval, public, source,
    )
    if conflict:
        return _fallback(
            (conflict,), limits, mode, decisions=decisions,
            margin_available=margin_available,
        )

    rule_conflict = deterministic_rule_conflict(
        request_text, public=public, source=source,
    )
    if rule_conflict:
        return _fallback(
            (REASON_DETERMINISTIC_RULE_CONFLICT,), limits, mode,
            decisions=decisions, margin_available=margin_available,
        )

    reason_codes = (REASON_ACCEPTED,)
    disposition = "accept" if (
        route == "no_retrieval" and accepted["scope"] == "general"
    ) else "constrain"
    if mode == "shadow":
        reason_codes = reason_codes + (REASON_SHADOW_ONLY,)
    return JevRouteDecision(
        disposition=disposition,
        knowledge_route=route,
        accepted_answers=accepted,
        field_decisions=tuple(decisions),
        reason_codes=reason_codes,
        margin_available=margin_available,
        applied_mode=mode,
    )


def _decide_privileged(
    result: Any,
    answers: Mapping[str, Any],
    limits: JevDecisionThresholds,
    mode: str,
) -> JevRouteDecision:
    """Reuse the same policy vocabulary for the privileged classifier.

    v0.5 deliberately does **not** widen Jev's authority on this path: the
    result only feeds reason codes/traces; plan/approval semantics stay where
    they are.  A missing/ambiguous intent therefore fails closed to ``fallback``.
    """

    required = ("intent", "requires_execution")
    missing = [name for name in required if name not in answers]
    if missing:
        return _fallback((REASON_MISSING_ANSWER,), limits, mode, missing=missing)

    decisions = []
    for name in required:
        threshold = (
            limits.privileged_intent if name == "intent"
            else limits.privileged_execution
        )
        decision = _decide_field(
            result, name, answers[name], threshold,
            min_margin=limits.min_margin,
            check_margin=_is_choice_field(name),
        )
        decisions.append(decision)
    rejected = [d for d in decisions if not d.accepted]
    if rejected:
        return _fallback(
            tuple(_unique_ordered(d.reason_code for d in rejected)),
            limits, mode, decisions=decisions,
        )

    intent = str(answers["intent"])
    requires_execution = bool(answers["requires_execution"])
    if intent == "ambiguous":
        return _fallback(
            (REASON_INVALID_ANSWER, "ambiguous_intent"), limits, mode,
            decisions=decisions,
        )
    if intent in {"readonly_action", "mutating_action", "resume_plan"} and (
        not requires_execution
    ):
        # A mutating/reading intent that claims no execution is self-contradictory.
        return _fallback(
            (REASON_DETERMINISTIC_RULE_CONFLICT,), limits, mode,
            decisions=decisions,
        )

    reason_codes = (REASON_ACCEPTED,)
    if mode == "shadow":
        reason_codes = reason_codes + (REASON_SHADOW_ONLY,)
    return JevRouteDecision(
        disposition="constrain",
        knowledge_route="legacy",
        accepted_answers={
            name: answers[name] for name in required
        },
        field_decisions=tuple(decisions),
        reason_codes=reason_codes,
        applied_mode=mode,
    )


# --- deterministic rules --------------------------------------------------

def deterministic_rule_conflict(
    request_text: str,
    *,
    public: bool,
    source: bool,
) -> Optional[str]:
    """Return a conflict marker when Jev contradicts explicit user wording.

    Rules (plan §6.1 items 7–8):
    - an explicit request for code location/implementation must not be accepted
      with ``requires_source_code=False``;
    - an explicit "don't look at the source" must not be accepted when the only
      routed store is ``source_code``.
    """

    text = str(request_text or "")
    if not text:
        return None
    if _SOURCE_CODE_NEGATION_RE.search(text) and source and not public:
        return "negated_source_only"
    if _SOURCE_CODE_REQUEST_RE.search(text) and not source:
        return "explicit_source_request"
    return None


# --- helpers --------------------------------------------------------------

def _decide_field(
    result: Any,
    name: str,
    value: Any,
    threshold: float,
    *,
    min_margin: float,
    check_margin: bool,
    gating: bool = True,
) -> FieldDecision:
    confidence = field_confidence(result, name, value)
    if confidence < threshold:
        return FieldDecision(
            name=name, value=value, confidence=confidence,
            threshold=threshold, accepted=False,
            reason_code=REASON_BELOW_FIELD_THRESHOLD, gating=gating,
        )
    if check_margin:
        margin = choice_margin(result, name)
        if margin is None:
            # Provider did not return a distribution: the margin rule cannot
            # apply.  Recorded explicitly so reports never claim a margin
            # rejection that never happened (plan §5 / §9).
            return FieldDecision(
                name=name, value=value, confidence=confidence,
                threshold=threshold, accepted=True,
                reason_code=REASON_MARGIN_UNAVAILABLE, gating=gating,
            )
        if margin < min_margin:
            return FieldDecision(
                name=name, value=value, confidence=confidence,
                threshold=threshold, accepted=False,
                reason_code=REASON_MARGIN_TOO_SMALL, gating=gating,
            )
    return FieldDecision(
        name=name, value=value, confidence=confidence, threshold=threshold,
        accepted=True, reason_code=REASON_ACCEPTED, gating=gating,
    )


def field_confidence(result: Any, name: str, value: Any) -> float:
    """Selected-answer confidence.

    Noul answers are probabilities, so the reported confidence must always be
    ``max(p, 1-p)`` for the *chosen* boolean (plan §10, first risk row).
    """

    confidences = getattr(result, "confidences", None) or {}
    if name in confidences:
        try:
            return max(0.0, min(float(confidences[name]), 1.0))
        except (TypeError, ValueError):
            pass
    distribution = (getattr(result, "probabilities", None) or {}).get(name) or {}
    if distribution:
        try:
            return max(0.0, min(max(float(v) for v in distribution.values()), 1.0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def choice_margin(result: Any, name: str) -> Optional[float]:
    """``top1 - top2`` for a Choice field, or ``None`` when unavailable."""

    distribution = (getattr(result, "probabilities", None) or {}).get(name)
    if not isinstance(distribution, Mapping) or len(distribution) < 2:
        return None
    try:
        values = sorted(
            (float(v) for v in distribution.values()), reverse=True,
        )
    except (TypeError, ValueError):
        return None
    return values[0] - values[1]


def _compose_route(requires_retrieval: bool, public: bool, source: bool) -> str:
    if not requires_retrieval:
        return "no_retrieval"
    if public and source:
        return "both"
    if public:
        return "public_docs"
    if source:
        return "source_code"
    return "no_retrieval"


def _consistency_conflict(
    scope: Any,
    requires_retrieval: bool,
    public: bool,
    source: bool,
) -> Optional[str]:
    if not requires_retrieval and (public or source):
        return REASON_RETRIEVAL_STORE_CONFLICT
    if requires_retrieval and not (public or source):
        return REASON_RETRIEVAL_STORE_CONFLICT
    if str(scope) == "general" and (public or source):
        return REASON_GENERAL_SCOPE_CONFLICT
    return None


def _valid_value(name: str, value: Any) -> bool:
    if name == "scope":
        return str(value) in _SCOPE_OPTIONS
    if name == "intent":
        return str(value) in {
            "conversation", "readonly_action", "mutating_action",
            "resume_plan", "ambiguous",
        }
    return isinstance(value, bool)


def _min_confidence(decisions: Tuple[FieldDecision, ...]) -> float:
    values = [d.confidence for d in decisions]
    return min(values) if values else 0.0


def _unique_ordered(values) -> list:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _fallback(
    reason_codes: Tuple[str, ...],
    limits: JevDecisionThresholds,
    mode: str,
    *,
    decisions: Tuple[FieldDecision, ...] = (),
    margin_available: bool = True,
    missing: Any = None,
    invalid: Any = None,
) -> JevRouteDecision:
    codes = tuple(_unique_ordered(reason_codes)) or (REASON_PROVIDER_ERROR,)
    if mode == "shadow" and REASON_SHADOW_ONLY not in codes:
        codes = codes + (REASON_SHADOW_ONLY,)
    return JevRouteDecision(
        disposition="fallback",
        knowledge_route="legacy",
        accepted_answers={},
        field_decisions=tuple(decisions),
        reason_codes=codes,
        margin_available=margin_available,
        applied_mode=mode,
    )
