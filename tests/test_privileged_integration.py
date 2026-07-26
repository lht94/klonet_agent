from __future__ import annotations

from types import SimpleNamespace

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


def test_orchestrator_sends_every_ops_privilege_turn_to_supervisor_first():
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
    import sys

    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent
    from klonet_agent.ops.privileged.store import PrivilegedPlanStore
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow import PrivilegedOpsWorkflow

    target = tmp_path / "result.txt"
    command = (
        '"%s" -c "from pathlib import Path; Path(r\'%s\').write_text(\'ready\')"'
        % (sys.executable, target)
    )

    class SequentialLLM:
        def __init__(self):
            self.responses = [
                json.dumps(
                    {
                        "goal": "create result",
                        "risk": "low",
                        "steps": [
                            {
                                "step_id": "create-result",
                                "title": "create result",
                                "command": command,
                                "risk": "low",
                                "postconditions": [
                                    {
                                        "checker": "file_exists",
                                        "args": {"path": str(target)},
                                    }
                                ],
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "status": "passed",
                        "goal_achieved": True,
                        "reason": "file exists",
                        "next_action": "",
                    }
                ),
            ]

        def complete(self, messages, tools=None):
            del messages, tools
            content = self.responses.pop(0)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    llm = SequentialLLM()
    store = PrivilegedPlanStore(tmp_path / "memory", user_id="u", project_id="p")
    workflow = PrivilegedOpsWorkflow(
        planner=PrivilegedPlannerAgent(llm),
        executor=PrivilegedCommandExecutor(),
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
