"""Typed decision-model contracts and the TypeSafe Jev HTTP transport."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import json
import os
import time
from typing import Any, Callable, Dict, Mapping
import urllib.request

from klonet_agent.tools.environment import redact_sensitive_text
from klonet_agent.config import (
    JEV_API_KEY_ENV,
    JEV_BASE_URL,
    JEV_ENABLED,
    JEV_GRAY_PERCENT,
    JEV_MODEL,
    JEV_TIMEOUT_SECONDS,
)


class DecisionModelError(RuntimeError):
    """A decision provider failed or returned an invalid typed result."""


@dataclass(frozen=True)
class DecisionResult:
    answers: Dict[str, Any]
    probabilities: Dict[str, Dict[str, float]] = field(default_factory=dict)
    confidences: Dict[str, float] = field(default_factory=dict)
    latency_seconds: float = 0.0
    input_tokens: int = 0
    estimated_cost_usd: float = 0.0
    provider: str = ""
    model: str = ""
    version: str = ""
    retried: bool = False
    fallback: bool = False


PrivilegedDecisionResult = DecisionResult


class DecisionModel(ABC):
    @abstractmethod
    def classify_turn(self, state: dict) -> DecisionResult:
        raise NotImplementedError

    @abstractmethod
    def classify_privileged_intent(self, state: dict) -> PrivilegedDecisionResult:
        raise NotImplementedError


def stable_gray_bucket(user_id: str, project_id: str) -> int:
    identity = "%s\0%s" % (str(user_id or ""), str(project_id or ""))
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


def in_gray_rollout(user_id: str, project_id: str, percent: int) -> bool:
    bounded = max(0, min(int(percent), 100))
    return bounded == 100 or (
        bounded > 0 and stable_gray_bucket(user_id, project_id) < bounded
    )


def configured_jev_decision_model(
    user_id: str,
    project_id: str,
) -> "JevDecisionModel | None":
    if not JEV_ENABLED or not in_gray_rollout(
        user_id, project_id, JEV_GRAY_PERCENT,
    ):
        return None
    api_key = os.getenv(JEV_API_KEY_ENV, "").strip()
    if not api_key:
        return None
    return JevDecisionModel(TypeSafeJevClient(
        api_key=api_key,
        base_url=JEV_BASE_URL,
        model=JEV_MODEL,
        timeout=JEV_TIMEOUT_SECONDS,
    ))


Transport = Callable[[str, Mapping[str, str], dict, float], dict]


class TypeSafeJevClient:
    """Small Python 3.8-compatible adapter for TypeSafe System One."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.typesafe.ai/v1/systemone",
        model: str = "jev-latest",
        timeout: float = 3.0,
        transport: Transport | None = None,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").strip()
        self.model = str(model or "jev-latest").strip()
        self.timeout = max(0.1, float(timeout))
        self._transport = transport or _http_transport

    def __repr__(self) -> str:
        return (
            "TypeSafeJevClient(base_url=%r, model=%r, api_key=<redacted>)"
            % (self.base_url, self.model)
        )

    def evaluate(self, *, state: dict, questions: dict) -> DecisionResult:
        if not self._api_key:
            raise DecisionModelError("TypeSafe API key is not configured")
        if not questions:
            raise DecisionModelError("at least one typed question is required")
        payload = {
            "model": self.model,
            "state": _redact_state(state),
            # System One accepts a question *map*.  Keep Klonet's compact
            # internal schema separate so callers and tests do not depend on
            # a vendor-specific spelling of Choice/Noul.
            "questions": _typesafe_questions(questions),
        }
        headers = {
            "Authorization": "Bearer %s" % self._api_key,
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            raw = self._transport(
                self.base_url, headers, payload, self.timeout,
            )
        except DecisionModelError:
            raise
        except Exception as exc:
            raise DecisionModelError(
                "TypeSafe request failed: %s" % type(exc).__name__
            ) from exc
        latency = time.monotonic() - started
        return _parse_result(raw, questions, self.model, latency)


class JevDecisionModel(DecisionModel):
    """Klonet's bounded question sets on top of a Jev evaluator."""

    def __init__(self, evaluator: TypeSafeJevClient) -> None:
        self.evaluator = evaluator

    def classify_turn(self, state: dict) -> DecisionResult:
        return self.evaluator.evaluate(state=state, questions=_TURN_QUESTIONS)

    def classify_privileged_intent(
        self, state: dict,
    ) -> PrivilegedDecisionResult:
        return self.evaluator.evaluate(
            state=state, questions=_PRIVILEGED_QUESTIONS,
        )


def _http_transport(
    url: str,
    headers: Mapping[str, str],
    payload: dict,
    timeout: float,
) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise DecisionModelError("TypeSafe response must be an object")
    return value


def _typesafe_questions(questions: Mapping[str, Mapping[str, Any]]) -> dict:
    """Translate Klonet's vendor-neutral decision schema to System One.

    Choice requires a labelled ``criteria`` map and Boolean is represented by
    TypeSafe's calibrated Noul primitive.  Descriptions intentionally contain
    no user state: sensitive input belongs only in the redacted ``state``.
    """
    translated = {}
    for name, question in questions.items():
        question_type = str(question.get("type") or "").lower()
        if question_type == "choice":
            options = list(question.get("options") or [])
            if not options:
                raise DecisionModelError("choice question %s has no options" % name)
            translated[name] = {
                "type": "choice",
                "instructions": str(question.get("instructions") or "Select the best %s classification." % name),
                "criteria": dict(question.get("criteria") or {str(option): str(option) for option in options}),
            }
        elif question_type == "boolean":
            translated[name] = {
                "type": "noul",
                "instructions": str(question.get("instructions") or "Is %s true for this state?" % name),
            }
        else:
            raise DecisionModelError("unsupported question type for %s" % name)
    return translated


def _parse_result(
    raw: Any,
    questions: dict,
    model: str,
    latency: float,
) -> DecisionResult:
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
        raise DecisionModelError("TypeSafe response is missing answers")
    raw_answers = raw["answers"]
    answers: Dict[str, Any] = {}
    probabilities: Dict[str, Dict[str, float]] = {}
    confidences: Dict[str, float] = {}
    metadata_confidence = raw.get("confidence")
    if not isinstance(metadata_confidence, dict):
        metadata_confidence = {}
    for name, question in questions.items():
        if name not in raw_answers:
            raise DecisionModelError("missing answer for %s" % name)
        answer = raw_answers[name]
        if isinstance(answer, dict):
            value = answer.get(
                "value", answer.get("answer", answer.get("choice")),
            )
            distribution = answer.get("probabilities")
            probability = answer.get("probability")
            noul_probability = answer.get("noul")
            inline_confidence = answer.get("confidence")
        else:
            value = answer
            distribution = None
            probability = None
            noul_probability = None
            inline_confidence = None
        question_type = str(question.get("type") or "").lower()
        if question_type == "choice":
            options = list(question.get("options") or [])
            if value not in options:
                raise DecisionModelError("invalid answer for %s" % name)
        elif question_type == "boolean":
            # TypeSafe Noul answers are probabilities rather than JSON
            # booleans.  A threshold of 0.5 only converts the typed result;
            # the caller still confidence-gates the decision separately.
            if isinstance(noul_probability, (int, float)):
                probability = float(noul_probability)
                if not 0.0 <= probability <= 1.0:
                    raise DecisionModelError("invalid Noul probability for %s" % name)
                value = probability >= 0.5
                distribution = {"true": probability, "false": 1.0 - probability}
                inline_confidence = max(probability, 1.0 - probability)
            elif not isinstance(value, bool) and isinstance(probability, (int, float)):
                value = float(probability) >= 0.5
            if not isinstance(value, bool):
                raise DecisionModelError("invalid boolean answer for %s" % name)
        answers[name] = value
        if isinstance(distribution, dict):
            probabilities[name] = {
                str(key): float(item) for key, item in distribution.items()
            }
        elif probability is not None:
            probabilities[name] = {str(value): float(probability)}
        confidence = metadata_confidence.get(name, inline_confidence)
        if confidence is None and probability is not None:
            confidence = probability
        if confidence is not None:
            confidences[name] = max(0.0, min(float(confidence), 1.0))
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    version = str(raw.get("version") or raw.get("model_version") or raw.get("model") or "")
    return DecisionResult(
        answers=answers,
        probabilities=probabilities,
        confidences=confidences,
        latency_seconds=latency,
        input_tokens=input_tokens,
        estimated_cost_usd=(input_tokens / 1_000_000.0) * 0.042,
        provider="typesafe",
        model=model,
        version=version,
    )


_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "password", "secret", "token",
}


