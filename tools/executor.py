"""工具执行分发器。

这里根据模型返回的 tool_name 和 arguments 调用对应工具函数，并统一包装执行结果。
后续可以在这里加入权限检查、错误处理、执行日志和危险命令拦截。
"""

from __future__ import annotations

import os

from klonet_agent.journal import ProjectJournal
from klonet_agent.knowledge import KNOWLEDGE_BASE, SKILL_LOADER
from klonet_agent.memory import MEMORY_STORE
from klonet_agent.session import AgentSession
from klonet_agent.tools.file_ops import list_files, read_file, write_file
from klonet_agent.tools.shell import run_command_linux, run_command_win, run_tests
from klonet_agent.tools.web import web_fetch
from klonet_agent.workspace.git_ops import show_diff
from klonet_agent.workspace.manager import WORKSPACE_MANAGER


class ToolExecutor:
    """统一的工具执行入口。

    大模型只会输出工具名和参数；真正调用 Python 函数、读写记忆、执行命令的逻辑都在这里。
    """

    def __init__(self, session: AgentSession | None = None, allowed_tools: set[str] | None = None):
        self.session = session or AgentSession()
        self.allowed_tools = allowed_tools

    def run(self, tool_name: str, tool_args: dict) -> str:
        """执行一个工具调用，并返回给大模型看的文本结果。"""

        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            return f"Error: 当前 Agent 模式不允许调用工具 {tool_name}"

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
            return SKILL_LOADER.load_skill(tool_args["skill_name"])

        if tool_name == "search_knowledge":
            return KNOWLEDGE_BASE.search_knowledge(
                tool_args["query"],
                tool_args.get("top_k", 5),
            )

        if tool_name == "list_files":
            return list_files(self.session, tool_args.get("path", "."))

        if tool_name == "read_file":
            return read_file(
                self.session,
                tool_args["path"],
                tool_args.get("max_chars", 12000),
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
            MEMORY_STORE.append_episode(tool_args["content"])
            return "已成功追加到今天的情景记忆日志中。"

        if tool_name == "write_memory":
            print("Klonet Agent：正在更新长期记忆。")
            MEMORY_STORE.write_memory(tool_args["content"])
            return "长期记忆 MEMORY.md 已被成功覆盖更新。"

        if tool_name == "write_user":
            print("Klonet Agent：正在更新用户画像。")
            MEMORY_STORE.write_user(tool_args["content"])
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
