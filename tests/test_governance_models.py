"""治理层单元测试：状态机、隐私网关、服务事务边界与导出。

这些用例不依赖数据库（内存仓储），对应计划 §4.7 的 L1 合约测试里
"状态机、幂等、隐私准入"三条。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from klonet_agent.memory.domain import Tenant
from klonet_agent.runtime.governance.exporters import JsonlEventExporter
from klonet_agent.runtime.governance.models import (
    FailureStatus,
    InvalidStatusTransitionError,
    PrivacyClass,
    RuntimeEvent,
    TaskStatus,
    ToolOutcome,
    ensure_failure_transition,
    ensure_step_transition,
    ensure_task_transition,
)
from klonet_agent.runtime.governance.privacy import (
    PrivacyGateway,
    SecretRejectedError,
    detect,
    redact_text,
)
from klonet_agent.runtime.governance.repository import (
    ConcurrentProjectionError,
    DuplicateEventError,
    InMemoryGovernanceRepository,
)
from klonet_agent.runtime.governance.service import (
    GovernanceUnavailableError,
    RuntimeGovernance,
)

TENANT = Tenant(user_id="alice", project_id="demo")


def make_governance(**kwargs) -> tuple[RuntimeGovernance, InMemoryGovernanceRepository]:
    repo = InMemoryGovernanceRepository()
    governance = RuntimeGovernance(repo, TENANT, **kwargs)
    governance.start_run()
    return governance, repo


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #


class TestStateMachines:
    def test_task_happy_path(self):
        ensure_task_transition("pending", "running")
        ensure_task_transition("running", "blocked")
        ensure_task_transition("blocked", "running")
        ensure_task_transition("running", "completed")

    def test_task_illegal_transitions_rejected(self):
        with pytest.raises(InvalidStatusTransitionError):
            ensure_task_transition("completed", "running")
        with pytest.raises(InvalidStatusTransitionError):
            ensure_task_transition("pending", "completed")  # 必须先 running
        with pytest.raises(InvalidStatusTransitionError):
            ensure_task_transition("cancelled", "running")

    def test_step_transitions(self):
        ensure_step_transition("pending", "running")
        ensure_step_transition("running", "failed")
        with pytest.raises(InvalidStatusTransitionError):
            ensure_step_transition("pending", "succeeded")
        with pytest.raises(InvalidStatusTransitionError):
            ensure_step_transition("failed", "running")  # 失败重开属于重规划

    def test_failure_close_requires_explicit_status(self):
        ensure_failure_transition("active", "resolved")
        ensure_failure_transition("resolved", "verified")
        with pytest.raises(InvalidStatusTransitionError):
            ensure_failure_transition("verified", "active")

    def test_invalid_status_string_rejected_loudly(self):
        with pytest.raises(ValueError):
            ensure_task_transition("pending", "flying")


# --------------------------------------------------------------------------- #
# 隐私网关
# --------------------------------------------------------------------------- #


class TestPrivacyGateway:
    def test_detect_secret_classes(self):
        assert detect("my key is sk-abcdefghijklmnop1234") == PrivacyClass.SECRET
        assert detect("-----BEGIN RSA PRIVATE KEY-----") == PrivacyClass.SECRET
        assert detect("password=hunter22secret") == PrivacyClass.SECRET
        assert detect("AKIAIOSFODNN7EXAMPLE") == PrivacyClass.SECRET

    def test_detect_sensitive_and_internal(self):
        assert detect("服务器 192.168.1.33 连不上") == PrivacyClass.SENSITIVE
        assert detect("普通的项目描述，没有特殊内容") == PrivacyClass.INTERNAL

    def test_redact_text_removes_secrets(self):
        text = "key is sk-abcdefghijklmnop1234 for 192.168.1.33"
        sanitized, records = redact_text(text)
        assert "sk-abcdefghijklmnop1234" not in sanitized
        assert "192.168.1.33" not in sanitized
        assert "[REDACTED:" in sanitized
        categories = {r.category for r in records}
        assert "api_key" in categories
        assert "network_address" in categories

    def test_redaction_records_never_carry_original(self):
        secret = "sk-abcdefghijklmnop1234"
        _, records = redact_text(f"token {secret}")
        dumped = json.dumps([r.__dict__ for r in records], ensure_ascii=False, default=str)
        assert secret not in dumped

    def test_admit_payload_rejects_secret(self):
        gateway = PrivacyGateway()
        with pytest.raises(SecretRejectedError):
            gateway.admit_payload({"note": "use sk-abcdefghijklmnop1234 please"})

    def test_admit_payload_redacts_sensitive(self):
        gateway = PrivacyGateway()
        sanitized, records = gateway.admit_payload(
            {"note": "host 10.0.0.42 down", "nested": {"cmd": "ping 10.0.0.42"}}
        )
        assert "10.0.0.42" not in json.dumps(sanitized)
        assert records

    def test_secret_rejection_carries_no_original(self):
        gateway = PrivacyGateway()
        secret = "Bearer abcdefghijklmnopqrstuvwx"
        try:
            gateway.admit_payload({"auth": secret})
        except SecretRejectedError as exc:
            assert secret not in str(exc)
        else:  # pragma: no cover
            pytest.fail("secret 未被拒绝")


# --------------------------------------------------------------------------- #
# 服务层
# --------------------------------------------------------------------------- #


class TestRuntimeGovernance:
    def test_run_and_turn_lifecycle(self):
        governance, repo = make_governance()
        governance.start_turn()
        assert governance.run_id
        assert governance.turn_id
        types = [e.event_type for e in repo.list_events(governance.run_id)]
        assert "run.started" in types
        assert "turn.started" in types

    def test_model_call_telemetry(self):
        governance, repo = make_governance()
        governance.record_model_call(model="test-model", total_tokens=120, duration_ms=33)
        assert repo.model_calls[0].total_tokens == 120
        assert repo.count_events(governance.run_id) == 2  # run.started + model_call

    def test_tool_call_and_unknown_outcome(self):
        governance, repo = make_governance()
        governance.record_tool_call(tool_name="run_command", outcome=ToolOutcome.OUTCOME_UNKNOWN)
        assert repo.tool_calls[0].outcome == ToolOutcome.OUTCOME_UNKNOWN
        governance.record_tool_call(tool_name="read_file", outcome="succeeded")
        assert repo.tool_calls[1].outcome == ToolOutcome.SUCCEEDED

    def test_apply_todos_creates_authoritative_tasks(self):
        governance, repo = make_governance()
        summary = governance.apply_todos(
            [
                {"id": 1, "content": "环境检查", "status": "in_progress"},
                {"id": 2, "content": "部署服务", "status": "pending"},
            ]
        )
        assert "created=2" in summary
        tasks = repo.list_tasks(governance.run_id)
        assert [str(getattr(t.status, "value", t.status)) for t in tasks] == ["running", "pending"]

    def test_todos_transition_enforced(self):
        governance, repo = make_governance()
        governance.apply_todos([{"id": 1, "content": "任务A", "status": "completed"}])
        # 模型试图把已完成任务改回 pending：非法转换必须被拒绝。
        with pytest.raises(InvalidStatusTransitionError):
            governance.apply_todos([{"id": 1, "content": "任务A", "status": "pending"}])
        # 投影保持 completed（不允许被新文本覆盖）。
        task = repo.list_tasks(governance.run_id)[0]
        assert str(getattr(task.status, "value", task.status)) == "completed"

    def test_apply_todos_idempotent(self):
        governance, repo = make_governance()
        todos = [{"id": 1, "content": "任务A", "status": "pending"}]
        governance.apply_todos(todos)
        before = repo.count_events(governance.run_id)
        governance.apply_todos(todos)
        assert repo.count_events(governance.run_id) == before  # 幂等：无新事件

    def test_todos_waiting_user_maps_to_blocked(self):
        governance, repo = make_governance()
        governance.apply_todos(
            [{"id": 1, "content": "等用户确认", "status": "waiting_user"}]
        )
        task = repo.list_tasks(governance.run_id)[0]
        assert str(getattr(task.status, "value", task.status)) == "blocked"
        assert task.blocked_reason == "waiting_user"

    def test_failure_lifecycle_requires_evidence_for_verified(self):
        governance, repo = make_governance()
        failure = governance.record_failure(
            stage="tool_execution", error_class="ConnectionError", message="db down"
        )
        assert repo.list_active_failures(governance.run_id)

        with pytest.raises(InvalidStatusTransitionError):
            governance.close_failure(failure.failure_id, to_status="verified")

        governance.close_failure(
            failure.failure_id,
            to_status="resolved",
            resolution_action="重启连接池",
        )
        governance.close_failure(
            failure.failure_id,
            to_status="verified",
            verification_evidence_ids=["ev-1"],
        )
        assert str(getattr(repo.get_failure(failure.failure_id).status, "value", "")) == "verified"
        # verified 失败生成经验候选，但不直接写入记忆。
        assert len(governance.lesson_candidates) == 1
        lesson = governance.lesson_candidates[0]
        assert lesson.failure_id == failure.failure_id
        assert lesson.scope_user_id == "alice"

    def test_failure_resolved_requires_action(self):
        governance, _ = make_governance()
        failure = governance.record_failure(stage="llm", error_class="Timeout")
        with pytest.raises(InvalidStatusTransitionError):
            governance.close_failure(failure.failure_id, to_status="resolved")

    def test_same_step_same_class_failure_deduplicated_by_key(self):
        governance, repo = make_governance()
        first = governance.record_failure(stage="tool", error_class="TimeoutX")
        second = governance.record_failure(stage="tool", error_class="TimeoutX")
        # 幂等键相同 → 第二次打开时事件幂等命中（无新事件），失败记录不翻倍。
        assert first.idempotency_key == second.idempotency_key
        assert len(repo.list_active_failures(governance.run_id)) == 1

    def test_secret_in_payload_becomes_tombstone(self):
        governance, repo = make_governance()
        governance.record_failure(
            stage="tool_execution",
            error_class="AuthError",
            message="invalid key sk-abcdefghijklmnop1234",
        )
        dumped = json.dumps(
            [e.payload for e in repo.all_events()], ensure_ascii=False
        )
        assert "sk-abcdefghijklmnop1234" not in dumped
        # tombstone 事件进入了账本（不含原文）。
        assert any(
            e.payload.get("rejected") == "secret_detected"
            for e in repo.all_events()
        )

    def test_optimistic_lock_conflict_detected(self):
        governance, repo = make_governance()
        governance.apply_todos([{"id": 1, "content": "A", "status": "pending"}])
        task = repo.list_tasks(governance.run_id)[0]
        # 用过期版本提交转换 → 乐观锁冲突。
        with pytest.raises(ConcurrentProjectionError):
            governance.transition_task(
                task.task_id,
                "running",
                reason_code="task.started",
                expected_version=task.version - 1,
            )

    def test_telemetry_buffered_when_repository_down(self):
        class FlakyRepo(InMemoryGovernanceRepository):
            fail_model_calls = True

            def record_model_call(self, record, event):  # noqa: D102
                if self.fail_model_calls:
                    raise ConnectionError("db down")
                return super().record_model_call(record, event)

        repo = FlakyRepo()
        governance = RuntimeGovernance(repo, TENANT)
        governance.start_run()
        governance.record_model_call(model="m", total_tokens=1, duration_ms=1)
        assert not repo.model_calls  # 落库失败
        assert len(governance._telemetry_buffer) == 1
        repo.fail_model_calls = False
        assert governance.flush_telemetry() == 1
        assert repo.model_calls  # 补投成功（幂等键保证不重复）

    def test_state_change_fails_closed_when_repository_down(self):
        class DeadRepo(InMemoryGovernanceRepository):
            def append_event(self, event, **kwargs):
                raise ConnectionError("db down")

        governance = RuntimeGovernance(DeadRepo(), TENANT)
        with pytest.raises(GovernanceUnavailableError):
            governance.start_run()
        # start_run 失败后 run 未建立，apply_todos 同样 fail closed。
        with pytest.raises(GovernanceUnavailableError):
            governance.apply_todos([{"id": 1, "content": "A", "status": "pending"}])


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


class TestJsonlExporter:
    def test_export_is_append_only_jsonl(self, tmp_path):
        path = tmp_path / "governance.jsonl"
        exporter = JsonlEventExporter(path)
        governance = RuntimeGovernance(
            InMemoryGovernanceRepository(), TENANT, exporters=[exporter]
        )
        governance.start_run()
        governance.record_model_call(model="m")
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        rows = [json.loads(line) for line in lines]
        assert rows[0]["event"] == "run.started"
        assert rows[1]["event"] == "model_call.completed"
        assert rows[1]["payload"]["model"] == "m"
        assert rows[1]["run_id"] == governance.run_id

    def test_export_failure_never_breaks_authority(self, tmp_path):
        class BrokenExporter:
            def export(self, event):
                raise OSError("disk full")

        governance = RuntimeGovernance(
            InMemoryGovernanceRepository(), TENANT, exporters=[BrokenExporter()]
        )
        governance.start_run()  # 不抛异常即合格
        assert governance.run_id
