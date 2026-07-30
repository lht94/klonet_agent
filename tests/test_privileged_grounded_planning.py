from __future__ import annotations

import json
from types import SimpleNamespace
import pytest


class FakeLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.payloads.pop(0))
                )
            ]
        )


def _action_payload(action="validate_project_files", args=None):
    if args is None:
        args = {"project_root": "/srv/klonet/demo_project"}
    return json.dumps(
        {
            "goal": "部署 Klonet 平台",
            "steps": [
                {
                    "step_id": "precheck",
                    "title": "校验项目文件",
                    "action": action,
                    "args": args,
                }
            ],
        },
        ensure_ascii=False,
    )


def test_context_builder_collects_knowledge_environment_and_action_catalog():
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    calls = []
    builder = PrivilegedPlanContextBuilder(
        knowledge_search=lambda query, **kwargs: (
            calls.append(("knowledge", query, kwargs)) or "部署知识证据"
        ),
        environment_inspector=lambda args: (
            calls.append(("environment", args)) or "服务器只读证据"
        ),
    )

    context = builder.build("帮我部署平台")

    assert calls[0][0] == "knowledge"
    assert calls[0][2]["task_type"] == "deployment"
    assert calls[1][0] == "environment"
    assert "部署知识证据" in context.render()
    assert "服务器只读证据" in context.render()
    assert "action=validate_project_files" not in context.action_catalog
    assert "category=" in context.action_catalog
    assert "Execution Agent" in context.action_catalog
    assert "one-time shell artifact" in context.action_catalog


def test_recovery_diagnostics_execute_only_allowlisted_readonly_probes(monkeypatch):
    from klonet_agent.ops.privileged import context as context_module
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    calls = []
    monkeypatch.setitem(
        context_module._RECOVERY_PROBES,
        "process_detail",
        lambda args: calls.append(args) or "port_owner=service-a",
    )
    builder = PrivilegedPlanContextBuilder(
        knowledge_search=lambda *args, **kwargs: "knowledge",
        environment_inspector=lambda args: "environment",
    )

    evidence = builder.run_recovery_diagnostics(
        [
            {
                "probe": "process_detail",
                "args": {"ports": [8080]},
                "purpose": "确认端口占用者",
            },
            {"probe": "mutate_host", "args": {}, "purpose": "不允许"},
        ]
    )

    assert calls == [{"ports": [8080]}]
    assert "port_owner=service-a" in evidence
    assert "probe_not_allowlisted" in evidence


def test_grounded_context_blocks_planning_when_klonet_evidence_is_missing():
    from klonet_agent.ops.privileged.context import GroundedPlanContext

    context = GroundedPlanContext(
        knowledge_evidence="未检索到可靠 Klonet 证据。",
        environment_evidence="server facts",
        action_catalog="action=validate_project_files",
    )

    assert "Klonet 证据" in context.planning_blocker()


