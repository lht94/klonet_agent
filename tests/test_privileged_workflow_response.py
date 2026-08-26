from __future__ import annotations

from types import SimpleNamespace

from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion
from klonet_agent.ops.privileged.workflow.response import ResponseAgent


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                )
            ]
        )


def test_readonly_response_preserves_model_layout_with_one_call():
    llm = FakeLLM("\n结论：已发现平台。\n\n1. 平台 A\n2. 平台 B\n")

    result = ResponseAgent(llm).render_readonly(
        "检查平台",
        EvidenceConclusion(),
    )

    assert result == "结论：已发现平台。\n\n1. 平台 A\n2. 平台 B"
    assert len(llm.calls) == 1
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "终端展示器" in system_prompt
    assert "列表项分别换行" in system_prompt
    assert "不得新增事实、证据缺口、后续检查、建议命令" in system_prompt


def test_response_agent_receives_frozen_knowledge_evidence():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    llm = FakeLLM("已回答")
    bundle = EvidenceBundle(goal="检查平台")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("klonet_knowledge", {"query": "检查平台"}, "知识"),
        "source=startup_shutdown.md\n后端健康以 /server_health/ 为准",
    ))

    ResponseAgent(llm).render_readonly(
        "检查平台", EvidenceConclusion(), evidence_bundle=bundle,
    )

    prompt = llm.calls[0]["messages"][1]["content"]
    assert "startup_shutdown.md" in prompt
    assert "/server_health/" in prompt


def test_plan_response_reconciles_history_with_persisted_plan_facts():
    llm = FakeLLM(
        "你讨论过修订，但没有生成新的正式 Plan ID；当前仍是旧计划。"
    )

    result = ResponseAgent(llm).render_plan_turn(
        "我之前不是修订过这个计划了吗",
        conversation_context="用户指出环境、Screen 验收和语义矛盾",
        plan_context=(
            "active_plan_id=priv-ops-old\n"
            "persisted_plan_count=1\n"
            "plan_id=priv-ops-old status=blocked contract=invalid"
        ),
        fallback="fallback",
    )

    assert "没有生成新的正式 Plan ID" in result
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "环境、Screen 验收和语义矛盾" in prompt
    assert "status=blocked" in prompt
    system = llm.calls[0]["messages"][0]["content"]
    assert "只有新的持久化 Plan ID" in system
    assert "不要因为出现‘计划’就机械复述计划" in system


def _failure_record(*, missing_decisions=None):
    from klonet_agent.ops.privileged.workflow.contracts import (
        FailureRecord, RecoveryOption,
    )

    return FailureRecord(
        failure_id="failure-response-test",
        stage="binding",
        category="binding_replan_unresolved",
        summary="实施计划没有形成完整动作。",
        technical_reason="runtime_recovery_action_missing=worker",
        failed_checks=["worker 缺少启动或重启动作"],
        attempted_recoveries=["首次绑定", "局部 Replan"],
        options=[
            RecoveryOption("continue", "继续处理", "继续", "continue_current_goal"),
            RecoveryOption("direction", "调整范围", "调整", "provide_direction"),
            RecoveryOption("cancel", "取消", "取消", "cancel"),
        ],
        goal="收编 test worker",
        goal_kind="execution",
        missing_decisions=list(missing_decisions or []),
    )


def test_failure_response_llm_only_explains_persisted_facts():
    llm = FakeLLM(
        "计划要求恢复 worker，但实施计划没有生成对应动作。系统已经尝试"
        "局部重规划；当前记录没有要求用户补充条件。"
    )
    failure = _failure_record()

    result = ResponseAgent(llm).render_failure(failure, fallback="fallback")

    assert "没有生成对应动作" in result
    assert "1." not in result
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "runtime_recovery_action_missing=worker" in prompt
    assert "worker 缺少启动或重启动作" in prompt
    assert "用户未决事项：无" in prompt
    system = llm.calls[0]["messages"][0]["content"]
    assert "不得生成恢复选项" in system
    assert "missing_decisions 为空" in system


def test_failure_response_rejects_model_generated_recovery_menu():
    failure = _failure_record()
    llm = FakeLLM("1. 继续处理\n2. 调整范围\n3. 取消")

    result = ResponseAgent(llm).render_failure(failure, fallback="safe fallback")

    assert result == "safe fallback"


def test_failure_response_rejects_invented_user_boundary():
    failure = _failure_record()
    llm = FakeLLM("需要你补充 worker 端口和运行权限。")

    result = ResponseAgent(llm).render_failure(failure, fallback="safe fallback")

    assert result == "safe fallback"


def test_failure_response_can_phrase_an_authoritative_missing_decision():
    decision = "是否允许停止并重启健康的 worker"
    failure = _failure_record(missing_decisions=[decision])
    llm = FakeLLM("实施计划仍缺少用户决定：%s。" % decision)

    result = ResponseAgent(llm).render_failure(failure, fallback="fallback")

    assert decision in result


def test_runtime_inventory_response_preserves_project_root_identity():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    bundle = EvidenceBundle(goal="检查有多少正常运行的平台")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        "\n".join([
            "runtime_candidate_count=2",
            "healthy_count=1",
            "abnormal_count=1",
            "code_only_count=1",
            "platform=vemu project_root=/srv/formal backend_status=healthy roles=master,worker master_port=45551 master_endpoint=healthy worker_port=45552 worker_endpoint=healthy",
            "platform=vemu project_root=/srv/test backend_status=abnormal roles=master,worker master_port=45554 master_endpoint=healthy worker_port=45555 worker_endpoint=unreachable",
            "code_only_root=/srv/code-only",
        ]),
    ))

    result = ResponseAgent(FakeLLM("should not be used")).render_readonly(
        "检查有多少正常运行的平台", EvidenceConclusion(), evidence_bundle=bundle,
    )

    assert "1 个正常，1 个异常" in result
    assert "项目目录：`/srv/formal`" in result
    assert "运行异常" in result
    assert "项目目录：`/srv/test`" in result
    assert "仅发现代码、没有后端运行证据" in result
    assert "/srv/code-only" in result


def test_response_rejects_new_probe_commands_after_goal_is_achieved():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceClaim

    llm = FakeLLM(
        "已确认两个平台。\n\n建议补充的只读命令：\n```bash\nsudo ss -lntp\n```"
    )
    conclusion = EvidenceConclusion(confirmed_facts=[EvidenceClaim(
        "已确认两个平台", ["ev-platforms"],
    )])

    result = ResponseAgent(llm).render_readonly("查看平台", conclusion)

    assert result == "已确认：已确认两个平台"
    assert "sudo" not in result
