from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": messages, "tools": tools, "kwargs": kwargs}
        )
        content = self.responses.pop(0)
        if tools:
            function_name = tools[0]["function"]["name"]
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name=function_name,
                            arguments=content,
                        )
                    )
                ],
            )
        else:
            message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=message
                )
            ]
        )


def _semantic_payload(*, objective="inspect the current platform state"):
    return json.dumps(
        {
            "status": "ready",
            "goal": "inspect platform",
            "assumptions": [],
            "steps": [
                {
                    "step_id": "inspect",
                    "title": "检查平台",
                    "objective": objective,
                    "reason": "the current state is required before further work",
                    "evidence_refs": ["server facts"],
                    "depends_on": [],
                    "expected_effects": [],
                    "success_criteria": ["the current state is observed"],
                    "risk_suggestion": "readonly",
                }
            ],
        }
    )


def test_planner_can_probe_then_returns_semantic_plan_without_actions():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    probes = []
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "ports",
                            "args": {"ports": [8080]},
                            "purpose": "identify the current listener",
                        }
                    ],
                }
            ),
            _semantic_payload(),
        ]
    )
    progress = []
    planner = PrivilegedPlannerAgent(
        llm,
        probe_runner=lambda requests: (
            probes.extend(requests) or "port 8080 is not listening"
        ),
        on_progress=progress.append,
    )

    plan = planner.plan("inspect platform")

    assert probes[0]["probe"] == "ports"
    assert plan.schema_version == 3
    assert plan.steps[0].execution_binding is None
    assert plan.steps[0].action == ""
    assert plan.probe_history[0]["round"] == 1
    assert "action=" not in llm.calls[0]["messages"][0]["content"]
    assert any("准备执行 1 个只读检查（ports）" in item for item in progress)
    assert progress[-1] == "规划结论：已形成 1 个语义步骤，开始匹配安全执行能力。"


def test_planner_receives_recent_dialogue_for_continuation_resolution():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM([_semantic_payload(objective="deploy instance lht")])
    dialogue = (
        "user: 新增一个 Klonet 平台实例\n"
        "assistant: 请提供实例名称"
    )

    PrivilegedPlannerAgent(llm).plan(
        "叫 lht 吧",
        conversation_context=dialogue,
    )

    prompt = llm.calls[0]["messages"][1]["content"]
    assert dialogue in prompt
    assert "Current request:\n叫 lht 吧" in prompt


def test_planner_reuses_prior_probe_evidence_instead_of_repeating_probe():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "ports",
                            "args": {"ports": [8080]},
                            "purpose": "repeat old check",
                        }
                    ],
                }
            ),
            _semantic_payload(),
        ]
    )
    progress = []
    planner = PrivilegedPlannerAgent(
        llm,
        probe_runner=lambda requests: (_ for _ in ()).throw(
            AssertionError("duplicate probe must not execute")
        ),
        on_progress=progress.append,
    )

    plan = planner.plan(
        "inspect platform",
        planning_feedback="port 8080 is available",
        prior_probe_history=[
            {
                "requests": [
                    {"probe": "ports", "args": {"ports": [8080]}}
                ],
                "evidence": "port 8080 is available",
            }
        ],
    )

    assert plan.steps
    assert any("已拒绝重复只读检查（ports）" in item for item in progress)


def test_planner_accepts_legacy_action_risk_labels_as_semantic_aliases():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    payload = json.loads(_semantic_payload())
    payload["risk"] = "privileged"
    payload["steps"][0]["risk_suggestion"] = "privileged"

    plan = PrivilegedPlannerAgent(
        FakeLLM([json.dumps(payload)])
    ).plan("inspect platform")

    assert plan.risk == "medium"
    assert plan.steps[0].risk == "medium"


def test_failure_packet_context_routes_rag_to_troubleshooting():
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    seen = []
    builder = PrivilegedPlanContextBuilder(
        knowledge_search=lambda query, **kwargs: (
            seen.append((query, kwargs)) or "recovery runbook"
        ),
        environment_inspector=lambda args: "environment",
    )

    builder.build(
        "上一执行失败，请根据 Failure Packet 诊断并修复",
        supplemental_environment_context="Connection refused on Redis port 9368",
    )

    assert seen[0][1]["task_type"] == "troubleshooting"
    assert "Redis port 9368" in seen[0][0]