@pytest.mark.skip(reason="registered action selection moved to Execution Agent")
def test_planner_accepts_only_registered_actions_and_uses_grounded_context(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    project_root = tmp_path / "demo_project"
    mains = project_root / "mains"
    mains.mkdir(parents=True)
    for name in (
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    ):
        (mains / name).write_text("# test\n", encoding="utf-8")
    llm = FakeLLM(
        [_action_payload(args={"project_root": str(project_root)})]
    )
    context = GroundedPlanContext(
        knowledge_evidence="真实 Klonet 部署文档",
        environment_evidence="project_root=%s" % project_root,
        action_catalog="action=validate_project_files",
    )

    plan = PrivilegedPlannerAgent(llm).plan(
        "帮我部署平台",
        grounded_context=context,
    )

    assert plan.steps[0].action == "validate_project_files"
    assert plan.steps[0].command == ""
    assert plan.steps[0].args["project_root"] == str(project_root)
    assert plan.grounding["knowledge_status"] == "available"
    prompt = llm.calls[0][1]["content"]
    assert "真实 Klonet 部署文档" in prompt
    assert "project_root=%s" % project_root in prompt


def test_planner_rejects_model_authored_shell_even_after_repair():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    shell_payload = json.dumps(
        {
            "status": "ready",
            "steps": [
                {
                    "step_id": "bad",
                    "title": "编造命令",
                    "objective": "deploy platform",
                    "reason": "test invalid implementation leakage",
                    "success_criteria": ["platform runs"],
                    "command": "klonet deploy",
                }
            ]
        }
    )
    llm = FakeLLM([shell_payload, shell_payload])

    try:
        PrivilegedPlannerAgent(llm).plan("帮我部署平台")
    except ValueError as exc:
        assert "must not choose execution implementation" in str(exc)
    else:
        raise AssertionError("model-authored shell must never become executable")


@pytest.mark.skip(reason="registered argv and shell choice moved to Execution Agent")
def test_planner_allows_policy_checked_argv_fallback_but_not_shell_program():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    allowed = _action_payload(
        action="run_controlled_argv",
        args={"program": "git", "argv": ["status"], "cwd": ""},
    )
    plan = PrivilegedPlannerAgent(FakeLLM([allowed])).plan("查看源码状态")

    assert plan.steps[0].action == "run_ops_command"
    assert plan.steps[0].args == {
        "program": "git",
        "argv": ["status"],
        "cwd": "",
    }
    assert plan.steps[0].risk == "readonly"
    assert plan.steps[0].command == ""

    denied = _action_payload(
        action="run_ops_command",
        args={"program": "sh", "argv": ["-c", "touch /tmp/bad"]},
    )
    llm = FakeLLM([denied, denied])
    try:
        PrivilegedPlannerAgent(llm).plan("执行脚本")
    except ValueError as exc:
        assert "program_not_allowed=sh" in str(exc)
    else:
        raise AssertionError("shell interpreters must not pass argv policy")


@pytest.mark.skip(reason="action argument validation moved to Execution Agent")
def test_planner_rejects_missing_grounded_action_arguments():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(
        [
            _action_payload(args={}),
            _action_payload(args={}),
        ]
    )
    try:
        PrivilegedPlannerAgent(llm).plan("帮我部署平台")
    except ValueError as exc:
        assert "missing_required_args=project_root" in str(exc)
    else:
        raise AssertionError("missing project root must block planning")


@pytest.mark.skip(reason="operation variant validation moved to Execution Agent")
def test_planner_rejects_incomplete_operation_variant_before_confirmation():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    invalid = _action_payload(
        action="manage_docker_image",
        args={
            "operation": "load",
            "archive_path": "/srv/images/onos.tar",
        },
    )
    llm = FakeLLM([invalid, invalid])

    try:
        PrivilegedPlannerAgent(llm).plan("加载 ONOS 镜像")
    except ValueError as exc:
        assert "archive_path_and_expected_image_required" in str(exc)
    else:
        raise AssertionError("incomplete operation variant must not be confirmable")


def test_planner_adds_deterministic_postconditions_for_registered_actions():
    from klonet_agent.ops.privileged.planner import _default_action_postconditions

    redis = _default_action_postconditions(
        "start_redis_instance",
        {"expected_port": "8368"},
    )
    network = _default_action_postconditions(
        "manage_docker_network",
        {
            "network": "klonet-overlay",
            "operation": "connect",
            "container": "onos",
        },
    )
    packages = _default_action_postconditions(
        "install_system_packages",
        {"packages": ["screen", "nginx"]},
    )

    assert redis[0]["checker"] == "port_listening"
    assert network[0]["checker"] == "docker_network_attachment"
    assert network[0]["args"]["attached"] is True
    assert [item["args"]["package"] for item in packages] == ["screen", "nginx"]


def test_standard_deploy_has_no_deterministic_resolver_when_llm_plan_is_invalid(
    tmp_path,
):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    project_root = tmp_path / "demo_project"
    project_root.mkdir()
    for name in (
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    ):
        (project_root / name).write_text("# test\n", encoding="utf-8")
    context = GroundedPlanContext(
        knowledge_evidence="检索到以下可靠 Klonet 证据",
        environment_evidence="project_root=%s" % project_root,
        action_catalog="registered actions",
        facts={"candidate_project_roots": [str(project_root)]},
    )
    llm = FakeLLM(
        [
            json.dumps({"status": "ready", "steps": []}),
            json.dumps({"status": "ready", "steps": []}),
        ]
    )

    with pytest.raises(ValueError, match="valid semantic plan"):
        PrivilegedPlannerAgent(llm).plan(
            "帮我部署平台",
            grounded_context=context,
        )


def test_recovery_never_falls_back_to_repeating_standard_deploy(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    project_root = tmp_path / "demo_project"
    project_root.mkdir()
    for name in (
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    ):
        (project_root / name).write_text("# test\n", encoding="utf-8")
    context = GroundedPlanContext(
        knowledge_evidence="Klonet recovery evidence",
        environment_evidence="failed start evidence",
        action_catalog="registered actions",
        facts={"candidate_project_roots": [str(project_root)]},
    )
    llm = FakeLLM(
        [
            json.dumps({"status": "ready", "steps": []}),
            json.dumps({"status": "ready", "steps": []}),
        ]
    )

    try:
        PrivilegedPlannerAgent(llm).plan(
            (
                "原目标：部署平台\n"
                "先处理已诊断的失败原因，再安全地继续完成原目标。\n"
                "修复能力要求：调整启动环境"
            ),
            grounded_context=context,
        )
    except ValueError as exc:
        assert "Planner did not return a valid semantic plan" in str(exc)
    else:
        raise AssertionError("recovery must not reuse the happy-path resolver")


def test_executor_dispatches_action_runner_without_shell():
    from klonet_agent.ops.operations import RecipeExecutionResult
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    calls = []

    def runner(plan, step):
        calls.append((plan, step))
        return RecipeExecutionResult("completed", "environment unchanged")

    executor = PrivilegedCommandExecutor(action_runner=runner)
    step = PrivilegedStep(
            step_id="precheck",
            title="校验项目",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                action="validate_project_files",
                args={"project_root": "/srv/klonet/demo_project"},
                risk="readonly",
            ),
            risk="readonly",
        )
    evidence = executor.execute(step)

    assert evidence.return_code == 0
    assert calls[0][1].action == "validate_project_files"
    assert calls[0][1].args["project_root"] == "/srv/klonet/demo_project"


def test_action_executor_hides_raw_output_but_keeps_audit_evidence():
    from klonet_agent.ops.operations import RecipeExecutionResult
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    starts = []
    outputs = []
    raw = (
        "recipe_id=validate_project_files "
        "project_root=/home/klonet-agent/vemu_uestc "
        "found_files=gun.py,master_main.py,celery_worker.py,"
        "web_terminal_main.py,worker_gun.py,worker_main.py "
        "environment unchanged"
    )
    executor = PrivilegedCommandExecutor(
        action_runner=lambda plan, step: RecipeExecutionResult("completed", raw),
        on_start=starts.append,
        on_output=lambda channel, chunk: outputs.append((channel, chunk)),
    )

    evidence = executor.execute(
        PrivilegedStep(
            step_id="validate",
            title="校验 Klonet 项目文件",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                action="validate_project_files",
                args={"project_root": "/home/klonet-agent/vemu_uestc"},
                risk="readonly",
            ),
            risk="readonly",
        )
    )

    assert starts == ["正在执行已确认步骤：校验 Klonet 项目文件"]
    assert outputs == []
    assert evidence.stdout == raw


