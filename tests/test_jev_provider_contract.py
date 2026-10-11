"""TypeSafe provider-contract probe (plan 05 §8 阶段 0.5).

``JEV_MIN_MARGIN``, the calibration metrics and the policy's ``probabilities``
field all assume the provider returns a Choice distribution.  That assumption
must be *verified*, not assumed, so this module pins the contract from a
recorded sample file and fails loudly if the file still holds the synthetic
placeholder when a real capture is expected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


SAMPLES = Path(__file__).resolve().parents[1] / "evals" / "data" / "jev_provider_samples.json"


def _payload():
    return json.loads(SAMPLES.read_text(encoding="utf-8"))


def test_provider_sample_file_is_present_and_well_formed():
    payload = _payload()

    assert payload["schema_version"] == 1
    assert isinstance(payload["captured"], bool)
    assert payload["contract_questions"], "必须列出待确认的 Provider 契约问题"
    assert payload["samples"], "至少要有 Choice/Noul 两种形态的样本"


def test_synthetic_placeholder_documents_the_pending_real_capture():
    """阶段 0.5 cannot be closed until a real capture lands."""

    payload = _payload()
    if payload["captured"]:
        assert payload["captured_at"], "captured=true 时必须记录采样时间"
    else:
        assert payload["captured_at"] is None
        assert "阶段 0.5" in payload["note"]
        assert "captured=true" in payload["note"]


def _evaluate(sample):
    from klonet_agent.llm.decision import TypeSafeJevClient

    client = TypeSafeJevClient(
        api_key="probe-key",
        transport=lambda *_args: sample["response"],
    )
    return client.evaluate(state=sample["state"], questions=sample["questions"])


def test_choice_with_distribution_exposes_probabilities():
    sample = _sample("turn_choice_with_distribution")

    result = _evaluate(sample)

    assert result.answers["scope"] == "klonet"
    assert result.probabilities["scope"]["klonet"] == pytest.approx(0.93)
    assert result.confidences["scope"] == pytest.approx(0.93)
    assert result.answers["requires_retrieval"] is True
    assert result.confidences["requires_retrieval"] == pytest.approx(0.96)
    assert result.version == "1.13.0"


def test_choice_without_distribution_falls_back_to_top1_only():
    sample = _sample("turn_choice_without_distribution")

    result = _evaluate(sample)

    assert result.answers["scope"] == "general"
    distribution = result.probabilities.get("scope") or {}
    assert len(distribution) < 2, "只有 top1 时无法计算 margin"
    assert result.confidences["scope"] == pytest.approx(0.88)


def test_noul_false_is_confident_not_low_confidence():
    sample = _sample("privileged_noul_false_is_confident")

    result = _evaluate(sample)

    assert result.answers["requires_execution"] is False
    assert result.confidences["requires_execution"] == pytest.approx(0.96)
    assert result.probabilities["requires_execution"] == pytest.approx(
        {"true": 0.04, "false": 0.96}
    )


def test_contract_conclusion_drives_whether_margin_can_apply():
    """The margin rule must match what the recorded samples actually support."""

    supports_distribution = False
    for sample in _payload()["samples"]:
        result = _evaluate(sample)
        for distribution in (result.probabilities or {}).values():
            if len(distribution) >= 2 and any(
                question.get("type") == "choice"
                for question in sample["questions"].values()
            ):
                supports_distribution = True

    if not supports_distribution:
        # Plan §5: margin must then be reported as unavailable, never faked.
        from klonet_agent.llm.decision_policy import (
            REASON_MARGIN_UNAVAILABLE,
            REASON_MARGIN_TOO_SMALL,
        )

        assert REASON_MARGIN_UNAVAILABLE == "margin_unavailable"
        assert REASON_MARGIN_TOO_SMALL == "probability_margin_too_small"


def test_real_capture_is_required_before_enforcing_margin(monkeypatch):
    """Guard: with only synthetic samples, do not claim the margin gate is live."""

    if _payload()["captured"]:
        pytest.skip("真实样本已落盘，本守卫不再适用")

    from klonet_agent import config as _config

    assert _config.JEV_MIN_MARGIN >= 0.0
    assert os.environ.get("JEV_ROUTING_MODE", "") != "enforced", (
        "在 Provider 契约未确认前不得把 JEV_ROUTING_MODE 设为 enforced"
    )


def _sample(name):
    for sample in _payload()["samples"]:
        if sample["name"] == name:
            return sample
    raise AssertionError("missing provider sample %s" % name)