def test_execution_agent_maps_semantic_step_to_registered_action():
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload()])
    ).plan("inspect platform")
    binder = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "manual_checkpoint",
                    "selection_reason": "registered checkpoint covers objective",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "args": {"reason": "record observed platform state"},
                    "binding_reason": "the registered checkpoint covers the objective",
                    "resolved_from_evidence": ["server facts"],
                    "postconditions": [
                        {"checker": "exit_code_zero", "args": {}}
                    ],
                }
            )
        ]
    )

    progress = []
    bound = PrivilegedExecutionAgent(
        binder,
        on_progress=progress.append,
    ).prepare_plan(
        plan,
        grounded_context=None,
    )

    binding = bound.steps[0].execution_binding
    assert binding.kind == "registered_action"
    assert binding.action == "manual_checkpoint"
    assert binding.approval_scope == "plan"
    assert bound.status == "awaiting_confirmation"
    assert "manual_checkpoint" in binder.calls[0]["messages"][1]["content"]
    assert len(binder.calls) == 2
    assert "frozen_action" in binder.calls[1]["messages"][1]["content"]
    assert progress[0].startswith("实现节点 1/1")
    assert progress[-1].endswith("注册 Action：manual_checkpoint。")


def test_execution_agent_repairs_selected_action_args_without_replanning(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    source = tmp_path / "source"
    destination = tmp_path / "lht"
    source.mkdir()
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="copy source for lht")])
    ).plan("deploy lht")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "sync_directory",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    # Stage 2 is not allowed to switch the frozen selection.
                    "action": "write_ops_file",
                    "args": {
                        "source": str(source),
                        "destination": str(destination),
                    },
                    "resolved_from_evidence": ["observed source directory"],
                }
            ),
        ]
    )
    progress = []

    bound = PrivilegedExecutionAgent(
        binder_llm,
        on_progress=progress.append,
    ).prepare_plan(plan, grounded_context=None)

    binding = bound.steps[0].execution_binding
    assert binding is not None
    assert binding.action == "sync_directory"
    assert binding.args == {
        "source": str(source),
        "destination": str(destination),
    }
    assert len(binder_llm.calls) == 2
    repair_request = json.loads(binder_llm.calls[1]["messages"][1]["content"])
    assert repair_request["frozen_action"] == "sync_directory"
    assert repair_request["required_args"] == ["source", "destination"]
    contract_call = binder_llm.calls[1]
    contract_tool = contract_call["tools"][0]["function"]
    assert contract_tool["name"] == "bind_action_sync_directory"
    assert contract_tool["parameters"]["properties"]["args"]["required"] == [
        "source",
        "destination",
    ]
    assert contract_call["kwargs"]["tool_choice"]["function"]["name"] == (
        "bind_action_sync_directory"
    )
    assert contract_call["kwargs"]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    assert any("单独补全参数合同" in item for item in progress)


def test_execution_agent_accepts_json_stringified_registered_action_args():
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="record deployment checkpoint")])
    ).plan("record checkpoint")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "registered_action",
                    "action": "manual_checkpoint",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "args": json.dumps({"reason": "deployment prepared"}),
                }
            )
        ]
    )

    bound = PrivilegedExecutionAgent(binder_llm).prepare_plan(
        plan,
        grounded_context=None,
    )

    assert bound.steps[0].execution_binding.args == {
        "reason": "deployment prepared"
    }
    assert len(binder_llm.calls) == 2


def test_missing_action_is_reported_before_missing_args():
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )

    agent = PrivilegedExecutionAgent(FakeLLM([]))

    try:
        agent._registered_binding(
            {"status": "registered_action"},
            PrivilegedStep(
                step_id="configure",
                title="修改配置",
                objective="modify config ports",
                risk="high",
            ),
            None,
        )
    except ValueError as exc:
        assert str(exc) == "action_not_directly_registered=<missing>"
    else:
        raise AssertionError("missing action must be rejected")


def test_execution_agent_repairs_invalid_status_and_accepts_case_normalization():
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload()])
    ).plan("inspect platform")
    valid_selection = {
        "status": "REGISTERED_ACTION",
        "action": "manual_checkpoint",
    }
    valid_contract = {
        "status": "ready",
        "args": {"reason": "record observed platform state"},
        "binding_reason": "registered action matches",
        "resolved_from_evidence": ["server facts"],
    }
    binder_llm = FakeLLM(
        [
            json.dumps({"status": "ready"}),
            json.dumps({"status": "success"}),
            json.dumps(valid_selection),
            json.dumps(valid_contract),
        ]
    )

    bound = PrivilegedExecutionAgent(binder_llm).prepare_plan(
        plan,
        grounded_context=None,
    )

    assert bound.steps[0].execution_binding.action == "manual_checkpoint"
    assert len(binder_llm.calls) == 4
    repair = binder_llm.calls[1]["messages"][-1]["content"]
    assert "shell_artifact" in repair
    assert "stage 2 status ready is invalid" in repair


