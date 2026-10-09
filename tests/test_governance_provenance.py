"""阶段 4/5 测试：证据与 provenance、能力注册与确定性路由（内存仓储）。"""

from __future__ import annotations

import pytest

from klonet_agent.memory.domain import Tenant
from klonet_agent.runtime.governance.capabilities import (
    ModelCapability,
    ModelCapabilityRegistry,
    RouteReasonCode,
    TaskRequirements,
    default_registry,
)
from klonet_agent.runtime.governance.provenance import (
    ClaimEvidenceRelation,
    ClaimStatus,
    EvidenceConflict,
    SourceType,
    Scope,
    claim_evidence_gate,
    content_hash,
    detect_conflict,
    is_stale,
    make_evidence,
    ops_evidence_to_governance,
    refresh_freshness,
)
from klonet_agent.runtime.governance.repository import InMemoryGovernanceRepository
from klonet_agent.runtime.governance.routing_policy import (
    POLICY_VERSION,
    decide_route,
    hard_filter,
    score_candidates,
)
from klonet_agent.runtime.governance.service import RuntimeGovernance

TENANT = Tenant(user_id="alice", project_id="demo")


def make_governance() -> tuple[RuntimeGovernance, InMemoryGovernanceRepository]:
    repo = InMemoryGovernanceRepository()
    governance = RuntimeGovernance(repo, TENANT)
    governance.start_run()
    return governance, repo


# --------------------------------------------------------------------------- #
# provenance 单元
# --------------------------------------------------------------------------- #


class TestProvenance:
    def test_content_hash_stable(self):
        assert content_hash("abc") == content_hash("abc")
        assert content_hash("abc") != content_hash("abd")
        assert content_hash({"b": 1, "a": 2}) == content_hash({"a": 2, "b": 1})

    def test_make_evidence_requires_fields_by_source(self):
        with pytest.raises(Exception):
            make_evidence(  # file 来源必须有 artifact_hash
                "file", run_id="r", user_id="u", project_id=None,
                subject="f", source_uri="/tmp/x",
            )
        ev = make_evidence(
            "file", run_id="r", user_id="u", project_id=None,
            subject="f", source_uri="/tmp/x", raw_content="hello",
        )
        assert ev.artifact_hash == content_hash("hello")

    def test_stale_by_source_hash_change(self):
        ev = make_evidence(
            "file", run_id="r", user_id="u", project_id=None,
            subject="f", source_uri="/tmp/x", raw_content="v1",
        )
        assert refresh_freshness(ev, current_source_hash=content_hash("v1")) == "fresh"
        # 源文件被修改：旧哈希证据必须被识别为 stale（计划验收）。
        assert refresh_freshness(ev, current_source_hash=content_hash("v2")) == "stale"
        assert is_stale(ev)

    def test_stale_by_ttl(self):
        from datetime import timedelta

        ev = make_evidence(
            "http", run_id="r", user_id="u", project_id=None,
            subject="page", source_uri="http://x", ttl_seconds=-1,
        )
        assert ev.valid_until is not None
        assert is_stale(ev)

    def test_conflict_detection_same_subject(self):
        ev1 = make_evidence(
            "tool", run_id="r", user_id="u", project_id=None,
            subject="run_command", observation="port 8080 free",
        )
        ev2 = make_evidence(
            "tool", run_id="r", user_id="u", project_id=None,
            subject="run_command", observation="port 8080 occupied",
        )
        conflict = detect_conflict(ev2, [ev1])
        assert isinstance(conflict, EvidenceConflict)
        # 同观察不算冲突（幂等/重复观察）
        ev3 = make_evidence(
            "tool", run_id="r", user_id="u", project_id=None,
            subject="run_command", observation="port 8080 free",
        )
        assert detect_conflict(ev3, [ev1]) is None
        # 空白差异不算冲突
        ev4 = make_evidence(
            "tool", run_id="r", user_id="u", project_id=None,
            subject="run_command", observation="port   8080  FREE",
        )
        assert detect_conflict(ev4, [ev1]) is None

    def test_evidence_gate(self):
        from klonet_agent.runtime.governance.provenance import ClaimEvidenceLink

        assert not claim_evidence_gate([])
        assert claim_evidence_gate([ClaimEvidenceLink("c", "e1")])
        assert not claim_evidence_gate(
            [ClaimEvidenceLink("c", "e1", relation=ClaimEvidenceRelation.CONTRADICTS)]
        )

    def test_ops_adapter(self):
        """Ops 领域证据通过 adapter 映射为通用证据，不反向覆盖 Ops 权威。"""

        class FakeRequest:
            command = "systemctl status nginx"

        class FakeOpsEvidence:
            request = FakeRequest()
            output = "active (running)"
            status = "available"
            collected_at = "2026-10-09T10:00:00+08:00"

        ev = ops_evidence_to_governance(
            FakeOpsEvidence(), run_id="r", user_id="u", project_id="demo"
        )
        assert ev.source_type == SourceType.OPS_PROBE
        assert "systemctl" in ev.source_uri
        assert ev.artifact_hash == content_hash("active (running)")
        assert ev.producer == "ops"