def test_failed_action_respects_explicit_environment_unchanged_evidence():
    from klonet_agent.ops.operations import RecipeExecutionResult
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    executor = PrivilegedCommandExecutor(
        action_runner=lambda plan, step: RecipeExecutionResult(
            "blocked",
            "error=startup_preflight_failed environment_changed=false",
        )
    )

    evidence = executor.execute(
        PrivilegedStep(
            step_id="start",
            title="启动平台",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                action="start_platform_screens",
                args={"platform": "p", "project_root": "/srv/p"},
                risk="medium",
            ),
            risk="medium",
        )
    )

    assert evidence.return_code == 2
    assert evidence.environment_changed is False


def test_failed_platform_start_explains_pause_without_printing_traceback(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    validate = PrivilegedStep(
        step_id="validate",
        title="校验 Klonet 项目文件",
        action="validate_project_files",
        args={"project_root": "/home/klonet-agent/vemu_uestc"},
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="validate_project_files",
            args={"project_root": "/home/klonet-agent/vemu_uestc"},
            risk="readonly",
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        ),
        risk="readonly",
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
    )
    start = PrivilegedStep(
        step_id="start",
        title="启动 Klonet 平台组件",
        action="start_platform_screens",
        args={
            "platform": "vemu_uestc",
            "project_root": "/home/klonet-agent/vemu_uestc",
        },
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="start_platform_screens",
            args={
                "platform": "vemu_uestc",
                "project_root": "/home/klonet-agent/vemu_uestc",
            },
            risk="medium",
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        ),
        risk="medium",
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
    )
    plan = PrivilegedPlan(
        plan_id="priv-readable",
        goal="帮我部署平台",
        risk="medium",
        status="approved",
        steps=[validate, start],
    )
    plan.authorize()
    for step in plan.steps:
        step.status = "approved"

    validate_raw = (
        "recipe_id=validate_project_files "
        "found_files=gun.py,master_main.py,celery_worker.py,"
        "web_terminal_main.py,worker_gun.py,worker_main.py "
        "environment unchanged"
    )
    failure_raw = (
        "helper_startup_preflight_failed returncode=2 "
        "error=startup_preflight_failed component=master "
        "detail=last_error=ModuleNotFoundError: "
        "No module named 'vemu_uestc' Traceback (most recent call last): ... "
        "environment_changed=false"
    )

    class Planner:
        def plan(self, goal, **kwargs):
            del goal, kwargs
            return plan

    class Executor:
        def execute(self, step):
            if step.step_id == "validate":
                return ExecutionEvidence(return_code=0, stdout=validate_raw)
            return ExecutionEvidence(return_code=1, stderr=failure_raw)

    class PreboundExecutionAgent:
        @staticmethod
        def prepare_plan(current_plan, *, grounded_context):
            del grounded_context
            return current_plan

    class Summarizer:
        def summarize(self, step, **kwargs):
            del kwargs
            if step.step_id == "validate":
                return (
                    "项目文件校验通过：已找到 6 个必要入口文件；"
                    "未修改服务器环境。"
                )
            return (
                "平台启动失败：master 启动预检无法导入 Python 模块 "
                "vemu_uestc；未修改服务器环境。"
            )

    workflow = PrivilegedOpsWorkflow(
        planner=Planner(),
        executor=Executor(),
        verifier=PrivilegedVerifierAgent(None),
        execution_agent=PreboundExecutionAgent(),
        store=PrivilegedPlanStore(
            tmp_path / "store",
            user_id="u",
            project_id="p",
        ),
        summarizer=Summarizer(),
    )

    result = workflow.submit("帮我部署平台")

    assert result.kind == "paused"
    assert (
        "校验 Klonet 项目文件（已完成）："
        "项目文件校验通过：已找到 6 个必要入口文件；未修改服务器环境。"
    ) in result.message
    assert (
        "启动 Klonet 平台组件（已暂停）：平台启动失败：master "
        "启动预检无法导入 Python 模块 vemu_uestc"
    ) in result.message
    assert "具体原因见上方步骤" in result.message
    assert "Traceback" not in result.message
    assert result.plan.steps[1].evidence.stderr == failure_raw