def test_execution_selection_prompt_is_bounded_and_omits_stage2_catalog():
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload()])
    ).plan("inspect platform")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "test stops after prompt capture",
                }
            )
        ]
    )
    context = GroundedPlanContext(
        knowledge_evidence="K" * 40000,
        environment_evidence="E" * 40000,
        action_catalog="summary",
        facts={"environment_model": {"platform": "lht"}},
    )

    try:
        PrivilegedExecutionAgent(binder_llm).prepare_plan(
            plan,
            grounded_context=context,
        )
    except Exception:
        pass
    content = binder_llm.calls[0]["messages"][1]["content"]
    payload = json.loads(content)

    assert payload["required_status_values"] == [
        "registered_action",
        "shell_artifact",
        "need_evidence",
        "blocked",
    ]
    assert "manual_checkpoint" in payload["registered_action_catalog"]
    assert "sync_directory" in payload["allowed_action_names"]
    assert "system_environment" in payload["registered_probe_catalog"]
    assert "registered_checker_catalog" not in payload
    assert "exact grounded values will be supplied to stage 2" in payload[
        "selection_context"
    ]
    assert "K" * 100 not in payload["selection_context"]
    assert "E" * 100 not in payload["selection_context"]
    assert len(payload["selection_context"]) < 7000
    selection_call = binder_llm.calls[0]
    selection_tool = selection_call["tools"][0]
    assert selection_call["kwargs"]["tool_choice"]["function"]["name"] == (
        "select_execution_implementation"
    )
    assert selection_tool["function"]["parameters"]["properties"][
        "action"
    ]["enum"] == ["", *payload["allowed_action_names"]]


def test_unregistered_shell_requires_plan_then_exact_step_confirmation(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    target = tmp_path / "shell-result"
    planner = PrivilegedPlannerAgent(
        FakeLLM(
            [
                _semantic_payload(
                    objective="create the one-off deployment marker"
                )
            ]
        )
    )
    binder = PrivilegedExecutionAgent(
        FakeLLM(
            [
                json.dumps(
                    {
                        "status": "shell_artifact",
                        "selection_reason": "no registered action covers marker",
                    }
                ),
                json.dumps(
                    {
                        "status": "ready",
                        "script": "touch %s" % target,
                        "cwd": str(tmp_path),
                        "run_as": "",
                        "timeout": 10,
                        "declared_changes": [str(target)],
                        "rollback": "remove the marker file",
                        "binding_reason": "no registered action covers this marker",
                        "postconditions": [
                            {
                                "checker": "file_exists",
                                "args": {"path": str(target)},
                            }
                        ],
                    }
                )
            ]
        )
    )
    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        execution_agent=binder,
        executor=PrivilegedCommandExecutor(),
        verifier=PrivilegedVerifierAgent(None),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
    )

    waiting = workflow.submit("create marker")
    plan_id = waiting.plan.plan_id
    step_id = waiting.plan.steps[0].step_id
    after_plan_confirmation = workflow.handle_command(
        "confirm-priv %s" % plan_id
    )
    details = workflow.handle_command("show-priv %s" % plan_id)
    completed = workflow.handle_command(
        "confirm-priv-step %s %s" % (plan_id, step_id)
    )

    assert waiting.kind == "awaiting_confirmation"
    assert after_plan_confirmation.kind == "awaiting_step_confirmation"
    assert "固定脚本如下" in details.message
    assert "脚本 SHA-256" in details.message
    assert completed.kind == "completed"
    assert target.is_file()
    assert completed.plan.is_authorized is True
    assert (
        completed.plan.steps[0].execution_binding.shell_artifact.status
        == "executed"
    )


def test_execution_agent_completes_missing_shell_postconditions_separately(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "generated-marker"
    plan = PrivilegedPlannerAgent(
        FakeLLM(
            [
                _semantic_payload(
                    objective="create an observable deployment marker"
                )
            ]
        )
    ).plan("create marker")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "no registered action covers marker",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": "touch %s" % target,
                    "cwd": str(tmp_path),
                    "run_as": "",
                    "timeout": 10,
                    "declared_changes": [str(target)],
                    "rollback": "remove the marker",
                    "binding_reason": "no registered action covers this",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "postconditions": [
                        {
                            "checker": "file_exists",
                            "args": {"path": str(target)},
                        }
                    ],
                }
            ),
        ]
    )
    progress = []

    bound = PrivilegedExecutionAgent(
        binder_llm,
        on_progress=progress.append,
    ).prepare_plan(plan, grounded_context=None)

    binding = bound.steps[0].execution_binding
    assert binding is not None
    assert binding.kind == "shell_artifact"
    assert binding.postconditions == [
        {"checker": "file_exists", "args": {"path": str(target)}}
    ]
    assert binding.shell_artifact is not None
    assert "touch %s" % target in binding.shell_artifact.script
    assert len(binder_llm.calls) == 3
    shell_request = json.loads(
        binder_llm.calls[1]["messages"][1]["content"]
    )
    assert shell_request["frozen_implementation_kind"] == "shell_artifact"
    assert "checker=file_contains args=path,text" in shell_request[
        "registered_checker_catalog"
    ]
    verification_request = json.loads(
        binder_llm.calls[2]["messages"][1]["content"]
    )
    assert verification_request["frozen_shell_artifact"]["sha256"] == (
        binding.shell_artifact.sha256
    )
    assert any("脚本已通过安全校验并冻结" in item for item in progress)
    assert progress[-1].endswith("需要一次性脚本并单独确认。")