# --------------------------------------------------------------------------- #
# 路由策略（shadow）
# --------------------------------------------------------------------------- #


def _registry() -> ModelCapabilityRegistry:
    return ModelCapabilityRegistry(
        [
            ModelCapability(
                model_id="strong", version="v1", context_window=1_000_000,
                tool_calling=True, structured_output=True, vision=True,
                latency_ms_p50=3000, cost_per_1k_input=10.0, health=0.9,
                task_levels=frozenset({"standard", "critical"}),
            ),
            ModelCapability(
                model_id="cheap", version="v1", context_window=32768,
                tool_calling=True, structured_output=True, vision=False,
                latency_ms_p50=1000, cost_per_1k_input=1.0, health=0.8,
                task_levels=frozenset({"cheap", "standard"}),
            ),
            ModelCapability(
                model_id="no_tools", version="v1",
                tool_calling=False, structured_output=False, vision=False,
                latency_ms_p50=500, cost_per_1k_input=0.5, health=0.9,
                task_levels=frozenset({"cheap", "standard", "critical"}),
            ),
        ]
    )


class TestRoutingPolicy:
    def test_hard_filter_capability(self):
        feasible, excluded = hard_filter(
            _registry(), TaskRequirements(needs_tool_calling=True)
        )
        names = {m.model_id for m in feasible}
        assert "no_tools" not in names
        assert RouteReasonCode.EXCLUDED_CAPABILITY in excluded["no_tools"]

    def test_hard_filter_context_cost_latency_privacy(self):
        feasible, excluded = hard_filter(
            _registry(),
            TaskRequirements(min_context_tokens=100_000),
        )
        assert {m.model_id for m in feasible} == {"strong"}
        assert RouteReasonCode.EXCLUDED_CONTEXT in excluded["cheap"]

        feasible, _ = hard_filter(
            _registry(), TaskRequirements(max_cost_per_1k_input=2.0)
        )
        assert {m.model_id for m in feasible} == {"cheap", "no_tools"}

        feasible, _ = hard_filter(
            _registry(), TaskRequirements(max_latency_ms_p50=800)
        )
        assert {m.model_id for m in feasible} == {"no_tools"}

        # privacy 上限：sensitive 任务不允许 max_privacy_class=internal 的模型
        reg = _registry()
        reg._models[("cheap", "v1")].max_privacy_class = "public"
        feasible, excluded = hard_filter(
            reg, TaskRequirements(privacy_max_class="sensitive")
        )
        assert "cheap" not in {m.model_id for m in feasible}
        assert RouteReasonCode.EXCLUDED_PRIVACY in excluded["cheap"]

    def test_no_candidate_returns_reason(self):
        decision = decide_route(
            _registry(),
            TaskRequirements(task_level="critical", needs_vision=True),
            run_id="r", user_id="u", project_id=None,
        )
        # critical + vision：只有 strong 满足？strong 有 vision 且 critical → 有候选
        assert decision is not None and decision.selected == "strong"

        empty = ModelCapabilityRegistry([])
        decision = decide_route(
            empty, TaskRequirements(), run_id="r", user_id="u", project_id=None,
        )
        assert decision is not None
        assert decision.selected == ""
        assert RouteReasonCode.NO_CANDIDATE in decision.reason_codes

    def test_score_prefers_cheap_when_equal_quality(self):
        feasible, _ = hard_filter(
            _registry(), TaskRequirements(task_level="standard")
        )
        scores = score_candidates(feasible, TaskRequirements())
        assert scores["cheap"] > scores["strong"]  # 同质时成本优先

    def test_decision_is_explainable_and_shadow(self):
        decision = decide_route(
            _registry(),
            TaskRequirements(task_level="standard", needs_tool_calling=True),
            run_id="r", user_id="u", project_id=None,
            actual_model="strong",
        )
        assert decision.shadow is True
        assert decision.actual_model == "strong"
        assert decision.policy_version == POLICY_VERSION
        assert decision.reason_codes == [RouteReasonCode.SELECTED_BY_SCORE]
        assert decision.candidate_scores

    def test_default_registry_covers_deployed_models(self):
        registry = default_registry()
        assert registry.all()


