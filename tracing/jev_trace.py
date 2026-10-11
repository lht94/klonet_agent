"""Redacted JSONL decision events for Jev knowledge routing (plan 05).

One line per Jev attempt.  This is **knowledge-store routing** telemetry, not
the governance layer's model-selection routing
(``runtime/governance/routing_policy.py``); the two must not be conflated.

Hard rules:
- never write API keys, Authorization headers, passwords or raw environment;
- a write failure must never break the conversation (log once, keep going).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from klonet_agent import config as _config
from klonet_agent.tools.environment import redact_sensitive_text


SCHEMA_VERSION = 1
EVENT_NAME = "jev_route_decision"

_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "password", "secret", "token",
    "cookie", "private_key",
}

_logger = logging.getLogger("klonet_agent.jev_trace")


def request_hash(text: str) -> str:
    """Stable, non-reversible digest of the user input."""

    return "sha256:" + hashlib.sha256(
        str(text or "").encode("utf-8")
    ).hexdigest()


class JevTraceWriter:
    """Append-only JSONL writer that never raises into the main path."""

    def __init__(self, path: Optional[str] = None, *, logger=None):
        self.path = str(path or getattr(
            _config, "JEV_TRACE_FILE", "tracing/jev_decisions.jsonl",
        ))
        self._logger = logger or _logger
        self._lock = threading.Lock()
        self._warned = False

    def write(self, event: Mapping[str, Any]) -> bool:
        """Append one sanitized event.  Returns ``True`` on success."""

        payload = dict(event or {})
        payload.setdefault("event", EVENT_NAME)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        sanitized = sanitize_event(payload)
        try:
            directory = Path(self.path).parent
            if str(directory) and not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
            line = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            return True
        except Exception as exc:
            # Observability must never block the conversation.
            if not self._warned:
                self._warned = True
                self._logger.warning(
                    "jev trace write failed (%s); routing continues",
                    type(exc).__name__,
                )
            return False


def sanitize_event(value: Any, key: str = "") -> Any:
    """Recursively redact sensitive keys and secret shapes."""

    if str(key).lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_event(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_event(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def build_route_event(
    *,
    request_text: str,
    route_decision: Any,
    result: Any = None,
    mode: str = "shadow",
    legacy_route: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Compose the trace payload for one knowledge-route decision."""

    event = {
        "event": EVENT_NAME,
        "schema_version": SCHEMA_VERSION,
        "request_hash": request_hash(request_text),
        "mode": str(mode),
        "provider": str(getattr(result, "provider", "") or ""),
        "model": str(getattr(result, "model", "") or ""),
        "version": str(getattr(result, "version", "") or ""),
        "questions_schema": _questions_schema_hash(),
        "answers": dict(getattr(result, "answers", None) or {}),
        "confidences": dict(getattr(result, "confidences", None) or {}),
        "knowledge_route": str(
            getattr(route_decision, "knowledge_route", "legacy")
        ),
        "disposition": str(getattr(route_decision, "disposition", "fallback")),
        "reason_codes": list(getattr(route_decision, "reason_codes", ()) or ()),
        "margin_available": bool(
            getattr(route_decision, "margin_available", True)
        ),
        "fallback": not bool(getattr(route_decision, "adopted", False)),
        "latency_seconds": float(
            getattr(result, "latency_seconds", 0.0) or 0.0
        ),
        "legacy_route": legacy_route,
        "shadow_match": (
            None if legacy_route is None
            else str(getattr(route_decision, "knowledge_route", "legacy"))
            == str(legacy_route)
        ),
    }
    if extra:
        for key, value in extra.items():
            event.setdefault(str(key), value)
    return event


def field_reason_map(route_decision: Any) -> dict:
    """``{field: reason_code}`` for reporting which field blocked adoption."""

    return {
        str(item.name): str(item.reason_code)
        for item in (getattr(route_decision, "field_decisions", ()) or ())
    }


def _questions_schema_hash() -> str:
    try:
        from klonet_agent.llm.jev_questions import questions_schema_hash

        return questions_schema_hash()
    except Exception:
        return ""


def read_events(path: str) -> Iterable[dict]:
    """Read a trace file back (used by tests and reports)."""

    file_path = Path(path)
    if not file_path.exists():
        return []
    events = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events
