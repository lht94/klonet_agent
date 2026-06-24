"""Agent 主编排流程。

这里承接旧版 runner.py 的职责：接收用户输入、组装上下文、调用 LLM、分发工具调用、
写入记忆和项目日志，并决定是否继续下一轮工具循环。
"""

from __future__ import annotations

import json
from time import perf_counter

from klonet_agent.agents import AgentProfile, get_profile
from klonet_agent.answer_policy import build_answer_policy
from klonet_agent.config import (
    HISTORY_MAX_MESSAGES,
    MAX_TODO_CONTINUATIONS,
    MAX_TOKEN,
    MAX_TOOL_ROUNDS,
    MEMORY_DIR,
    RAG_SEARCH_BUDGETS,
    TRACE_FILE,
)
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge import SKILL_LOADER, route_query
from klonet_agent.llm import LLMClient
from klonet_agent.memory import MemoryStore
from klonet_agent.prompts import build_system_prompts
from klonet_agent.session import AgentSession, render_todos
from klonet_agent.tools import TOOLS, ToolExecutor
from klonet_agent.tracing.logger import TraceLogger


class AgentOrchestrator:
    """Agent 的主流程控制器。

    LLMClient 负责“怎么调用模型”，ToolExecutor 负责“怎么执行工具”，
    AgentOrchestrator 只负责把这些模块按对话流程串起来。
    """

    def __init__(
        self,
        profile: AgentProfile | None = None,
        session: AgentSession | None = None,
        llm: LLMClient | None = None,
        tool_executor: ToolExecutor | None = None,
        trace_logger: TraceLogger | None = None,
        memory_store: MemoryStore | None = None,
    ):
        self.profile = profile or get_profile("mentor")
        self.session = session or AgentSession(mode=self.profile.name)
        self.llm = llm or LLMClient()
        self.trace_logger = trace_logger or TraceLogger(TRACE_FILE)
        self.memory_store = memory_store or MemoryStore.for_session(
            MEMORY_DIR,
            self.session.user_id,
            self.session.project_id,
        )
        self._query_route = route_query("Klonet")
        self._query_intent: QueryIntent | None = None
        self._knowledge_search_count = 0
        self.tool_executor = tool_executor or ToolExecutor(
            session=self.session,
            # 执行层再次检查工具权限，避免模型绕过可见工具列表。
            allowed_tools=self.profile.allowed_tools,
            trace_logger=self.trace_logger,
            memory_store=self.memory_store,
        )

    def init_history(self) -> list[dict]:
        """初始化对话记忆，包含系统提示词、记忆提示词、技能描述和任务规划规则。"""

        history = []
        # 把分层系统提示词加入上下文。Profile 决定 Mentor/Coding 的行为差异。
        for prompt in build_system_prompts(self.profile.mode_prompt):
            history.append({"role": "system", "content": prompt})

        # 把记忆设定加入到系统提示词中。
        memory_prompt = self.memory_store.memory_prompt()
        history.append({"role": "system", "content": memory_prompt})

        # 把目前已有的 skill 加入到系统提示词中。
        # 这里遵循渐进披露原则：只喂名字和描述，不直接喂完整正文，按需再通过 load_skill 提取。
        skill_prompt = f"当前已有的技能有{SKILL_LOADER.get_descriptions()}"
        history.append({"role": "system", "content": skill_prompt})

        # 把当前会话状态加入上下文，方便模型知道自己服务的是哪个用户/项目。
        session_prompt = f"""
        【当前会话】
        - mode: {self.profile.name}
        - user_id: {self.session.user_id}
        - project_id: {self.session.project_id}
        - workspace: {self.session.workspace_path}
        - journal: {self.session.journal_path}
        - workflow: {self.profile.default_workflow}
        """
        history.append({"role": "system", "content": session_prompt})

        # 载入上一次对话，即把未归档压缩的工作记忆加入上下文。
        last_history = self.memory_store.load_unarchived_history(
            max_messages=HISTORY_MAX_MESSAGES,
        )
        history.extend(last_history)

        return history

    def chat_with_llm(self, history: list[dict]):
        """调用 LLM 并返回响应。

        旧版是在 runner.py 中直接调用底层 SDK 的 chat.completions.create(...)，
        现在统一通过 LLMClient.complete() 发送请求。
        """

        start = perf_counter()
        response = self.llm.complete(messages=history, tools=self._visible_tools())
        duration_ms = int((perf_counter() - start) * 1000)
        self.trace_logger.record_llm_call(
            user_id=self.session.user_id,
            project_id=self.session.project_id,
            mode=self.session.mode,
            total_tokens=getattr(response.usage, "total_tokens", 0),
            duration_ms=duration_ms,
        )
        return response

    def use_tool(self, tool_name: str, tool_args: dict) -> str:
        """按本轮作用域和检索预算调用工具执行器。"""

        if tool_name == "search_knowledge" and self._query_route.hard_disable_rag:
            return (
                "原始用户输入已明确排除 Klonet，未执行 Klonet RAG。"
                "查询改写和模型意图不能覆盖该否定条件。"
            )
        scope = (
            self._query_intent.scope
            if self._query_intent is not None
            else self._query_route.scope
        )
        if scope == "general" and tool_name == "read_project_journal":
            return "本轮属于 generic 问题，禁止读取 Klonet 项目日志。"

        if tool_name != "search_knowledge":
            return self.tool_executor.run(tool_name, tool_args)

        budget = RAG_SEARCH_BUDGETS[scope]
        if self._knowledge_search_count >= budget:
            return (
                f"本轮 {scope} 检索预算已用完（最多 {budget} 次）。"
                "请根据已有证据完成回答，不要继续改写查询。"
            )

        self._knowledge_search_count += 1
        result = self.tool_executor.run(tool_name, tool_args)
        if scope == "general":
            return (
                "【secondary Klonet evidence】\n"
                "以下内容只能用于辅助对比，不能改变通用技术问题的主要方向：\n"
                f"{result}"
            )
        return result

    def compress_memory(self, history: list[dict], token: int):
        """触发记忆复盘与压缩。"""

        print("Klonet Agent：正在进行记忆复盘与折叠...")
        compress_instruction = """【系统强制指令 - 记忆反思折叠】
        我们当前的对话历史即将达到容量上限并被截断。为了防止你失忆，请立刻全面回顾我们刚才的新对话：
        1. 提炼出核心的技术进展、Bug 解决过程或实验结论，调用 `append_episode` 追加到今天的日记。
        2. 检查是否有项目的全局核心目标、网络架构事实改变，若有，将其与当前提示词中的长期记忆融合，调用 `write_memory` 全量更新。
        3. 评估用户的个人偏好、工作流、环境是否有变，若有，调用 `write_user` 全量更新。

        **执行规范**：
        - 请根据实际对话进展，主动、合理地触发上述工具。
        - 执行完工具后（或发现没有需要记录的新信息时），请简要说明记忆复盘已经完成，可以继续新的任务。
        """
        # 这是条静默指令。虽然 role 为 user，但它不是用户主动输入的，而是系统触发压缩用的内部指令。
        history.append({"role": "user", "content": compress_instruction})

        # 开启后台静默压缩内循环。因为压缩过程也可能调用工具，所以同样需要一层工具循环。
        while True:
            compress_response = self.chat_with_llm(history)
            token += compress_response.usage.total_tokens

            if compress_response.choices[0].message.tool_calls:
                comp_assistant = self._assistant_tool_message(
                    compress_response.choices[0].message
                )
                history.append(comp_assistant)
                self.memory_store.append_history(comp_assistant)

                # 执行归档压缩中的工具调用。
                for tool_call in compress_response.choices[0].message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)

                    tool_result = self.use_tool(tool_name, tool_args)

                    comp_tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    }
                    history.append(comp_tool_msg)
                    self.memory_store.append_history(comp_tool_msg)

                    if tool_name in ["write_memory", "write_user"]:
                        self._refresh_memory_prompt(history)
            else:
                # 压缩完成，拿到最终回复。
                compress_reply = compress_response.choices[0].message.content

                comp_assistant = {
                    "role": "assistant",
                    "content": compress_reply,
                }
                if self._has_reasoning(compress_response.choices[0].message):
                    comp_assistant["reasoning_content"] = (
                        compress_response.choices[0].message.reasoning_content
                    )

                history.append(comp_assistant)
                self.memory_store.append_history(comp_assistant)

                print(f"Klonet Agent：{compress_reply}")
                # 打入压缩标记，表示前面的内容已经被压缩归档。
                self.memory_store.append_compact_marker()
                # 重新初始化记忆，并将压缩总结加入数组，保持上下文连贯。
                history = self.init_history()
                history.append({"role": "assistant", "content": compress_reply})
                break

        return history, token

    def single_chat(self, user_input: str, history: list[dict], token: int):
        """实现一次完整的用户输入处理。

        这对应旧版 runner.py 中的内循环：
        用户输入 -> 调用 LLM -> 可能调用工具 -> 把工具结果送回 LLM -> 输出自然语言。
        """

        # 设定对话消息。消息列表中的每个消息都包含 role 和 content。
        # role 可以是 system、user、assistant、tool。
        history.append({"role": "user", "content": user_input})
        self.memory_store.append_history({"role": "user", "content": user_input})

        reply = ""
        tool_rounds = 0
        todo_continuations = 0
        self._query_route = route_query(user_input)
        self._query_intent = None
        self._knowledge_search_count = 0
        turn_scope_message = self._build_turn_scope_message(user_input)
        history.append(turn_scope_message)
        turn_answer_policy_message = None
        if self.profile.name == "mentor":
            turn_answer_policy_message = {
                "role": "system",
                "content": build_answer_policy(self._query_route.task_type, user_input),
            }
            history.append(turn_answer_policy_message)

        # 工具循环有明确上限，避免模型反复调用工具后阻塞 CLI。
        while tool_rounds < MAX_TOOL_ROUNDS:
            tool_rounds += 1
            response = self.chat_with_llm(history)
            # 记录 token 要放在外层，避免只有调用工具时才计数。
            token += response.usage.total_tokens

            # tool_calls 是本次模型决定要调用的工具集合。
            # 处理流程：模型输出标准工具参数 -> Python 执行工具 -> 工具结果输入模型 -> 模型继续判断。
            if response.choices[0].message.tool_calls:
                # 总共要记录两次记忆：模型发起了哪些工具调用、工具返回了什么。
                # 注意不能直接把复杂 SDK 对象 append 到 history，要转换成普通字典。
                assistant_msg = self._assistant_tool_message(response.choices[0].message)
                history.append(assistant_msg)
                self.memory_store.append_history(assistant_msg)

                for tool_call in response.choices[0].message.tool_calls:
                    # 工具名，即 schema 中 function.name。
                    tool_name = tool_call.function.name
                    # 工具参数，即 schema 中 function.parameters 约束出的标准 JSON。
                    tool_args = json.loads(tool_call.function.arguments)
                    if tool_name == "search_knowledge":
                        candidate_intent = QueryIntent.from_mapping(
                            tool_args.get("intent")
                        )
                        if candidate_intent.confidence >= 0.6:
                            self._query_intent = candidate_intent
                            if turn_answer_policy_message is not None:
                                turn_answer_policy_message["content"] = (
                                    build_answer_policy(
                                        candidate_intent.task_type,
                                        user_input,
                                        intent=candidate_intent,
                                    )
                                )
                    # 调用工具函数，开始执行命令或其他动作。
                    result = self.use_tool(tool_name, tool_args)

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                    history.append(tool_msg)
                    self.memory_store.append_history(tool_msg)

                    # 核心补丁：记忆同步刷新机制。
                    # 如果刚才执行的工具修改了长期记忆文件，要立刻刷新系统提示词中的记忆内容。
                    if tool_name in ["write_memory", "write_user"]:
                        self._refresh_memory_prompt(history)
                        print("Klonet Agent：长期记忆已刷新。")
            else:
                # 没有调用工具，说明本次对话进入最终自然语言回答。
                reply = response.choices[0].message.content
                print(f"Klonet Agent：{reply}")

                # 只有 Coding 模式中的可执行任务允许有限自动续跑。
                if self.session.todos:
                    actionable = [
                        todo
                        for todo in self.session.todos
                        if todo["status"] in {"pending", "in_progress"}
                    ]
                    if actionable and self.profile.name != "coding":
                        self._set_unfinished_todo_status("blocked")
                        print("Klonet Agent：当前模式无法执行这些任务，已停止自动续跑。")
                    elif actionable and todo_continuations < MAX_TODO_CONTINUATIONS:
                        todo_continuations += 1
                        print("Klonet Agent：任务列表里还有未完成项，再自动推进一次。")
                        print(render_todos(self.session.todos))
                        print()
                        continue_prompt = (
                            "以下任务仍未完成。只再推进一次；"
                            "如果仍无法完成，请将状态改为 waiting_user 或 blocked：\n"
                            + render_todos(self.session.todos)
                        )
                        history.append({"role": "user", "content": continue_prompt})
                        self.memory_store.append_history(
                            {"role": "user", "content": continue_prompt}
                        )
                        continue
                    elif actionable:
                        self._set_unfinished_todo_status("waiting_user")
                        print("Klonet Agent：已达到自动续跑上限，等待用户确认后继续。")

                    if all(
                        todo["status"] == "completed"
                        for todo in self.session.todos
                    ):
                        print("Klonet Agent：任务全部完成。")
                        print(render_todos(self.session.todos))
                        print()
                        self.session.todos.clear()
                    else:
                        print(render_todos(self.session.todos))
                        print()

                assistant_msg = {"role": "assistant", "content": reply}
                if self._has_reasoning(response.choices[0].message):
                    assistant_msg["reasoning_content"] = (
                        response.choices[0].message.reasoning_content
                    )

                history.append(assistant_msg)
                self.memory_store.append_history(assistant_msg)
                break

        else:
            reply = "本轮工具调用已达到上限，任务已暂停，等待用户确认后继续。"
            self._set_unfinished_todo_status("waiting_user")
            assistant_msg = {"role": "assistant", "content": reply}
            history.append(assistant_msg)
            self.memory_store.append_history(assistant_msg)
            print(f"Klonet Agent：{reply}")

        # 本轮作用域只约束当前用户输入，不写入长期历史，避免影响下一轮。
        history = [
            message
            for message in history
            if (
                message is not turn_scope_message
                and message is not turn_answer_policy_message
            )
        ]

        # 条件触发对话压缩。这里沿用旧版逻辑，用本轮 context token 判断。
        current_context_size = response.usage.total_tokens
        if current_context_size >= MAX_TOKEN:
            print(f"\nKlonet Agent：当前上下文约 {current_context_size} token，开始整理记忆。")
            history, token = self.compress_memory(history, token)

        return reply, history, token

    def _set_unfinished_todo_status(self, status: str):
        """暂停未完成任务，防止编排器继续自动循环。"""

        for todo in self.session.todos:
            if todo["status"] in {"pending", "in_progress"}:
                todo["status"] = status

    def _visible_tools(self) -> list[dict]:
        """根据 profile 和当前问题范围过滤模型可见工具。"""

        tools = [
            tool
            for tool in TOOLS
            if tool["function"]["name"] in self.profile.allowed_tools
        ]
        if self._query_route.scope == "general":
            tools = [
                tool
                for tool in tools
                if tool["function"]["name"] != "read_project_journal"
            ]
        if self._query_route.hard_disable_rag:
            tools = [
                tool
                for tool in tools
                if tool["function"]["name"] != "search_knowledge"
            ]
        return tools

    def _build_turn_scope_message(self, user_input: str) -> dict:
        """构建只在当前工具循环中生效的问题范围约束。"""

        route = self._query_route
        rules = [
            "【本轮问题范围】",
            f"- scope: {route.scope}",
            f"- confidence: {route.confidence}",
            f"- original_user_input: {user_input}",
            "- 本轮范围由原始用户输入确定，后续查询改写不得改变。",
        ]
        if route.hard_disable_rag:
            rules.extend(
                [
                    "- 用户明确排除了 Klonet；禁止执行 Klonet RAG。",
                    "- 查询改写、后续工具参数和模型意图都不能覆盖该否定条件。",
                    "- 只使用通用技术知识回答原始需求。",
                ]
            )
        elif route.scope == "general":
            rules.extend(
                [
                    "- 通用知识是主要依据，必须先完整回答原始技术需求。",
                    "- Klonet RAG 只能作为 secondary 辅助证据，最多检索 1 次。",
                    "- 禁止读取 Klonet 项目日志。",
                    "- 不得让 Klonet 资料取代或带偏最终回答的主要方向。",
                ]
            )
        elif route.scope == "mixed":
            rules.extend(
                [
                    "- 通用知识与 Klonet 证据需要分区回答。",
                    "- 本轮 Klonet 知识检索最多 2 次。",
                ]
            )
        else:
            rules.extend(
                [
                    "- Klonet RAG 是主要事实依据。",
                    "- 本轮 Klonet 知识检索最多 2 次。",
                ]
            )
        return {"role": "system", "content": "\n".join(rules)}

    def _refresh_memory_prompt(self, history: list[dict]):
        """刷新上下文里的记忆系统提示词。"""

        for msg in history:
            if msg.get("role") == "system" and "MEMORY.md" in msg.get("content", ""):
                msg["content"] = self.memory_store.memory_prompt()
                return

    def _assistant_tool_message(self, message) -> dict:
        """把 SDK 返回的 assistant message 转成普通字典，方便写入 history。"""

        assistant_msg = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                # 列表推导式：for 循环写在后面，遍历 tool_calls 并生成新列表。
                for tool_call in message.tool_calls
            ],
        }
        # DeepSeek 支持 reasoning_content。
        # 当模型进行复杂推理时，下一轮请求需要把 reasoning_content 原样传回去，否则可能报错。
        if self._has_reasoning(message):
            assistant_msg["reasoning_content"] = message.reasoning_content
        return assistant_msg

    def _has_reasoning(self, message) -> bool:
        """判断响应消息里是否有 reasoning_content 字段。"""

        return bool(getattr(message, "reasoning_content", None))