# --------------------------------------------------------------------------- #
# 服务层证据/主张/路由
# --------------------------------------------------------------------------- #


class TestServiceEvidence:
    def test_record_evidence_persists(self):
        governance, repo = make_governance()
        result = governance.record_evidence(
            source_type="tool",
            subject="run_command",
            observation="port 8080 free",
            raw_content="port 8080 free",
        )
        evidence, claim = result
        assert claim is None
        assert repo.get_evidence(evidence.evidence_id) is not None
        assert evidence.artifact_hash == content_hash("port 8080 free")

    def test_conflict_creates_contradicted_claim(self):
        governance, repo = make_governance()
        ev1, _ = governance.record_evidence(
            source_type="tool", subject="run_command", observation="port 8080 free"
        )
        ev2, claim = governance.record_evidence(
            source_type="tool", subject="run_command", observation="port 8080 occupied"
        )
        assert claim is not None
        assert str(getattr(claim.status, "value", claim.status)) == "contradicted"
        links = repo.list_claim_links(claim.claim_id)
        assert {l.evidence_id for l in links} == {ev1.evidence_id, ev2.evidence_id}

    def test_claim_gate_blocks_unsupported(self):
        governance, _ = make_governance()
        claim = governance.create_claim(
            subject="x", statement="无证据主张", evidence_ids=[], status="supported"
        )
        assert not governance.evidence_sufficient(claim.claim_id)
        ev = governance.record_evidence(
            source_type="tool", subject="x", observation="obs"
        )[0]
        claim2 = governance.create_claim(
            subject="x", statement="有证据主张", evidence_ids=[ev.evidence_id]
        )
        assert governance.evidence_sufficient(claim2.claim_id)

    def test_mark_stale(self):
        governance, repo = make_governance()
        ev, _ = governance.record_evidence(
            source_type="file", subject="f", source_uri="/tmp/a.txt",
            raw_content="v1", observation="v1",
        )
        governance.mark_evidence_stale(ev.evidence_id, current_source_hash=content_hash("v2"))
        assert repo.get_evidence(ev.evidence_id).freshness == "stale"

    def test_route_decision_shadow_recorded(self):
        governance, repo = make_governance()
        decision = governance.record_route_decision(actual_model="strong")
        assert decision is not None
        assert decision.shadow is True
        assert len(repo.route_decisions) == 1
        assert repo.route_decisions[0].actual_model == "strong"