_TURN_QUESTIONS = {
    "scope": {
        "type": "choice",
        "instructions": "Classify the request's knowledge scope.",
        "options": ["klonet", "general", "mixed"],
        "criteria": {"klonet": "Klonet platform, its deployment, source, configuration or operations", "general": "No substantive Klonet-specific request", "mixed": "Both Klonet-specific and unrelated requests"},
    },
    "task_type": {
        "type": "choice",
        "instructions": "Classify the user's task, not quoted history.",
        "options": [
            "concept", "deployment_preparation", "deployment_guidance",
            "credential_boundary", "operation_guide", "troubleshooting",
            "code_lookup", "development", "project_progress", "general",
        ],
        "criteria": {"concept": "asks what a Klonet concept, component or term means", "deployment_preparation": "prepares prerequisites before deployment", "deployment_guidance": "asks how to deploy or configure", "credential_boundary": "asks about credentials, permissions or secrets", "operation_guide": "asks for operational steps", "troubleshooting": "reports a failure and asks diagnosis", "code_lookup": "asks where or how code implements something", "development": "asks to change or develop code", "project_progress": "asks project status", "general": "general conversation"},
    },
    "operation": {
        "type": "choice",
        "instructions": "Choose only the operation currently requested.",
        "options": [
            "unknown", "environment_setup", "dependency_install",
            "platform_start", "platform_stop", "platform_restart",
            "platform_destroy", "acceptance_check",
        ],
    },
    "deployment_phase": {
        "type": "choice",
        "instructions": "Classify the current deployment or usage phase.",
        "options": [
            "local_tool_preparation", "environment_setup", "platform_startup",
            "platform_shutdown", "platform_restart", "topology_deploy",
            "platform_usage", "troubleshooting", "unknown",
        ],
    },
    "action_goal": {
        "type": "choice",
        "instructions": "Classify the user's current goal.",
        "options": [
            "prepare_tools", "install_dependencies", "start_services",
            "stop_services", "restart_services", "deploy_topology",
            "inspect_error", "use_feature", "explain_concept", "unknown",
        ],
    },
    "requires_retrieval": {
        "type": "boolean",
        "instructions": "Does the answer require Klonet evidence?",
    },
    "requires_environment_diagnosis": {
        "type": "boolean",
        "instructions": "Must current machine state be inspected?",
    },
    "clarification_required": {
        "type": "boolean",
        "instructions": "Is a material user choice missing and undiscoverable?",
    },
    "is_correction": {
        "type": "boolean",
        "instructions": "Is the user correcting the previous interpretation?",
    },
}


