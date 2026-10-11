"""Versioned Choice/Noul question sets for the TypeSafe Jev decision model.

Kept separate from :mod:`klonet_agent.llm.decision` so that the vendor HTTP
transport, the business questions and the policy thresholds do not live in one
file.  Changing any question wording changes the calibrated probability
distribution, so the schema carries an explicit version and the prompt hash is
recorded in traces.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping


# Bump whenever question text / options change: calibration data is only valid
# for one question schema.
QUESTIONS_SCHEMA_VERSION = 2

_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_CHARS = 800
_MAX_SUMMARY_CHARS = 400


TURN_QUESTIONS: Dict[str, Dict[str, Any]] = {
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
    # Two independent Noul rather than one 4-way Choice: public documentation and
    # source code are not mutually exclusive, and both probabilities must be
    # calibratable independently.
    "requires_public_docs": {
        "type": "boolean",
        "instructions": (
            "Does answering the current request require Klonet concepts, "
            "usage, deployment, operations, troubleshooting guidance or "
            "other public documentation evidence?"
        ),
    },
    "requires_source_code": {
        "type": "boolean",
        "instructions": (
            "Does answering the current request require Klonet/VEMU source "
            "implementation, symbols, APIs, configuration definitions or "
            "call-chain evidence?"
        ),
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


PRIVILEGED_QUESTIONS: Dict[str, Dict[str, Any]] = {
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


def questions_schema_hash() -> str:
    """Stable hash of both question sets, recorded in traces for replay."""

    payload = json.dumps(
        {"version": QUESTIONS_SCHEMA_VERSION, "turn": TURN_QUESTIONS,
         "privileged": PRIVILEGED_QUESTIONS},
        sort_keys=True,
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_turn_state(
    user_input: str,
    recent_history: List[Mapping[str, Any]] | None = None,
    *,
    last_assistant_intent_summary: str = "",
    mode: str = "mentor",
) -> dict:
    """Explicit, bounded state for the turn path.

    Named fields instead of one vague ``request`` blob so the two call sites
    (mentor turn path and privileged ops path) send the same schema.
    """

    return {
        "current_request": str(user_input or ""),
        "recent_user_turns": _bounded_turns(recent_history or []),
        "last_assistant_intent_summary": str(
            last_assistant_intent_summary or ""
        )[:_MAX_SUMMARY_CHARS],
        "mode": str(mode or "mentor"),
    }


def build_privileged_state(
    user_input: str,
    recent_history: List[Mapping[str, Any]] | None = None,
    *,
    last_assistant_intent_summary: str = "",
    mode: str = "ops",
) -> dict:
    """Same schema as :func:`build_turn_state` for the privileged path."""

    return build_turn_state(
        user_input,
        recent_history,
        last_assistant_intent_summary=last_assistant_intent_summary,
        mode=mode,
    )


def _bounded_turns(history: List[Mapping[str, Any]]) -> List[dict]:
    result = []
    for message in list(history)[-_MAX_HISTORY_TURNS:]:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        result.append({
            "role": role,
            "content": str(message.get("content") or "")[:_MAX_HISTORY_CHARS],
        })
    return result