def test_shell_verification_repair_cannot_replace_frozen_script(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "frozen-marker"
    original_script = "touch %s" % target
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="create the frozen marker")])
    ).plan("create marker")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "no registered action covers marker",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": original_script,
                    "cwd": str(tmp_path),
                    "run_as": "",
                    "timeout": 10,
                    "declared_changes": [str(target)],
                    "rollback": "remove the marker",
                    "postconditions": [
                        {"checker": "file_exists", "args": {}}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": "rm -f /tmp/unrelated",
                    "postconditions": [
                        {
                            "checker": "file_exists",
                            "args": {"path": str(target)},
                        }
                    ],
                }
            ),
        ]
    )

    bound = PrivilegedExecutionAgent(binder_llm).prepare_plan(
        plan,
        grounded_context=None,
    )

    artifact = bound.steps[0].execution_binding.shell_artifact
    assert artifact is not None
    assert original_script in artifact.script
    assert "unrelated" not in artifact.script


def test_shell_postconditions_require_observable_state_not_only_exit_code(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    target = tmp_path / "observable-marker"
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="create an observable marker")])
    ).plan("create marker")
    binder_llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "shell_artifact",
                    "selection_reason": "no registered action covers marker",
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "script": "touch %s" % target,
                    "cwd": str(tmp_path),
                    "run_as": "",
                    "timeout": 10,
                    "declared_changes": [str(target)],
                    "rollback": "remove marker",
                    "postconditions": [
                        {"checker": "exit_code_zero", "args": {}}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "postconditions": [
                        {"checker": "exit_code_zero", "args": {}}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "ready",
                    "postconditions": [
                        {
                            "checker": "file_exists",
                            "args": {"path": str(target)},
                        }
                    ],
                }
            ),
        ]
    )

    bound = PrivilegedExecutionAgent(binder_llm).prepare_plan(
        plan,
        grounded_context=None,
    )

    assert bound.steps[0].postconditions[0]["checker"] == "file_exists"
    repair_prompt = binder_llm.calls[3]["messages"][-1]["content"]
    assert "Repair only postconditions" in repair_prompt


def test_initial_binding_failure_returns_to_planner_before_blocking(tmp_path):
    from klonet_agent.ops.privileged.contracts import ExecutionBinding
    from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    planner_llm = FakeLLM(
        [
            _semantic_payload(objective="deploy lht in one broad step"),
            _semantic_payload(objective="prepare the lht instance safely"),
        ]
    )
    planner = PrivilegedPlannerAgent(planner_llm)

    class BindingAgent:
        def __init__(self):
            self.calls = 0

        def prepare_plan(self, plan, *, grounded_context):
            del grounded_context
            self.calls += 1
            if self.calls == 1:
                plan.probe_history.append(
                    {
                        "phase": "execution_binding",
                        "step_id": plan.steps[0].step_id,
                        "round": 1,
                        "requests": [
                            {"probe": "ports", "args": {"ports": [8080]}}
                        ],
                        "evidence": "port 8080 is available",
                    }
                )
                raise ExecutionBindingError(
                    "no registered action safely covers the broad step"
                )
            step = plan.steps[0]
            step.execution_binding = ExecutionBinding(
                kind="registered_action",
                action="manual_checkpoint",
                args={"reason": "prepared"},
                risk="medium",
                approval_scope="plan",
                postconditions=[
                    {"checker": "exit_code_zero", "args": {}}
                ],
            )
            step.risk = "medium"
            step.postconditions = list(step.execution_binding.postconditions)
            plan.risk = "medium"
            plan.status = "awaiting_confirmation"
            return plan

    binder = BindingAgent()
    progress = []
    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        execution_agent=binder,
        executor=object(),
        verifier=object(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
        on_progress=progress.append,
    )

    result = workflow.submit("部署名为 lht 的平台实例")

    assert result.kind == "awaiting_confirmation"
    assert binder.calls == 2
    assert len(planner_llm.calls) == 2
    retry_prompt = planner_llm.calls[1]["messages"][1]["content"]
    assert "execution_binding" in retry_prompt
    assert "no registered action safely covers" in retry_prompt
    assert "port 8080 is available" in retry_prompt
    assert result.plan.probe_history[0]["evidence"] == "port 8080 is available"
    assert "第 1/2 次" in progress[0]
    assert "no registered action safely covers" in progress[0]