_PRIVILEGED_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "Classify actual execution intent; quoted or negated actions are not requests.",
        "options": [
            "conversation", "readonly_action", "mutating_action",
            "resume_plan", "ambiguous",
        ],
    },
    "goal_clarity": {
        "type": "choice",
        "instructions": "Can the goal be handled or safely discovered?",
        "options": ["clear", "discoverable", "missing"],
    },
    "goal_relation": {
        "type": "choice",
        "instructions": "Relate the current goal to recent conversation.",
        "options": [
            "new", "continue_previous", "refine_previous", "supersede_previous",
        ],
    },
    "goal_kind": {
        "type": "choice",
        "instructions": "Classify the requested outcome.",
        "options": ["conversation", "execution", "health_check", "causal_diagnosis"],
    },
    "operation": {
        "type": "choice",
        "instructions": "Choose only the operation currently requested.",
        "options": ["none", "restart", "repair", "start", "stop", "inspect"],
    },
    "scope": {
        "type": "choice",
        "instructions": "Classify the operation scope.",
        "options": ["none", "platform", "component"],
    },
    "requires_execution": {
        "type": "boolean",
        "instructions": "Does the request require reading or changing real machine state?",
    },
}


def _redact_state(value: Any, key: str = "") -> Any:
    if key.lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_state(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_state(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value
