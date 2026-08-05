from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.helpers import local_temp_dir


def test_ops_privilege_model_visible_tools_exclude_all_mutating_execution_paths():
    from klonet_agent.agents import get_profile
    from klonet_agent.tools.registry import TOOLS

    profile = get_profile("ops-privilege")
    registered = {item["function"]["name"] for item in TOOLS}

    assert "run_privileged_command" not in registered
    assert "run_privileged_command" not in profile.allowed_tools
    assert "create_ops_operation_plan" not in profile.allowed_tools
    assert "execute_ops_operation_step" not in profile.allowed_tools
    assert "write_file" not in profile.allowed_tools
    assert "run_readonly_command" in profile.allowed_tools


def test_tool_executor_cannot_be_used_as_raw_privileged_shell_escape():
    from klonet_agent.session import AgentSession
    from klonet_agent.tools.executor import ToolExecutor

    executor = ToolExecutor(
        session=AgentSession(mode="ops-privilege"),
        allowed_tools={"run_privileged_command"},
    )

    result = executor.run(
        "run_privileged_command",
        {"command": "sudo systemctl restart nginx"},
    )

    assert result.startswith("Error:")
    assert "not registered" in result


class StubPrivilegedSupervisor:
    def __init__(self):
        self.calls = []

    def handle(self, text, environment_context=""):
        from klonet_agent.ops.privileged.supervisor import SupervisorResult

        self.calls.append((text, environment_context))
        return SupervisorResult(True, "completed", "privileged supervisor completed")


class NoCallLLM:
    def complete(self, *args, **kwargs):
        raise AssertionError("main LLM loop must not receive privileged execution requests")


class DelegatingSupervisor:
    def __init__(self):
        self.calls = []

    def handle(self, text, environment_context=""):
        from klonet_agent.ops.privileged.supervisor import SupervisorResult

        self.calls.append((text, environment_context))
        return SupervisorResult(False, "conversation")


class ContextCapturingSupervisor:
    def __init__(self):
        self.calls = []

    def handle_with_context(
        self,
        text,
        environment_context="",
        conversation_context="",
    ):
        from klonet_agent.ops.privileged.supervisor import SupervisorResult

        self.calls.append((text, environment_context, conversation_context))
        return SupervisorResult(True, "clarification", "context captured")


class AnswerLLM:
    def __init__(self):
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="ordinary ops answer",
                        tool_calls=None,
                    )
                )
            ],
            usage=SimpleNamespace(total_tokens=7),
        )


def test_orchestrator_builds_explicit_v4_runtime_without_v3_fallback():
    from klonet_agent.agents import get_profile
    from klonet_agent.memory import MemoryStore
    from klonet_agent.ops.privileged.v4.coordinator import PrivilegedOpsV4Coordinator
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    with local_temp_dir() as temp_dir:
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops-privilege"),
            session=AgentSession(user_id="u", project_id="p", mode="ops-privilege"),
            llm=NoCallLLM(),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u", "p"),
            privileged_workflow_version="v4",
        )

    assert orchestrator.privileged_workflow_version == "v4"
    assert isinstance(orchestrator.privileged_supervisor, PrivilegedOpsV4Coordinator)
    assert orchestrator.privileged_workflow is orchestrator.privileged_supervisor.mutation_workflow


def test_orchestrator_rejects_unknown_privileged_runtime_instead_of_falling_back():
    from klonet_agent.agents import get_profile
    from klonet_agent.memory import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    with local_temp_dir() as temp_dir, pytest.raises(ValueError, match="v3 or v4"):
        AgentOrchestrator(
            profile=get_profile("ops-privilege"),
            session=AgentSession(user_id="u", project_id="p", mode="ops-privilege"),
            llm=NoCallLLM(),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u", "p"),
            privileged_workflow_version="automatic",
        )


