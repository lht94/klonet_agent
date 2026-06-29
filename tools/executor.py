"""工具执行分发器。

这里根据模型返回的 tool_name 和 arguments 调用对应工具函数，并统一包装执行结果。
后续可以在这里加入权限检查、错误处理、执行日志和危险命令拦截。
"""

from __future__ import annotations

import os
from time import perf_counter

from klonet_agent.config import DEFAULT_RAG_TOP_K
from klonet_agent.journal import ProjectJournal
from klonet_agent.knowledge.conversation_state import ConversationState
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.memory import MEMORY_STORE, MemoryStore
from klonet_agent.session import AgentSession
from klonet_agent.tools.file_ops import list_files, read_file, write_file
from klonet_agent.tools.environment import (
    inspect_screen_session,
    inspect_klonet_runtime,
    inspect_ops_context,
    inspect_system_environment,
    read_klonet_logs,
    read_ops_file,
)
from klonet_agent.tools.shell import run_command_linux, run_command_win, run_tests
from klonet_agent.tools.source_code import (
    list_source_files,
    read_source_file,
    search_code,
)
from klonet_agent.tools.web import web_fetch
from klonet_agent.tracing.logger import TraceLogger
from klonet_agent.workspace.git_ops import show_diff
from klonet_agent.workspace.manager import WORKSPACE_MANAGER


TOOL_RESULT_MAX_CHARS = 12000
KNOWLEDGE_BASE = None
SKILL_LOADER = None