def test_initial_binding_replan_loop_is_bounded(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    planner_llm = FakeLLM([_semantic_payload()] * 3)
    planner = PrivilegedPlannerAgent(planner_llm)

    class AlwaysFailingBindingAgent:
        def __init__(self):
            self.calls = 0

        def prepare_plan(self, plan, *, grounded_context):
            del plan, grounded_context
            self.calls += 1
            raise ExecutionBindingError("binding remains impossible")

    binder = AlwaysFailingBindingAgent()
    workflow = PrivilegedOpsWorkflow(
        planner=planner,
        execution_agent=binder,
        executor=object(),
        verifier=object(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
    )

    result = workflow.submit("部署名为 lht 的平台实例")

    assert result.kind == "paused"
    assert "2 次重新规划后仍失败" in result.message
    assert "请由你决定" in result.message
    assert "replan-priv" in result.message
    assert result.plan.status == "paused"
    assert binder.calls == 3
    assert len(planner_llm.calls) == 3


def test_invalid_execute_contract_does_not_consume_planner_replans(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import ExecutionBindingError
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    planner_llm = FakeLLM([_semantic_payload()])
    progress = []

    class InvalidContractBindingAgent:
        def prepare_plan(self, plan, *, grounded_context):
            del plan, grounded_context
            raise ExecutionBindingError(
                "registered action args must be an object",
                replan_recommended=False,
                category="implementation_contract_invalid",
            )

    workflow = PrivilegedOpsWorkflow(
        planner=PrivilegedPlannerAgent(planner_llm),
        execution_agent=InvalidContractBindingAgent(),
        executor=object(),
        verifier=object(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
        on_progress=progress.append,
    )

    result = workflow.submit("部署名为 lht 的平台实例")

    assert result.kind == "paused"
    assert "未触发无意义的 Planner 重规划" in result.message
    assert "请由你决定" in result.message
    assert len(planner_llm.calls) == 1
    assert any("不消耗 Planner 重规划次数" in item for item in progress)


def test_execution_agent_binds_verification_only_without_command(tmp_path):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    marker = tmp_path / "healthy"
    marker.write_text("ok", encoding="utf-8")
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="verify deployment health")])
    ).plan("verify deployment")
    binder = PrivilegedExecutionAgent(
        FakeLLM(
            [
                json.dumps(
                    {
                        "status": "verification_only",
                        "action": "",
                        "selection_reason": "the step only verifies state",
                    }
                ),
                json.dumps(
                    {
                        "status": "ready",
                        "binding_reason": "file state proves health",
                        "postconditions": [
                            {
                                "checker": "file_exists",
                                "args": {"path": str(marker)},
                            }
                        ],
                    }
                ),
            ]
        )
    )

    bound = binder.prepare_plan(plan, grounded_context=None)
    step = bound.steps[0]
    evidence = PrivilegedCommandExecutor().execute(step)
    step.evidence = evidence
    decision = PrivilegedVerifierAgent(None).verify_deterministic_step(
        bound,
        step,
    )

    assert step.execution_binding.kind == "verification_only"
    assert evidence.return_code == 0
    assert evidence.environment_changed is False
    assert decision.status == "passed"


def test_execution_agent_reselects_after_action_contract_is_not_grounded(
    tmp_path,
):
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    marker = tmp_path / "healthy"
    progress = []
    plan = PrivilegedPlannerAgent(
        FakeLLM([_semantic_payload(objective="verify deployment health")])
    ).plan("verify deployment")
    binder = PrivilegedExecutionAgent(
        FakeLLM(
            [
                json.dumps(
                    {
                        "status": "registered_action",
                        "action": "run_ops_command",
                        "selection_reason": "initial but unsuitable choice",
                    }
                ),
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "no grounded program and argv",
                    }
                ),
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "no grounded program and argv",
                    }
                ),
                json.dumps(
                    {
                        "status": "verification_only",
                        "action": "",
                        "selection_reason": "no execution is required",
                    }
                ),
                json.dumps(
                    {
                        "status": "ready",
                        "binding_reason": "use a deterministic state check",
                        "postconditions": [
                            {
                                "checker": "file_exists",
                                "args": {"path": str(marker)},
                            }
                        ],
                    }
                ),
            ]
        ),
        on_progress=progress.append,
    )

    bound = binder.prepare_plan(plan, grounded_context=None)

    assert bound.steps[0].execution_binding.kind == "verification_only"
    assert any("正在重新选择" in item for item in progress)