def test_explicit_v4_readonly_turn_runs_through_staged_runtime(capsys):
    import json

    from klonet_agent.agents import get_profile
    from klonet_agent.memory import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    class QueueLLM:
        def __init__(self):
            self.outputs = [
                json.dumps(
                    {
                        "intent": "readonly_action",
                        "goal_clarity": "clear",
                        "command": "",
                        "confidence": 1,
                        "reason": "inspection",
                        "clarification_question": "",
                        "plan_reference": "",
                    }
                ),
                json.dumps({"status": "ready"}),
                json.dumps(
                    {
                        "confirmed_facts": [],
                        "uncertainties": [],
                        "missing_decisions": [],
                    }
                ),
                "V4 readonly response",
            ]

        def complete(self, messages, tools=None, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=self.outputs.pop(0), tool_calls=None)
                    )
                ],
                usage=SimpleNamespace(total_tokens=1),
            )

    with local_temp_dir() as temp_dir:
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops-privilege"),
            session=AgentSession(user_id="u", project_id="p", mode="ops-privilege"),
            llm=QueueLLM(),
            memory_store=MemoryStore.for_session(temp_dir / "memory", "u", "p"),
            privileged_workflow_version="v4",
        )
        reply, _, _ = orchestrator.single_chat("inspect platforms", [], 0)

    assert reply == "V4 readonly response"
    assert "Workflow Coordinator：V4 readonly response" in capsys.readouterr().out


def test_orchestrator_sends_every_ops_privilege_turn_to_supervisor_first(capsys):
    from klonet_agent.agents import get_profile
    from klonet_agent.memory import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    supervisor = StubPrivilegedSupervisor()
    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u", project_id="p", mode="ops-privilege")
        memory = MemoryStore.for_session(temp_dir / "memory", "u", "p")
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops-privilege"),
            session=session,
            llm=NoCallLLM(),
            memory_store=memory,
            privileged_supervisor=supervisor,
        )
        reply, history, token = orchestrator.single_chat(
            "请重启 nginx 服务",
            [],
            0,
        )

    assert reply == "privileged supervisor completed"
    assert supervisor.calls == [("请重启 nginx 服务", "")]
    assert history[-1] == {"role": "assistant", "content": reply}
    assert token == 0
    assert (
        "Workflow Coordinator：privileged supervisor completed"
        in capsys.readouterr().out
    )


def test_orchestrator_returns_handled_supervisor_result_before_main_llm():
    from klonet_agent.agents import get_profile
    from klonet_agent.memory import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    supervisor = StubPrivilegedSupervisor()
    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u", project_id="p", mode="ops-privilege")
        memory = MemoryStore.for_session(temp_dir / "memory", "u", "p")
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops-privilege"),
            session=session,
            llm=NoCallLLM(),
            memory_store=memory,
            privileged_supervisor=supervisor,
        )
        reply, _, _ = orchestrator.single_chat("show-priv priv-123", [], 0)

    assert reply == "privileged supervisor completed"
    assert supervisor.calls == [("show-priv priv-123", "")]


def test_orchestrator_passes_recent_dialogue_to_privileged_classifier():
    from klonet_agent.agents import get_profile
    from klonet_agent.memory import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    supervisor = ContextCapturingSupervisor()
    history = [
        {"role": "system", "content": "internal"},
        {"role": "user", "content": "检查 nginx"},
        {"role": "assistant", "content": "nginx 当前未运行"},
    ]
    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u", project_id="p", mode="ops-privilege")
        memory = MemoryStore.for_session(temp_dir / "memory", "u", "p")
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops-privilege"),
            session=session,
            llm=NoCallLLM(),
            memory_store=memory,
            privileged_supervisor=supervisor,
        )
        reply, _, _ = orchestrator.single_chat("重启它", history, 0)

    assert reply == "context captured"
    assert supervisor.calls[0][0] == "重启它"
    assert supervisor.calls[0][1] == ""
    assert "nginx 当前未运行" in supervisor.calls[0][2]
    assert "internal" not in supervisor.calls[0][2]


