"""TypeSafe provider-contract tests pinned to a real capture (plan §8 阶段 0.5).

The contract was captured from the live provider on 2026-10-11
(``evals/data/jev_provider_samples.json``, ``captured=true``).  These tests are
the regression pin: if the provider changes field names or stops returning a
Choice distribution, they fail and the margin rule must be re-decided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


SAMPLES = (
    Path(__file__).resolve().parents[1]
    / "evals" / "data" / "jev_provider_samples.json"
)


def _payload():
    return json.loads(SAMPLES.read_text(encoding="utf-8"))


def _sample(name):
    for sample in _payload()["samples"]:
        if sample["name"] == name:
            return sample
    raise AssertionError("missing provider sample %s" % name)


def _internal_questions(sample):
    """Klonet-side question schema for a recorded sample.

    The file stores the *translated* outbound payload (``criteria`` form), which
    cannot be fed back into ``evaluate()``; parsing uses the internal schema.
    """

    from klonet_agent.llm.jev_questions import (
        PRIVILEGED_QUESTIONS, TURN_QUESTIONS,
    )

    if sample["name"].startswith("privileged"):
        return PRIVILEGED_QUESTIONS
    return TURN_QUESTIONS


def _evaluate(sample):
    from klonet_agent.llm.decision import TypeSafeJevClient

    client = TypeSafeJevClient(
        api_key="probe-key", transport=lambda *_args: sample["response"],
    )
    return client.evaluate(
        state=sample["state"], questions=_internal_questions(sample),
    )


def test_recorded_outbound_payload_matches_the_translation_layer():
    from klonet_agent.llm.decision import _typesafe_questions

    for sample in _payload()["samples"]:
        assert sample["questions"] == _typesafe_questions(
            _internal_questions(sample)
        ), sample["name"]


def test_capture_is_present_and_complete():
    payload = _payload()

    assert payload["schema_version"] == 1
    assert payload["captured"] is True
    assert payload["captured_at"]
    assert payload["contract_questions"]
    assert len(payload["samples"]) >= 4
    assert payload["questions_schema_hash"].startswith("sha256:")


def test_contract_records_the_confirmed_field_names():
    contract = _payload()["contract"]

    assert contract["choice_returns_distribution"] is True
    assert contract["choice_value_field"] == "choice"
    assert contract["choice_confidence_field"] == "confidence"
    assert contract["boolean_probability_field"] == "noul"
    assert contract["answers_cover_all_questions"] is True
    assert contract["model_version"] == "jev-1.13.0"


def test_choice_answer_parses_value_distribution_and_confidence():
    sample = _sample("turn_docs_topology")

    result = _evaluate(sample)

    assert result.answers["scope"] == "klonet"
    assert len(result.probabilities["scope"]) == 3
    assert result.probabilities["scope"]["klonet"] == pytest.approx(1.0)
    assert result.confidences["scope"] == pytest.approx(1.0)
    assert result.version == "jev-1.13.0"


def test_noul_probability_becomes_selected_answer_with_max_p_confidence():
    """A near-tie must be recorded as a *low-confidence* boolean, not hidden."""

    sample = _sample("turn_source_port")
    result = _evaluate(sample)

    # observed noul: public_docs 0.39 → False, confidence = max(0.39, 0.61)
    assert result.answers["requires_public_docs"] is False
    assert result.confidences["requires_public_docs"] == pytest.approx(0.61)
    # observed noul: source_code 0.84 → True
    assert result.answers["requires_source_code"] is True
    assert result.confidences["requires_source_code"] == pytest.approx(0.84)


def test_general_request_is_cleanly_separated():
    sample = _sample("turn_general")
    result = _evaluate(sample)

    assert result.answers["scope"] == "general"
    assert result.answers["requires_retrieval"] is False
    assert result.confidences["requires_retrieval"] == pytest.approx(0.92)


def test_privileged_intent_choice_and_execution_noul():
    sample = _sample("privileged_restart")
    result = _evaluate(sample)

    assert result.answers["intent"] == "mutating_action"
    assert len(result.probabilities["intent"]) >= 4
    assert result.answers["requires_execution"] is True


def test_margin_is_actionable_because_the_provider_returns_distributions():
    from klonet_agent.llm.decision_policy import choice_margin

    sample = _sample("turn_docs_topology")
    result = _evaluate(sample)

    margin = choice_margin(result, "scope")
    assert margin is not None, "Choice 分布可得 → margin 规则可用"
    assert margin >= 0.0


def test_choice_without_distribution_is_still_handled_defensively():
    """Not observed live, but the parser must not crash if it ever happens."""

    from klonet_agent.llm.decision import TypeSafeJevClient
    from klonet_agent.llm.decision_policy import choice_margin

    client = TypeSafeJevClient(
        api_key="probe-key",
        transport=lambda *_args: {
            "answers": {"scope": {"type": "choice", "choice": "general",
                                  "confidence": 0.88}},
            "model": "jev-1.13.0",
        },
    )
    result = client.evaluate(
        state={"current_request": "hi"},
        questions={"scope": {"type": "choice",
                             "options": ["klonet", "general", "mixed"]}},
    )

    assert result.answers["scope"] == "general"
    assert result.confidences["scope"] == pytest.approx(0.88)
    assert choice_margin(result, "scope") is None