class ToolExecutor:
    """统一的工具执行入口。

    大模型只会输出工具名和参数；真正调用 Python 函数、读写记忆、执行命令的逻辑都在这里。
    """

    def __init__(
        self,
        session: AgentSession | None = None,
        allowed_tools: set[str] | None = None,
        trace_logger: TraceLogger | None = None,
        memory_store: MemoryStore | None = None,
    ):
        self.session = session or AgentSession()
        self.allowed_tools = allowed_tools
        self.trace_logger = trace_logger
        self.memory_store = memory_store or MEMORY_STORE

    def run(self, tool_name: str, tool_args: dict) -> str:
        """执行一个工具调用，并返回给大模型看的文本结果。"""

        start = perf_counter()
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            result = f"Error: 当前 Agent 模式不允许调用工具 {tool_name}"
            self._record_trace(tool_name, tool_args, "denied", start, result)
            return result

        try:
            result = self._run_allowed_tool(tool_name, tool_args)
        except Exception as exc:
            result = f"Error: {exc}"
            self._record_trace(tool_name, tool_args, "error", start, result)
            return result

        result = _truncate_tool_result(result)
        status = "error" if result.startswith("Error:") else "success"
        self._record_trace(tool_name, tool_args, status, start, result)
        return result

    def _run_allowed_tool(self, tool_name: str, tool_args: dict) -> str:
        """执行已经通过权限检查的工具。"""

        if tool_name == "run_command":
            sandbox = WORKSPACE_MANAGER.sandbox_for(self.session)
            reason = sandbox.reject_dangerous_command(tool_args["command"])
            if reason:
                return f"Error: {reason}"
            print(f"Klonet Agent：正在执行命令\n{tool_args['command']}")
            if os.name == "nt":
                result = run_command_win(tool_args["command"])
            else:
                result = run_command_linux(tool_args["command"])
            print(f"Klonet Agent：命令执行完成，结果为：\n{result}")
            return result

        if tool_name == "load_skill":
            print(f"Klonet Agent：正在加载技能 {tool_args['skill_name']}。")
            return _skill_loader().load_skill(tool_args["skill_name"])

        if tool_name == "search_knowledge":
            intent = QueryIntent.from_mapping(tool_args.get("intent"))
            conversation_state = ConversationState.from_mapping(
                tool_args.get("conversation_state")
            )
            return _knowledge_base().search_knowledge(
                tool_args["query"],
                tool_args.get("top_k", DEFAULT_RAG_TOP_K),
                task_type=tool_args.get("task_type"),
                layers=tuple(tool_args["layers"]) if tool_args.get("layers") else None,
                domains=tuple(tool_args["domains"]) if tool_args.get("domains") else None,
                min_priority=tool_args.get("min_priority"),
                intent=intent,
                conversation_state=conversation_state,
            )

        if tool_name == "inspect_system_environment":
            return inspect_system_environment(tool_args)

        if tool_name == "inspect_ops_context":
            result = inspect_ops_context(tool_args)
            if "## baseline" in result:
                self.memory_store.write_shared_ops_baseline(result)
            return result

        if tool_name == "inspect_klonet_runtime":
            return inspect_klonet_runtime(tool_args)

        if tool_name == "read_klonet_logs":
            return read_klonet_logs(tool_args)

        if tool_name == "read_ops_file":
            return read_ops_file(tool_args)

        if tool_name == "inspect_screen_session":
            return inspect_screen_session(tool_args)

        if tool_name == "search_shared_ops_memory":
            return self.memory_store.search_shared_memory(
                tool_args["query"],
                tool_args.get("max_results", 5),
            )

        if tool_name == "list_files":
            return list_files(self.session, tool_args.get("path", "."))

        if tool_name == "read_file":
            return read_file(
                self.session,
                tool_args["path"],
                tool_args.get("max_chars", 12000),
            )

        if tool_name == "search_code":
            return search_code(
                tool_args["query"],
                path=tool_args.get("path", ""),
                file_glob=tool_args.get("file_glob"),
                max_results=tool_args.get("max_results", 50),
                case_sensitive=tool_args.get("case_sensitive", False),
            )

        if tool_name == "read_source_file":
            return read_source_file(
                tool_args["path"],
                start_line=tool_args.get("start_line"),
                end_line=tool_args.get("end_line"),
                max_chars=tool_args.get("max_chars", 12000),
            )

        if tool_name == "list_source_files":
            return list_source_files(
                path=tool_args.get("path", ""),
                pattern=tool_args.get("pattern"),
                max_results=tool_args.get("max_results", 200),
            )

        if tool_name == "write_file":
            return write_file(self.session, tool_args["path"], tool_args["content"])

        if tool_name == "run_tests":
            return run_tests(self.session, tool_args.get("command", "pytest -q"))

        if tool_name == "show_diff":
            WORKSPACE_MANAGER.ensure_workspace(self.session)
            return show_diff(self.session.workspace_path)

        if tool_name == "append_episode":
            print("Klonet Agent：正在记录本次有价值的进展。")
            self.memory_store.append_episode(tool_args["content"])
            return "已成功追加到今天的情景记忆日志中。"

        if tool_name == "write_memory":
            print("Klonet Agent：正在更新长期记忆。")
            self.memory_store.write_memory(tool_args["content"])
            return "长期记忆 MEMORY.md 已被成功覆盖更新。"

        if tool_name == "write_user":
            print("Klonet Agent：正在更新用户画像。")
            self.memory_store.write_user(tool_args["content"])
            return "用户偏好 USER.md 已被成功覆盖更新。"

        if tool_name == "web_fetch":
            return web_fetch(
                tool_args["url"],
                tool_args.get("extract_mode", "text"),
                tool_args.get("max_chars", 8000),
            )

        if tool_name == "update_todos":
            print("Klonet Agent：正在更新任务计划。")
            return self.session.update_todos(tool_args["todos"])

        journal = ProjectJournal.from_session(self.session)

        if tool_name == "create_project_journal":
            path = journal.ensure(tool_args.get("goal"))
            return f"项目日志已创建或确认存在：{path}"

        if tool_name == "read_project_journal":
            max_chars = tool_args.get("max_chars", 3000)
            if max_chars:
                return journal.summary(max_chars=max_chars)
            return journal.read()

        if tool_name == "append_journal_event":
            return journal.append_event(tool_args["section"], tool_args["content"])

        if tool_name == "update_project_status":
            return journal.update_status(tool_args["status"])

        if tool_name == "record_test_result":
            return journal.record_test_result(tool_args["content"])

        if tool_name == "record_acceptance_gap":
            return journal.record_acceptance_gap(tool_args["content"])

        return f"Error:Unknown tool {tool_name}"

    def _record_trace(
        self,
        tool_name: str,
        tool_args: dict,
        status: str,
        start: float,
        result: str,
    ):
        """如果配置了 trace logger，就记录本次工具调用。"""

        if self.trace_logger is None:
            return
        duration_ms = int((perf_counter() - start) * 1000)
        self.trace_logger.record_tool_call(
            user_id=self.session.user_id,
            project_id=self.session.project_id,
            mode=self.session.mode,
            tool_name=tool_name,
            status=status,
            duration_ms=duration_ms,
            args=tool_args,
            result=result,
        )


def _truncate_tool_result(result: str, max_chars: int = TOOL_RESULT_MAX_CHARS) -> str:
    """统一截断过长工具结果，保护上下文窗口。"""

    if len(result) <= max_chars:
        return result
    suffix = "\n\n...（工具结果过长，已截断）"
    return result[: max_chars - len(suffix)].rstrip() + suffix


def _knowledge_base():
    """Delay heavy RAG initialization until a knowledge search is actually needed."""

    global KNOWLEDGE_BASE
    if KNOWLEDGE_BASE is None:
        from klonet_agent.knowledge import KNOWLEDGE_BASE as DEFAULT_KNOWLEDGE_BASE

        KNOWLEDGE_BASE = DEFAULT_KNOWLEDGE_BASE
    return KNOWLEDGE_BASE


def _skill_loader():
    """Delay skill loader construction for CLI/help paths that never load skills."""

    global SKILL_LOADER
    if SKILL_LOADER is None:
        from klonet_agent.knowledge import SKILL_LOADER as DEFAULT_SKILL_LOADER

        SKILL_LOADER = DEFAULT_SKILL_LOADER
    return SKILL_LOADER