def test_orchestrator_continues_to_answerer_when_supervisor_delegates_conversation():
    from klonet_agent.agents import get_profile
    from klonet_agent.memory import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession

    supervisor = DelegatingSupervisor()
    llm = AnswerLLM()
    with local_temp_dir() as temp_dir:
        session = AgentSession(user_id="u", project_id="p", mode="ops-privilege")
        memory = MemoryStore.for_session(temp_dir / "memory", "u", "p")
        orchestrator = AgentOrchestrator(
            profile=get_profile("ops-privilege"),
            session=session,
            llm=llm,
            memory_store=memory,
            privileged_supervisor=supervisor,
        )
        reply, history, _ = orchestrator.single_chat(
            "什么是 tc qdisc？",
            [],
            0,
        )

    assert supervisor.calls == [("什么是 tc qdisc？", "")]
    assert reply == "ordinary ops answer"
    assert history[-1] == {"role": "assistant", "content": reply}
    assert llm.calls


def test_trace_logger_records_privileged_lifecycle_event(tmp_path):
    import json

    from klonet_agent.tracing.logger import TraceLogger

    path = tmp_path / "trace.jsonl"
    logger = TraceLogger(path)

    logger.record_privileged_event(
        user_id="u",
        project_id="p",
        mode="ops-privilege",
        event="privileged_plan_created",
        payload={"plan_id": "priv-123", "risk": "medium"},
    )

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["event"] == "privileged_plan_created"
    assert row["plan_id"] == "priv-123"
    assert row["risk"] == "medium"


def test_end_to_end_supervisor_executes_and_verifies_a_confirmed_plan(tmp_path):
    import json

    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.execution_agent import (
        PrivilegedExecutionAgent,
    )
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    target = tmp_path / "result.txt"

    class SequentialLLM:
        def __init__(self):
            self.responses = [
                json.dumps(
                        {
                            "status": "ready",
                            "goal": "create result",
                            "risk": "low",
                            "steps": [
                                {
                                    "step_id": "create-result",
                                    "title": "create result",
                                    "objective": "create the requested result file",
                                    "reason": "the user requested a durable result",
                                    "success_criteria": ["the result file exists"],
                                    "risk_suggestion": "low",
                                }
                            ],
                        }
                    ),
                json.dumps(
                        {
                            "status": "registered_action",
                            "action": "write_ops_file",
                            "selection_reason": "registered writer covers objective",
                        }
                    ),
                json.dumps(
                        {
                            "status": "ready",
                            "args": {
                                "path": str(target),
                                "content": "ready",
                            },
                            "binding_reason": "registered file writer covers the objective",
                            "postconditions": [
                                {
                                    "checker": "file_exists",
                                    "args": {"path": str(target)},
                                }
                            ],
                        }
                    ),
            ]

        def complete(self, messages, tools=None, **kwargs):
            del messages, kwargs
            content = self.responses.pop(0)
            if tools:
                message = SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            function=SimpleNamespace(
                                name=tools[0]["function"]["name"],
                                arguments=content,
                            )
                        )
                    ],
                )
            else:
                message = SimpleNamespace(content=content, tool_calls=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)]
            )

    llm = SequentialLLM()
    store = PrivilegedPlanStore(tmp_path / "memory", user_id="u", project_id="p")

    def action_runner(plan, step):
        from klonet_agent.ops.operations import RecipeExecutionResult

        del plan
        target.write_text(step.args["content"], encoding="utf-8")
        return RecipeExecutionResult(
            "completed",
            "environment_changed=true",
        )

    workflow = PrivilegedOpsWorkflow(
        planner=PrivilegedPlannerAgent(llm),
        execution_agent=PrivilegedExecutionAgent(
            llm,
            enable_implementation_plans=False,
        ),
        executor=PrivilegedCommandExecutor(action_runner=action_runner),
        verifier=PrivilegedVerifierAgent(llm),
        store=store,
    )

    waiting = workflow.submit("create result")
    completed = workflow.handle_command(
        "confirm-priv %s" % waiting.plan.plan_id
    )

    assert waiting.kind == "awaiting_confirmation"
    assert completed.kind == "completed"
    assert target.read_text(encoding="utf-8") == "ready"
    assert store.load(waiting.plan.plan_id).steps[0].checks[0].status == "passed"
