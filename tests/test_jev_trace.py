"""Redacted JSONL decision-trace tests (plan 05 §4.5 / §8 阶段 3)."""

from __future__ import annotations

import json

import pytest


def _decision():
    from klonet_agent.llm.decision import DecisionResult
    from klonet_agent.llm.decision_policy import decide_knowledge_route

    answers = {
        "scope": "klonet",
        "requires_retrieval": True,
        "requires_public_docs": True,
        "requires_source_code": False,
        "requires_environment_diagnosis": False,
        "clarification_required": False,
    }
    result = DecisionResult(
        answers=answers,
        confidences={key: 0.93 for key in answers},
        latency_seconds=0.42,
        provider="typesafe",
        model="jev-latest",
        version="1.13.0",
    )
    return result, decide_knowledge_route(
        result, request_text="Klonet 的拓扑部署流程是什么？", mode="shadow",
    )


def test_trace_event_carries_schema_route_and_reason_codes(tmp_path):
    from klonet_agent.tracing.jev_trace import JevTraceWriter, build_route_event

    result, decision = _decision()
    event = build_route_event(
        request_text="Klonet 的拓扑部署流程是什么？",
        route_decision=decision,
        result=result,
        mode="shadow",
        legacy_route="public_docs",
    )
    writer = JevTraceWriter(str(tmp_path / "jev_decisions.jsonl"))
    assert writer.write(event) is True

    lines = (tmp_path / "jev_decisions.jsonl").read_text(
        encoding="utf-8",
    ).splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])

    assert payload["event"] == "jev_route_decision"
    assert payload["schema_version"] == 1
    assert payload["request_hash"].startswith("sha256:")
    assert payload["mode"] == "shadow"
    assert payload["knowledge_route"] == "public_docs"
    assert payload["disposition"] == "constrain"
    assert payload["reason_codes"] == ["accepted", "shadow_only"]
    assert payload["confidences"]["requires_public_docs"] == 0.93
    assert payload["latency_seconds"] == 0.42
    assert payload["version"] == "1.13.0"
    assert payload["shadow_match"] is True
    assert payload["questions_schema"].startswith("sha256:")


def test_request_text_itself_is_never_written_verbatim(tmp_path):
    from klonet_agent.tracing.jev_trace import JevTraceWriter, build_route_event

    result, decision = _decision()
    secret_request = "我的 TYPESAFE_API_KEY=sk-live-abcdef123456 是不是配错了？"
    writer = JevTraceWriter(str(tmp_path / "t.jsonl"))
    writer.write(build_route_event(
        request_text=secret_request, route_decision=decision, result=result,
    ))

    raw = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "sk-live-abcdef123456" not in raw
    assert secret_request not in raw


@pytest.mark.parametrize(
    "sensitive",
    [
        "Authorization: Bearer sk-abcdef123456",
        "password=hunter2",
        "api_key: sk-sensitive-value",
        "token=abc123456",
    ],
)
def test_nested_secrets_are_redacted(tmp_path, sensitive):
    from klonet_agent.tracing.jev_trace import JevTraceWriter

    writer = JevTraceWriter(str(tmp_path / "s.jsonl"))
    writer.write({
        "event": "jev_route_decision",
        "answers": {"note": sensitive},
        "nested": {"authorization": "Bearer sk-nested-value"},
    })

    raw = (tmp_path / "s.jsonl").read_text(encoding="utf-8")
    for leaked in ("sk-abcdef123456", "hunter2", "sk-sensitive-value",
                   "abc123456", "sk-nested-value"):
        assert leaked not in raw
    assert "[REDACTED]" in raw


def test_write_failure_never_raises_into_the_main_path(tmp_path):
    from klonet_agent.tracing.jev_trace import JevTraceWriter

    class Logger:
        warnings = []

        def warning(self, message, *args):
            self.warnings.append(message % args)

    directory = tmp_path / "i_am_a_directory"
    directory.mkdir()
    logger = Logger()
    writer = JevTraceWriter(str(directory), logger=logger)

    assert writer.write({"event": "jev_route_decision"}) is False
    assert logger.warnings, "a single non-sensitive warning must be logged"
    assert "sk-" not in " ".join(logger.warnings)


def test_field_reason_map_reports_the_blocking_field():
    from klonet_agent.llm.decision import DecisionResult
    from klonet_agent.llm.decision_policy import decide_knowledge_route
    from klonet_agent.tracing.jev_trace import field_reason_map

    answers = {
        "scope": "klonet",
        "requires_retrieval": True,
        "requires_public_docs": False,
        "requires_source_code": False,
        "requires_environment_diagnosis": False,
        "clarification_required": False,
    }
    decision = decide_knowledge_route(
        DecisionResult(
            answers=answers, confidences={key: 0.95 for key in answers},
        ),
        request_text="Klonet 是什么？",
        mode="enforced",
    )

    assert decision.disposition == "fallback"
    reasons = field_reason_map(decision)
    assert reasons["requires_public_docs"] == "accepted"


def test_shadow_comparison_marks_mismatch_when_legacy_route_differs(tmp_path):
    from klonet_agent.tracing.jev_trace import JevTraceWriter, read_events

    result, decision = _decision()
    from klonet_agent.tracing.jev_trace import build_route_event

    path = str(tmp_path / "c.jsonl")
    writer = JevTraceWriter(path)
    writer.write(build_route_event(
        request_text="Klonet 部署", route_decision=decision, result=result,
        legacy_route="source_code",
    ))

    events = read_events(path)
    assert events[0]["shadow_match"] is False
    assert events[0]["legacy_route"] == "source_code"


def test_trace_path_defaults_to_config_value(monkeypatch):
    from klonet_agent.tracing import jev_trace

    monkeypatch.setattr(
        jev_trace._config, "JEV_TRACE_FILE", "tracing/custom.jsonl",
    )

    assert jev_trace.JevTraceWriter().path == "tracing/custom.jsonl"