def test_workflow_safely_retries_one_transient_idempotent_action(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
        VerificationDecision,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    class Executor:
        def __init__(self):
            self.calls = 0

        def execute(self, step):
            del step
            self.calls += 1
            if self.calls == 1:
                return ExecutionEvidence(
                    return_code=1,
                    stderr="connection refused environment_changed=false",
                    environment_changed=False,
                )
            return ExecutionEvidence(return_code=0, environment_changed=False)

    class Verifier:
        def __init__(self):
            self.calls = 0

        def verify_step(self, plan, step):
            del plan, step
            self.calls += 1
            if self.calls == 1:
                return VerificationDecision(
                    status="failed",
                    reason="service temporarily unavailable",
                )
            return VerificationDecision(status="passed", reason="healthy")

    step = PrivilegedStep(
        step_id="start",
        title="启动组件",
        objective="start component",
        risk="medium",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="start_screen_component",
            args={"project_root": str(tmp_path), "component": "worker"},
            risk="medium",
        ),
    )
    plan = PrivilegedPlan(
        plan_id="priv-transient-retry",
        goal="start platform",
        risk="medium",
        steps=[step],
        status="awaiting_confirmation",
    )
    store = PrivilegedPlanStore(tmp_path / "memory", user_id="u", project_id="p")
    store.save(plan)
    executor = Executor()
    progress = []
    workflow = PrivilegedOpsWorkflow(
        planner=object(),
        execution_agent=object(),
        executor=executor,
        verifier=Verifier(),
        store=store,
        on_progress=progress.append,
    )

    result = workflow.approve_plan(plan.plan_id)

    assert result.kind == "completed"
    assert executor.calls == 2
    assert result.plan.steps[0].execution_attempts == 2
    assert any("有限重试" in item for item in progress)


def test_runtime_failure_rebinds_implementation_and_requires_confirmation(
    tmp_path,
):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
        VerificationDecision,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    class Executor:
        def execute(self, step):
            del step
            return ExecutionEvidence(
                return_code=1,
                stderr="implementation did not satisfy objective",
                environment_changed=False,
            )

    class Verifier:
        def verify_step(self, plan, step):
            del plan, step
            return VerificationDecision(
                status="failed",
                reason="expected state is absent",
            )

    class Context:
        def build(self, goal, supplemental_environment_context=""):
            del goal, supplemental_environment_context
            return SimpleNamespace(planning_blocker=lambda: "")

    class Binder:
        def prepare_step(
            self,
            plan,
            step,
            *,
            grounded_context,
            implementation_feedback="",
        ):
            del plan, step, grounded_context
            assert "rejected_implementation" in implementation_feedback
            return ExecutionBinding(
                kind="verification_only",
                risk="readonly",
                postconditions=[
                    {"checker": "file_exists", "args": {"path": str(tmp_path)}}
                ],
                binding_reason="materially different implementation",
            )

    step = PrivilegedStep(
        step_id="repair",
        title="修复实例",
        objective="repair instance",
        risk="medium",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            action="stop_klonet_runtime_instance",
            args={"project_root": str(tmp_path)},
            risk="medium",
        ),
    )
    plan = PrivilegedPlan(
        plan_id="priv-runtime-rebind",
        goal="repair platform",
        risk="medium",
        steps=[step],
        status="awaiting_confirmation",
    )
    store = PrivilegedPlanStore(tmp_path / "memory", user_id="u", project_id="p")
    store.save(plan)
    workflow = PrivilegedOpsWorkflow(
        planner=object(),
        execution_agent=Binder(),
        executor=Executor(),
        verifier=Verifier(),
        store=store,
        context_builder=Context(),
    )

    result = workflow.approve_plan(plan.plan_id)

    assert result.kind == "awaiting_confirmation"
    assert result.plan.is_authorized is False
    assert result.plan.steps[0].execution_binding.kind == "verification_only"
    assert "旧授权已经失效" in result.message
    assert "confirm-priv" in result.message


