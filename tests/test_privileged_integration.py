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
        self.calls.append((text, environment_context))
        return SimpleNamespace(
            handled=True,
            status="completed",
            message="privileged supervisor completed",
        )


class NoCallLLM:
    def complete(self, *args, **kwargs):
        raise AssertionError("main LLM loop must not receive privileged execution requests")


class DelegatingSupervisor:
    def __init__(self):
        self.calls = []

    def handle(self, text, environment_context=""):
        self.calls.append((text, environment_context))
        return SimpleNamespace(handled=False, status="conversation", message="")


class ContextCapturingSupervisor:
    def __init__(self):
        self.calls = []

    def handle_with_context(
        self,
        text,
        environment_context="",
        conversation_context="",
    ):
        self.calls.append((text, environment_context, conversation_context))
        return SimpleNamespace(
            handled=True,
            status="clarification",
            message="context captured",
        )


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


def test_orchestrator_builds_v4_as_the_only_privileged_runtime():
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
        )

    assert not hasattr(orchestrator, "privileged_workflow_version")
    assert isinstance(orchestrator.privileged_supervisor, PrivilegedOpsV4Coordinator)
    assert orchestrator.privileged_workflow is orchestrator.privileged_supervisor.mutation_workflow


def test_v4_readonly_turn_runs_through_staged_runtime(capsys):
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
        )
        reply, _, _ = orchestrator.single_chat("inspect platforms", [], 0)

    assert reply == "V4 readonly response"
    assert "工作流协调器：V4 readonly response" in capsys.readouterr().out


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
        "工作流协调器：privileged supervisor completed"
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