def test_small_model_summarizes_redacted_evidence_without_action_rules():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionEvidence,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.summarizer import (
        PrivilegedEvidenceSummarizer,
    )

    llm = FakeLLM(
        ["master 启动失败，因为 Python 无法导入 vemu_uestc 模块；环境未被修改。"]
    )
    step = PrivilegedStep(
        step_id="start",
        title="启动平台",
        action="future_action_not_in_summary_rules",
        evidence=ExecutionEvidence(
            return_code=1,
            stderr=(
                "password=do-not-send "
                "ModuleNotFoundError: No module named 'vemu_uestc'"
            ),
            environment_changed=False,
        ),
    )

    summary = PrivilegedEvidenceSummarizer(llm).summarize(
        step,
        status="failed",
    )

    assert "vemu_uestc" in summary
    prompt = llm.calls[0][1]["content"]
    assert "do-not-send" not in prompt
    assert "[REDACTED]" in prompt


def test_small_model_explains_step_before_execution_with_redacted_args():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.summarizer import (
        PrivilegedEvidenceSummarizer,
    )

    llm = FakeLLM(
        [
            (
                "将检查 /srv/vemu_uestc 下的平台入口文件是否完整；"
                "这是只读操作，不会修改服务器。"
            )
        ]
    )
    step = PrivilegedStep(
        step_id="validate",
        title="校验项目文件",
        action="validate_project_files",
        args={
            "project_root": "/srv/vemu_uestc",
            "password": "do-not-send",
        },
        risk="readonly",
    )

    description = PrivilegedEvidenceSummarizer(llm).describe_execution(
        step,
        index=1,
        total=2,
    )

    assert description.startswith("第 1/2 步：")
    assert "/srv/vemu_uestc" in description
    prompt = llm.calls[0][1]["content"]
    assert "do-not-send" not in prompt
    assert "[REDACTED]" in prompt