def test_unfinished_plan_options_prevent_plain_continue_from_replanning(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    plan = PrivilegedPlan(
        plan_id="priv-resume-choice",
        goal="deploy lht",
        risk="medium",
        steps=[PrivilegedStep(step_id="deploy", title="部署", risk="medium")],
        status="awaiting_confirmation",
    )
    store = PrivilegedPlanStore(tmp_path / "memory", user_id="u", project_id="p")
    store.save(plan)
    workflow = PrivilegedOpsWorkflow(
        planner=object(),
        execution_agent=object(),
        executor=object(),
        verifier=object(),
        store=store,
    )

    result = workflow.unfinished_plan_options()

    assert result.kind == "recovery_options"
    assert "不会自动执行" in result.message
    assert "confirm-priv priv-resume-choice" in result.message


def test_shell_policy_hard_denies_dynamic_egress_secrets_and_agent_changes(
    tmp_path,
):
    from klonet_agent.ops.privileged.shell_artifact import (
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    policy = ShellArtifactPolicy()
    scripts = (
        "echo $(id)",
        "curl https://example.test/upload",
        "cat /etc/shadow",
        "sed -i 's/x/y/' ops/privileged/policy.py",
        "PASSWORD=plain-text command true",
        "python3 -c 'print(1)'",
    )
    for index, script in enumerate(scripts):
        artifact = create_shell_artifact(
            artifact_id="shell-%s" % index,
            script=script,
            cwd=str(tmp_path),
            run_as="",
            timeout=10,
            environment_fingerprint="",
            declared_changes=[],
            rollback="",
            nonce="nonce-%s" % index,
        )
        assert policy.validate(artifact), script


def test_shell_policy_rejects_normalized_empty_artifact(tmp_path):
    from klonet_agent.ops.privileged.shell_artifact import (
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    artifact = create_shell_artifact(
        artifact_id="shell-empty",
        script="",
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env",
        declared_changes=[str(tmp_path / "marker")],
        rollback="none",
        nonce="nonce",
    )

    assert ShellArtifactPolicy().validate(artifact) == "shell_artifact_empty"


def test_shell_policy_allows_bounded_multiline_configuration(tmp_path):
    from klonet_agent.ops.privileged.shell_artifact import (
        MAX_SCRIPT_LINES,
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    script = "\n".join("printf '%s\\n' line-%s" % ("%s", index) for index in range(80))
    artifact = create_shell_artifact(
        artifact_id="shell-multiline",
        script=script,
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env",
        declared_changes=[str(tmp_path / "config")],
        rollback="remove config",
        nonce="nonce",
    )

    assert MAX_SCRIPT_LINES >= 80
    assert ShellArtifactPolicy().validate(artifact) == ""


def test_shell_policy_reports_actual_line_limit(tmp_path):
    from klonet_agent.ops.privileged.shell_artifact import (
        MAX_SCRIPT_LINES,
        ShellArtifactPolicy,
        create_shell_artifact,
    )

    actual_lines = MAX_SCRIPT_LINES + 1
    script = "\n".join("true" for _ in range(actual_lines))
    artifact = create_shell_artifact(
        artifact_id="shell-too-many-lines",
        script=script,
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env",
        declared_changes=[str(tmp_path / "config")],
        rollback="remove config",
        nonce="nonce",
    )

    assert ShellArtifactPolicy().validate(artifact) == (
        "shell_artifact_too_many_lines=%s>%s"
        % (actual_lines, MAX_SCRIPT_LINES)
    )


def test_executor_refuses_changed_expired_drifted_or_reused_shell(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.shell_artifact import create_shell_artifact

    artifact = create_shell_artifact(
        artifact_id="shell-once",
        script="touch %s" % (tmp_path / "once"),
        cwd=str(tmp_path),
        run_as="",
        timeout=10,
        environment_fingerprint="env-a",
        declared_changes=[str(tmp_path / "once")],
        rollback="remove once",
        nonce="nonce",
    )
    artifact.status = "approved"
    artifact.approved_contract_hash = artifact.contract_hash
    step = PrivilegedStep(
        step_id="once",
        title="one time",
        objective="create marker once",
        reason="test",
        success_criteria=["marker exists"],
        risk="high",
        approval_scope="step",
        execution_binding=ExecutionBinding(
            kind="shell_artifact",
            risk="high",
            approval_scope="step",
            shell_artifact=artifact,
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        ),
    )
    drifted = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-b"
    ).execute(step)
    assert drifted.stderr == "shell_artifact_environment_fingerprint_changed"

    artifact.environment_fingerprint = "env-a"
    completed = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    reused = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert completed.return_code == 0
    assert reused.stderr == "shell_artifact_not_exactly_approved"

    artifact.status = "approved"
    artifact.expires_at = "2000-01-01T00:00:00+00:00"
    artifact.approved_contract_hash = artifact.contract_hash
    expired = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert expired.stderr == "shell_artifact_expired"

    artifact.status = "approved"
    artifact.expires_at = "2999-01-01T00:00:00+00:00"
    artifact.approved_contract_hash = artifact.contract_hash
    artifact.script += "echo changed\n"
    changed = PrivilegedCommandExecutor(
        environment_fingerprint_provider=lambda: "env-a"
    ).execute(step)
    assert changed.stderr == "shell_artifact_contract_not_exactly_approved"


def test_verifier_probe_evidence_is_persisted_and_cannot_override_failure():
    from klonet_agent.ops.privileged.contracts import (
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = PrivilegedStep(
        step_id="verify",
        title="verify",
        objective="verify service",
        reason="test",
        success_criteria=["service is active"],
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
        evidence=ExecutionEvidence(return_code=9, stderr="failed"),
    )
    plan = PrivilegedPlan(
        plan_id="priv-v3",
        goal="verify service",
        risk="medium",
        steps=[step],
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "service",
                            "args": {"services": ["nginx"]},
                            "purpose": "observe service state",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "passed",
                    "summary": "service looks active",
                    "confirmed_facts": ["nginx active"],
                    "failed_criteria": [],
                    "missing_evidence": [],
                    "reflection": "the command itself still failed",
                    "recommended_next_focus": "inspect command failure",
                }
            ),
        ]
    )
    verifier = PrivilegedVerifierAgent(
        llm,
        probe_runner=lambda requests: "nginx active",
    )

    decision = verifier.verify_step(plan, step)

    assert decision.status == "failed"
    assert decision.probe_history[0]["requests"][0]["probe"] == "service"
    assert "return_code=9" in decision.failures


def test_replan_rejects_same_failed_remaining_route(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ExecutionEvidence,
        PrivilegedPlan,
        PrivilegedStep,
        VerificationDecision,
    )
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    def step(step_id):
        return PrivilegedStep(
            step_id=step_id,
            title="启动平台",
            objective="start the Klonet platform",
            reason="the user requested deployment",
            success_criteria=["platform is healthy"],
            risk="medium",
            status="approved",
            execution_binding=ExecutionBinding(
                kind="registered_action",
                action="manual_checkpoint",
                args={"reason": "same route"},
                risk="medium",
                postconditions=[{"checker": "exit_code_zero", "args": {}}],
            ),
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        )

    original = PrivilegedPlan(
        plan_id="priv-loop",
        goal="deploy platform",
        risk="medium",
        status="approved",
        steps=[step("start")],
    )
    original.authorize()
    replacement = PrivilegedPlan(
        plan_id="priv-replacement",
        goal="deploy platform",
        risk="medium",
        steps=[step("start-again")],
    )

    class Planner:
        def plan(self, goal, **kwargs):
            del goal, kwargs
            return replacement

    class PreboundExecutionAgent:
        @staticmethod
        def prepare_plan(plan, *, grounded_context):
            del grounded_context
            plan.status = "awaiting_confirmation"
            return plan

    class ContextBuilder:
        @staticmethod
        def current_environment_fingerprint():
            return "env"

        @staticmethod
        def build(goal, **kwargs):
            del goal, kwargs
            return GroundedPlanContext(
                knowledge_evidence="recovery runbook",
                environment_evidence="same failure evidence",
                action_catalog="capability summary",
            )

    class Executor:
        @staticmethod
        def execute(current_step):
            del current_step
            return ExecutionEvidence(return_code=1, stderr="same failure")

    class Verifier:
        @staticmethod
        def verify_step(plan, current_step):
            del plan, current_step
            return VerificationDecision(
                status="failed",
                reason="same failure",
                reflection="the route did not change the prerequisite",
            )

    workflow = PrivilegedOpsWorkflow(
        planner=Planner(),
        execution_agent=PreboundExecutionAgent(),
        executor=Executor(),
        verifier=Verifier(),
        context_builder=ContextBuilder(),
        store=PrivilegedPlanStore(
            tmp_path / "memory",
            user_id="u",
            project_id="p",
        ),
    )
    workflow.store.save(original)

    result = workflow.execute("priv-loop")

    assert result.kind == "paused"
    assert "没有实质差异" in result.message
    assert result.plan.replan_attempts == 1


def test_schema_v2_raw_command_migrates_to_non_executable_audit_record():
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan

    raw = {
        "schema_version": 2,
        "plan_id": "priv-old",
        "goal": "deploy",
        "risk": "medium",
        "status": "approved",
        "authorized_hash": hashlib.sha256(b"old").hexdigest(),
        "steps": [
            {
                "step_id": "legacy",
                "title": "legacy",
                "command": "klonet deploy",
                "risk": "medium",
                "status": "approved",
            }
        ],
    }

    plan = PrivilegedPlan.from_dict(raw)

    assert plan.schema_version == 3
    assert plan.authorized_hash == ""
    assert plan.steps[0].execution_binding.kind == "legacy_command"
    assert plan.steps[0].status == "blocked"
