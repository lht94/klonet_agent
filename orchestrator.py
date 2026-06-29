"""Agent 主编排流程。

这里承接旧版 runner.py 的职责：接收用户输入、组装上下文、调用 LLM、分发工具调用、
写入记忆和项目日志，并决定是否继续下一轮工具循环。
"""

from __future__ import annotations

import json
from time import perf_counter
from types import SimpleNamespace

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
from klonet_agent.knowledge.clarification import (
    decide_model_intent_clarification,
    decide_pre_llm_clarification,
)
from klonet_agent.knowledge.conversation_state import (
    ConversationState,
    ConversationStateManager,
)
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.intent_analyzer import IntentAnalyzer, route_from_intent
from klonet_agent.knowledge.semantic_understanding import IntentDecision
from klonet_agent.knowledge.turn_intent import (
    TurnDecision,
    TurnDecisionPlanner,
    TurnIntent,
    TurnIntentBuilder,
)
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
        intent_analyzer: IntentAnalyzer | None = None,
        answer_style: str = "default",
    ):
        self.profile = profile or get_profile("mentor")
        self.session = session or AgentSession(mode=self.profile.name)
        self.llm = llm or LLMClient()
        self.answer_style = answer_style
        self.intent_analyzer = intent_analyzer or IntentAnalyzer(self.llm)
        self.trace_logger = trace_logger or TraceLogger(TRACE_FILE)
        self.memory_store = memory_store or MemoryStore.for_session(
            MEMORY_DIR,
            self.session.user_id,
            self.session.project_id,
        )
        self._query_route = route_query("Klonet")
        self._query_intent: QueryIntent | None = None
        self._intent_decision: IntentDecision | None = None
        self._turn_intent: TurnIntent | None = None
        self._turn_decision: TurnDecision | None = None
        self._turn_intent_builder = TurnIntentBuilder()
        self._turn_decision_planner = TurnDecisionPlanner()
        self._conversation_state = ConversationState()
        self._conversation_state_manager = ConversationStateManager()
        self._knowledge_search_count = 0
        self._paused_turn_state: dict | None = None
        self._last_turn_state: dict | None = None
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

    def chat_with_llm(
        self,
        history: list[dict],
        *,
        stream: bool = False,
        on_delta=None,
    ):
        """调用 LLM 并返回响应。

        旧版是在 runner.py 中直接调用底层 SDK 的 chat.completions.create(...)，
        现在统一通过 LLMClient.complete() 发送请求。
        """

        start = perf_counter()
        response = self._complete_llm(history, stream=stream)
        if stream and not self._is_complete_response(response):
            response = self._collect_stream_response(response, on_delta=on_delta)
        duration_ms = int((perf_counter() - start) * 1000)
        self.trace_logger.record_llm_call(
            user_id=self.session.user_id,
            project_id=self.session.project_id,
            mode=self.session.mode,
            total_tokens=getattr(response.usage, "total_tokens", 0),
            duration_ms=duration_ms,
        )
        return response

    def _complete_llm(self, history: list[dict], *, stream: bool):
        tools = self._visible_tools()
        if not stream:
            return self.llm.complete(messages=history, tools=tools)
        try:
            return self.llm.complete(messages=history, tools=tools, stream=True)
        except TypeError as exc:
            if "stream" not in str(exc):
                raise
            return self.llm.complete(messages=history, tools=tools)
        except Exception as exc:
            if not self._is_timeout_error(exc):
                raise
            return self.llm.complete(messages=history, tools=tools)

    def _is_complete_response(self, response) -> bool:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return False
        return hasattr(choices[0], "message")

    def _is_timeout_error(self, exc: Exception) -> bool:
        error_type = exc.__class__.__name__.lower()
        error_module = exc.__class__.__module__.lower()
        message = str(exc).lower()
        return (
            "timeout" in error_type
            or "timeout" in message
            or error_type == "apitimeouterror"
            or ("openai" in error_module and "timeout" in error_type)
        )

    def _collect_stream_response(self, stream, *, on_delta=None):
        content_parts: list[str] = []
        tool_call_parts: dict[int, dict] = {}
        total_tokens = 0
        finish_reason = None

        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            chunk_tokens = getattr(usage, "total_tokens", 0) if usage is not None else 0
            if chunk_tokens:
                total_tokens = chunk_tokens

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            content = getattr(delta, "content", None)
            if content:
                content_parts.append(content)
                if on_delta is not None:
                    on_delta(content)

            for tool_call in getattr(delta, "tool_calls", None) or []:
                index = getattr(tool_call, "index", 0) or 0
                current = tool_call_parts.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                tool_id = getattr(tool_call, "id", None)
                if tool_id:
                    current["id"] = tool_id

                function = getattr(tool_call, "function", None)
                if function is None:
                    continue
                name = getattr(function, "name", None)
                if name:
                    current["name"] += name
                arguments = getattr(function, "arguments", None)
                if arguments:
                    current["arguments"] += arguments

        tool_calls = [
            SimpleNamespace(
                id=part["id"],
                function=SimpleNamespace(
                    name=part["name"],
                    arguments=part["arguments"],
                ),
            )
            for _, part in sorted(tool_call_parts.items())
        ]
        message = SimpleNamespace(
            content="".join(content_parts),
            tool_calls=tool_calls or None,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
            usage=SimpleNamespace(total_tokens=total_tokens),
        )

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
        recent_history_for_intent = self._recent_dialogue_history(history)
        history.append({"role": "user", "content": user_input})
        self.memory_store.append_history({"role": "user", "content": user_input})

        reply = ""
        tool_rounds = 0
        todo_continuations = 0
        tool_events: list[dict] = []
        reasoning_trace_printed = False
        self._query_intent = None
        self._intent_decision = None
        self._turn_intent = None
        self._turn_decision = None
        self._knowledge_search_count = 0
        semantic_frame = None
        resume_state = self._resume_state_for(user_input)
        resume_paused_turn = (
            resume_state is not None
            and resume_state is self._paused_turn_state
        )
        resume_previous_turn = resume_state is not None
        effective_user_input = user_input
        turn_resume_message = None

        thinking_prompt = "Klonet Agent\uff1a\u6b63\u5728\u601d\u8003..."
        thinking_visible = False
        if self._show_visible_reasoning_trace():
            thinking_visible = True
            print(thinking_prompt, end="", flush=True)

        def clear_thinking_prompt():
            nonlocal thinking_visible
            if not thinking_visible:
                return
            print("\r\033[2K", end="", flush=True)
            thinking_visible = False

        def print_reasoning_trace_once():
            nonlocal reasoning_trace_printed
            if reasoning_trace_printed or not self._show_visible_reasoning_trace():
                return
            clear_thinking_prompt()
            print(self._render_visible_reasoning_trace(tool_events))
            reasoning_trace_printed = True

        def print_progress(message: str):
            if not self._show_progress_updates():
                return
            clear_thinking_prompt()
            print(f"Klonet Agent：{message}")

        if resume_previous_turn:
            state = resume_state or {}
            effective_user_input = str(state.get("original_user_input") or user_input)
            self._query_intent = state.get("intent")
            self._intent_decision = state.get("decision")
            self._query_route = state.get("route") or route_query(effective_user_input)
            self._conversation_state = state.get("conversation_state") or ConversationState()
            self._refresh_turn_plan(
                user_input,
                recent_history=recent_history_for_intent,
                resume_state=state,
                effective_user_input=effective_user_input,
            )
            turn_resume_message = {
                "role": "system",
                "content": (
                    "【恢复上一轮暂停任务】\n"
                    f"- original_user_input: {effective_user_input}\n"
                    "- 当前用户输入是继续上一轮，不是新的部署/安装问题。\n"
                    "- 禁止重新追问“首次安装环境还是启动平台服务”。\n"
                    "- 请基于已有工具结果继续回答；如果证据已经足够，直接给阶段性结论。"
                ),
            }
            history.append(turn_resume_message)
        else:
            pre_decision = (
                decide_pre_llm_clarification(user_input)
                if self.profile.name == "mentor"
                else None
            )
            if pre_decision is not None and pre_decision.should_stop:
                reply = pre_decision.reply
                assistant_msg = {"role": "assistant", "content": reply}
                history.append(assistant_msg)
                self.memory_store.append_history(assistant_msg)
                clear_thinking_prompt()
                print(f"Klonet Agent\uff1a{reply}")
                return reply, history, token

        if self.profile.name == "mentor" and not resume_previous_turn:
            try:
                print_progress("正在理解你的问题...")
                analysis = self.intent_analyzer.analyze(
                    user_input,
                    recent_history=recent_history_for_intent,
                )
                token += analysis.token_usage
                self._query_intent = analysis.intent
                self._intent_decision = analysis.decision
                self._conversation_state = self._conversation_state_manager.from_turn(
                    user_input,
                    recent_history=recent_history_for_intent,
                    semantic_frame=analysis.semantic_frame,
                    intent=analysis.intent,
                    decision=analysis.decision,
                    previous_state=self._conversation_state,
                )
                self._query_route = route_from_intent(user_input, analysis.intent)
                semantic_frame = analysis.semantic_frame
                self._refresh_turn_plan(
                    user_input,
                    recent_history=recent_history_for_intent,
                    semantic_frame=semantic_frame,
                )
                print_progress(self._progress_intent_summary())
            except Exception:
                self._query_route = route_query(user_input)
                self._query_intent = None
                self._intent_decision = None
                self._refresh_turn_plan(
                    user_input,
                    recent_history=recent_history_for_intent,
                )
                print_progress(self._progress_intent_summary())
        elif not resume_previous_turn:
            self._query_route = route_query(user_input)
            self._refresh_turn_plan(
                user_input,
                recent_history=recent_history_for_intent,
            )
            print_progress(self._progress_intent_summary())

        if (
            self.profile.name == "mentor"
            and self._query_intent is not None
            and not resume_previous_turn
        ):
            if self._turn_decision is not None:
                clarification = self._turn_decision.to_clarification_decision()
            else:
                clarification = decide_model_intent_clarification(
                    self._query_intent,
                    user_input=user_input,
                    recent_history=recent_history_for_intent,
                )
            if clarification.should_stop:
                reply = clarification.reply
                assistant_msg = {"role": "assistant", "content": reply}
                history.append(assistant_msg)
                self.memory_store.append_history(assistant_msg)
                clear_thinking_prompt()
                print(f"Klonet Agent\uff1a{reply}")
                return reply, history, token

        turn_scope_message = self._build_turn_scope_message(effective_user_input)
        history.append(turn_scope_message)
        turn_answer_policy_message = None
        if self.profile.name == "mentor":
            turn_answer_policy_message = {
                "role": "system",
                "content": build_answer_policy(
                    (
                        self._turn_decision.answer_task_type
                        if self._turn_decision is not None
                        else self._query_route.task_type
                    ),
                    effective_user_input,
                    intent=self._query_intent,
                ),
            }
            history.append(turn_answer_policy_message)

        # 工具循环有明确上限，避免模型反复调用工具后阻塞 CLI。
        while tool_rounds < MAX_TOOL_ROUNDS:
            tool_rounds += 1
            printed_stream_reply = False
            if tool_rounds == 1:
                print_progress("正在组织回答...")

            def print_reply_delta(delta: str):
                nonlocal printed_stream_reply
                if not printed_stream_reply:
                    clear_thinking_prompt()
                    print_reasoning_trace_once()
                    print("Klonet Agent\uff1a", end="", flush=True)
                    printed_stream_reply = True
                print(delta, end="", flush=True)

            response = self.chat_with_llm(
                history,
                stream=True,
                on_delta=print_reply_delta,
            )
            # 记录 token 要放在外层，避免只有调用工具时才计数。
            token += response.usage.total_tokens

            # tool_calls 是本次模型决定要调用的工具集合。
            # 处理流程：模型输出标准工具参数 -> Python 执行工具 -> 工具结果输入模型 -> 模型继续判断。
            if response.choices[0].message.tool_calls:
                clear_thinking_prompt()
                if printed_stream_reply:
                    print()
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
                        current_intent_confidence = (
                            self._query_intent.confidence
                            if self._query_intent is not None
                            else 0.0
                        )
                        should_accept_candidate_intent = (
                            candidate_intent.confidence >= 0.6
                            and (
                                current_intent_confidence < 0.6
                                or candidate_intent.is_correction
                            )
                        )
                        if should_accept_candidate_intent:
                            self._query_intent = candidate_intent
                            self._conversation_state = (
                                self._conversation_state_manager.from_turn(
                                    user_input,
                                    recent_history=recent_history_for_intent,
                                    intent=candidate_intent,
                                    previous_state=self._conversation_state,
                                )
                            )
                            self._refresh_turn_plan(
                                user_input,
                                recent_history=recent_history_for_intent,
                            )
                            tool_args["conversation_state"] = (
                                self._conversation_state.to_tool_args()
                            )
                            tool_args["intent"] = self._query_intent_tool_args()
                            if turn_answer_policy_message is not None:
                                turn_answer_policy_message["content"] = (
                                    build_answer_policy(
                                        candidate_intent.task_type,
                                        user_input,
                                        intent=candidate_intent,
                                    )
                                )
                        elif self._query_intent is not None:
                            tool_args["intent"] = self._query_intent_tool_args()
                            tool_args["conversation_state"] = (
                                self._conversation_state.to_tool_args()
                            )
                    # 调用工具函数，开始执行命令或其他动作。
                    print_progress(f"正在调用工具：{tool_name}")
                    result = self.use_tool(tool_name, tool_args)
                    print_progress(f"工具完成：{tool_name}")
                    self._print_tool_loop_observation(tool_name, result)
                    tool_events.append(
                        {
                            "name": tool_name,
                            "args": dict(tool_args),
                            "result": result,
                        }
                    )

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
                if printed_stream_reply:
                    print()
                else:
                    clear_thinking_prompt()
                    print_reasoning_trace_once()
                    print(f"Klonet Agent\uff1a{reply}")

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
                self._last_turn_state = self._snapshot_turn_state(effective_user_input)
                self._paused_turn_state = None
                break

        else:
            reply = "本轮工具调用已达到上限，任务已暂停，等待用户确认后继续。"
            self._set_unfinished_todo_status("waiting_user")
            self._paused_turn_state = self._snapshot_turn_state(effective_user_input)
            self._last_turn_state = self._paused_turn_state
            assistant_msg = {"role": "assistant", "content": reply}
            history.append(assistant_msg)
            self.memory_store.append_history(assistant_msg)
            print(f"Klonet Agent\uff1a{reply}")

        # 本轮作用域只约束当前用户输入，不写入长期历史，避免影响下一轮。
        history = [
            message
            for message in history
            if (
                message is not turn_scope_message
                and message is not turn_answer_policy_message
                and message is not turn_resume_message
            )
        ]

        # 条件触发对话压缩。这里沿用旧版逻辑，用本轮 context token 判断。
        current_context_size = response.usage.total_tokens
        if current_context_size >= MAX_TOKEN:
            print(f"\nKlonet Agent：当前上下文约 {current_context_size} token，开始整理记忆。")
            history, token = self.compress_memory(history, token)

        return reply, history, token

    def _show_visible_reasoning_trace(self) -> bool:
        """默认输出用户可见思考摘要；brief 模式只输出最终答案。"""

        return self.profile.name != "ops" and self.answer_style != "brief"

    def _show_progress_updates(self) -> bool:
        """Show safe CLI progress milestones without adding them to model context."""

        return self.profile.name in {"mentor", "ops"} and self.answer_style != "brief"

    def _print_tool_loop_observation(self, tool_name: str, result: str) -> None:
        """Print an audit-friendly Ops tool-loop observation."""

        if self.profile.name != "ops" or self.answer_style == "brief":
            return
        evidence_line = self._first_evidence_line(result) or "工具没有返回可展示的摘要。"
        print(f"Klonet Agent：工具结果摘要：{tool_name} -> {evidence_line}")
        print("Klonet Agent：下一步：把该结果交给模型判断是否继续调用工具或形成诊断结论。")

    def _progress_intent_summary(self) -> str:
        """Return a short, non-sensitive summary of the current turn intent."""

        if self._turn_intent is None:
            return "已完成问题理解。"
        parts = [self._turn_intent.task_type]
        if self._turn_intent.phase and self._turn_intent.phase != "unknown":
            parts.append(self._turn_intent.phase)
        if self._turn_intent.operation and self._turn_intent.operation != "unknown":
            parts.append(self._turn_intent.operation)
        return "已识别：" + " / ".join(parts)

    def _is_resume_request(self, user_input: str) -> bool:
        """判断当前输入是否是在请求继续上一轮暂停任务。"""

        text = (user_input or "").strip().lower().replace(" ", "")
        return text in {
            "继续",
            "接着",
            "继续说",
            "接着说",
            "往下讲",
            "往下说",
            "继续讲",
            "继续回答",
            "继续上面",
            "接着上面",
            "goon",
            "continue",
        }

    def _resume_state_for(self, user_input: str) -> dict | None:
        """返回可用于“继续”语义的上一轮状态。"""

        if not self._is_resume_request(user_input):
            return None
        if self._paused_turn_state is not None:
            return self._paused_turn_state
        return self._last_turn_state

    def _snapshot_turn_state(self, original_user_input: str) -> dict:
        """保存可恢复的轻量回合状态。"""

        return {
            "original_user_input": original_user_input,
            "intent": self._query_intent,
            "decision": self._intent_decision,
            "turn_intent": self._turn_intent,
            "turn_decision": self._turn_decision,
            "route": self._query_route,
            "conversation_state": self._conversation_state,
        }

    def _refresh_turn_plan(
        self,
        user_input: str,
        *,
        recent_history: list[dict] | None = None,
        semantic_frame=None,
        resume_state: dict | None = None,
        effective_user_input: str | None = None,
    ) -> None:
        """Build the single turn intent/decision used by downstream actions."""

        current_route = self._query_route
        if self._query_intent is None and current_route is not None:
            self._query_intent = QueryIntent.from_mapping(
                {
                    "scope": current_route.scope,
                    "task_type": current_route.task_type,
                    "requires_retrieval": not current_route.hard_disable_rag,
                    "confidence": current_route.confidence,
                }
            )
        self._turn_intent = self._turn_intent_builder.build(
            user_input,
            recent_history=recent_history,
            intent=self._query_intent,
            semantic_frame=semantic_frame,
            decision=self._intent_decision,
            conversation_state=self._conversation_state,
            resume_state=resume_state,
            effective_user_input=effective_user_input,
        )
        self._turn_decision = self._turn_decision_planner.plan(self._turn_intent)
        self._query_intent = self._turn_intent.to_query_intent()
        if current_route is not None and current_route.hard_disable_rag:
            self._query_route = current_route
        else:
            self._query_route = route_from_intent(
                self._turn_intent.effective_user_input or user_input,
                self._query_intent,
            )

    def _render_visible_reasoning_trace(self, tool_events: list[dict]) -> str:
        """把本轮已发生的路由、意图和工具动作整理成可见摘要。

        这里输出的是可验证的执行轨迹，不是模型完整内部思维链。
        """

        task_type = self._query_route.task_type
        operation = getattr(self._query_intent, "operation", "unknown") or "unknown"
        scope = (
            self._query_intent.scope
            if self._query_intent is not None
            else self._query_route.scope
        )
        return "\n".join(
            [
                "Klonet Agent：思考摘要：",
                f"1. 问题类型：scope={scope}，task_type={task_type}，operation={operation}。",
                f"2. 证据计划：{self._evidence_plan_for_trace(scope, task_type)}",
                f"3. 工具动作：{self._tool_summary_for_trace(tool_events)}",
                f"4. 依据摘要：{self._evidence_summary_for_trace(tool_events)}",
            ]
        )

    def _evidence_plan_for_trace(self, scope: str, task_type: str) -> str:
        if scope == "general":
            return "以通用知识为主；Klonet 资料最多作为辅助对比。"
        if task_type in {"code_lookup", "troubleshooting", "development"}:
            return "优先核对源码或知识库证据，再组织回答。"
        if task_type in {"deployment_guidance", "operation_guide"}:
            return "优先检索 Klonet 操作知识，必要时用源码确认命令和配置。"
        return "优先使用 Klonet 知识库证据；证据不足时说明不确定。"

    def _tool_summary_for_trace(self, tool_events: list[dict]) -> str:
        if not tool_events:
            return "本轮未调用外部工具，直接根据当前上下文回答。"
        return "已调用 " + " → ".join(event["name"] for event in tool_events) + "。"

    def _evidence_summary_for_trace(self, tool_events: list[dict]) -> str:
        if not tool_events:
            return "暂无新增工具证据。"

        hints: list[str] = []
        for event in tool_events:
            name = event["name"]
            args = event.get("args", {})
            result = event.get("result", "")
            if name == "search_knowledge":
                hints.append(f"search_knowledge query={args.get('query', '')!r}")
            elif name == "search_code":
                hints.append(f"search_code query={args.get('query', '')!r}")
            elif name in {"read_source_file", "read_file"}:
                hints.append(f"{name} path={args.get('path', '')!r}")
            else:
                hints.append(name)

            evidence_line = self._first_evidence_line(result)
            if evidence_line:
                hints.append(evidence_line)
            if len(hints) >= 3:
                break
        return "；".join(hints[:3]) + "。"

    def _first_evidence_line(self, result: str) -> str:
        for line in (result or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("Error:"):
                continue
            if len(stripped) > 80:
                stripped = stripped[:77] + "..."
            return stripped
        return ""

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

    def _recent_dialogue_history(self, history: list[dict], limit: int = 6) -> list[dict]:
        """Return recent user/assistant turns for front-loaded intent analysis."""

        dialogue = [
            {"role": message.get("role"), "content": message.get("content", "")}
            for message in history
            if message.get("role") in {"user", "assistant"}
        ]
        return dialogue[-limit:]

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
        if self._turn_intent is not None:
            rules.extend(
                [
                    "【Unified TurnIntent】",
                    f"- task_type: {self._turn_intent.task_type}",
                    f"- operation: {self._turn_intent.operation}",
                    f"- target: {self._turn_intent.target}",
                    f"- requires_environment_diagnosis: {self._turn_intent.requires_environment_diagnosis}",
                    f"- user_role: {self._turn_intent.user_role}",
                    f"- machine_role: {self._turn_intent.machine_role}",
                    f"- phase: {self._turn_intent.phase}",
                    f"- context_ref: {self._turn_intent.context_ref}",
                    f"- source_need: {self._turn_intent.source_need}",
                    f"- excluded_meanings: {', '.join(self._turn_intent.excluded_meanings)}",
                    "- Downstream clarification, retrieval, source lookup and answer policy must follow this TurnIntent.",
                ]
            )
            if (
                self.profile.name == "mentor"
                and self._turn_intent.requires_environment_diagnosis
            ):
                rules.extend(
                    [
                        "- 这是 Klonet 运维诊断类问题；回答中应建议用户切换到 Ops 模式继续排查。",
                        "- 不要只给切换建议；先尝试基于当前知识库证据和已有上下文回答可确认部分，再建议切换到 Ops 模式读取本机环境做持续排查。",
                        "- Mentor 模式不得直接读取本机环境；inspect_system_environment、inspect_klonet_runtime、read_klonet_logs 只属于 Ops 模式。",
                    ]
                )
        if self._intent_decision is not None and self._intent_decision.soft_note:
            rules.extend(
                [
                    f"- 默认解释：{self._intent_decision.soft_note}",
                    "- 回答时先按默认解释直接给方案，再用一句低打扰提示说明另一种解释的流程不同。",
                ]
            )
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

    def _query_intent_tool_args(self) -> dict:
        """Return the front-loaded intent as safe tool arguments."""

        intent = (
            self._turn_intent.to_query_intent()
            if self._turn_intent is not None
            else self._query_intent
        )
        if intent is None:
            return {}
        return {
            "scope": intent.scope,
            "task_type": intent.task_type,
            "operation": intent.operation,
            "target": intent.target,
            "symptom": intent.symptom,
            "excluded_intents": list(intent.excluded_intents),
            "prerequisites": list(intent.prerequisites),
            "requires_retrieval": intent.requires_retrieval,
            "requires_environment_diagnosis": intent.requires_environment_diagnosis,
            "clarification_required": intent.clarification_required,
            "clarification_question": intent.clarification_question,
            "is_correction": intent.is_correction,
            "confidence": intent.confidence,
        }

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