@pytest.mark.skip(reason="standalone RecoveryAgent was replaced by same-Planner recovery")
def test_recovery_agent_uses_hypotheses_probes_conclusion_and_plan_review():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.recovery import PrivilegedRecoveryAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "summary": "可能是端口冲突。",
                    "hypotheses": ["端口被占用"],
                    "confirmed_cause": "",
                    "probes": [
                        {
                            "probe": "process_detail",
                            "args": {"ports": [8080]},
                            "purpose": "检查端口占用",
                        }
                    ],
                    "required_capability": "调整监听端口",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "summary": "确认端口冲突。",
                    "confirmed_cause": "8080 被其他服务占用",
                    "remaining_uncertainty": [],
                    "required_capability": "调整监听端口",
                    "planning_guidance": "修改配置后重试",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "covers_cause": True,
                    "explanation": "新计划先修改配置。",
                    "missing_capability": "",
                },
                ensure_ascii=False,
            ),
        ]
    )
    failed = PrivilegedStep(
        step_id="deploy",
        title="部署应用",
        action="run_ops_command",
        args={"program": "make", "argv": ["deploy"]},
        evidence=ExecutionEvidence(return_code=1, stderr="bind 8080 failed"),
    )
    plan = PrivilegedPlan(
        plan_id="priv-recovery-agent",
        goal="部署应用",
        risk="medium",
        steps=[failed],
    )
    replacement = PrivilegedPlan(
        plan_id="priv-repair",
        goal="修复并部署",
        risk="medium",
        steps=[
            PrivilegedStep(
                step_id="edit",
                title="调整监听端口",
                action="write_ops_file",
                args={"path": "/srv/app.conf", "content": "port=8081"},
            )
        ],
    )
    agent = PrivilegedRecoveryAgent(llm)

    analysis = agent.analyze(
        plan,
        failed,
        probe_catalog="process_detail",
    )
    conclusion = agent.conclude(
        plan,
        failed,
        analysis,
        "port 8080 owner=service-a",
    )
    review = agent.review_plan(failed, conclusion, replacement)

    assert analysis.probes[0]["probe"] == "process_detail"
    assert conclusion.confirmed_cause == "8080 被其他服务占用"
    assert review.covers_cause is True


def test_readonly_registered_action_executes_without_command_validation(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    step = PrivilegedStep(
        step_id="precheck",
        title="校验项目",
        action="validate_project_files",
        args={"project_root": str(tmp_path)},
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="validate_project_files",
            args={"project_root": str(tmp_path)},
            risk="readonly",
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        ),
        risk="readonly",
        status="approved",
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
    )
    plan = PrivilegedPlan(
        plan_id="priv-readonly-action",
        goal="校验项目",
        risk="readonly",
        status="approved",
        steps=[step],
    )

    class Planner:
        def plan(self, goal, **kwargs):
            del goal, kwargs
            return plan

    class Executor:
        def execute(self, action_step):
            from klonet_agent.ops.privileged.contracts import ExecutionEvidence

            assert action_step.action == "validate_project_files"
            return ExecutionEvidence(return_code=0)

    class Verifier:
        def verify_deterministic_step(self, current_plan, current_step):
            from klonet_agent.ops.privileged.contracts import VerificationDecision

            del current_plan, current_step
            return VerificationDecision(status="passed", goal_achieved=True)

        verify_step = verify_deterministic_step

    class PreboundExecutionAgent:
        @staticmethod
        def prepare_plan(current_plan, *, grounded_context):
            del grounded_context
            return current_plan

    workflow = PrivilegedOpsWorkflow(
        planner=Planner(),
        executor=Executor(),
        verifier=Verifier(),
        execution_agent=PreboundExecutionAgent(),
        store=PrivilegedPlanStore(
            tmp_path / "store",
            user_id="u",
            project_id="p",
        ),
    )

    result = workflow.submit("校验项目")

    assert result.kind == "completed"


def test_old_or_unregistered_mutating_command_plan_is_never_executed(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    class Planner:
        def __init__(self, plan):
            self.plan_result = plan

        def plan(self, goal, **kwargs):
            del goal, kwargs
            return self.plan_result

    class Executor:
        def __init__(self):
            self.calls = 0

        def execute(self, step):
            del step
            self.calls += 1
            raise AssertionError("unregistered command must not execute")

    class Verifier:
        def verify_step(self, plan, step):
            del plan, step
            raise AssertionError("verification must not run")

    for schema_version in (1, 2):
        step = PrivilegedStep(
            step_id="bad",
            title="legacy command",
            command="klonet deploy",
            risk="medium",
            status="approved",
        )
        plan = PrivilegedPlan(
            plan_id="priv-command-%s" % schema_version,
            goal="部署平台",
            risk="medium",
            schema_version=schema_version,
            status="approved",
            steps=[step],
        )
        plan.authorize()
        executor = Executor()
        workflow = PrivilegedOpsWorkflow(
            planner=Planner(plan),
            executor=executor,
            verifier=Verifier(),
            store=PrivilegedPlanStore(
                tmp_path / ("store-%s" % schema_version),
                user_id="u",
                project_id="p",
            ),
        )

        result = workflow.submit("部署平台")

        assert result.kind == "blocked"
        assert executor.calls == 0
