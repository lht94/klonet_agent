"""Agent 主编排流程。

这里承接旧版 runner.py 的职责：接收用户输入、组装上下文、调用 LLM、分发工具调用、
写入记忆和项目日志，并决定是否继续下一轮工具循环。
"""

from __future__ import annotations

import json
from time import perf_counter

from klonet_agent.agents import AgentProfile, get_profile
from klonet_agent.config import MAX_TOKEN, TRACE_FILE
from klonet_agent.knowledge import SKILL_LOADER
from klonet_agent.llm import LLMClient
from klonet_agent.memory import MEMORY_STORE
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
    ):
        self.profile = profile or get_profile("mentor")
        self.session = session or AgentSession(mode=self.profile.name)
        self.llm = llm or LLMClient()
        self.trace_logger = trace_logger or TraceLogger(TRACE_FILE)
        self.tool_executor = tool_executor or ToolExecutor(
            session=self.session,
            # 执行层再次检查工具权限，避免模型绕过可见工具列表。
            allowed_tools=self.profile.allowed_tools,
            trace_logger=self.trace_logger,
        )

    def init_history(self) -> list[dict]:
        """初始化对话记忆，包含系统提示词、记忆提示词、技能描述和任务规划规则。"""

        history = []
        # 把分层系统提示词加入上下文。Profile 决定 Mentor/Coding 的行为差异。
        for prompt in build_system_prompts(self.profile.mode_prompt):
            history.append({"role": "system", "content": prompt})

        # 把记忆设定加入到系统提示词中。
        memory_prompt = MEMORY_STORE.memory_prompt()
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
        last_history = MEMORY_STORE.load_unarchived_history()
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
        """调用工具执行器。"""

        return self.tool_executor.run(tool_name, tool_args)

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
                MEMORY_STORE.append_history(comp_assistant)

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
                    MEMORY_STORE.append_history(comp_tool_msg)

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
                MEMORY_STORE.append_history(comp_assistant)

                print(f"Klonet Agent：{compress_reply}")
                # 打入压缩标记，表示前面的内容已经被压缩归档。
                MEMORY_STORE.append_compact_marker()
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
        MEMORY_STORE.append_history({"role": "user", "content": user_input})

        reply = ""

        # 再套一层 while 循环，使得 LLM 可以一直循环调用工具，直到停止工作并输出自然语言。
        while True:
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
                MEMORY_STORE.append_history(assistant_msg)

                for tool_call in response.choices[0].message.tool_calls:
                    # 工具名，即 schema 中 function.name。
                    tool_name = tool_call.function.name
                    # 工具参数，即 schema 中 function.parameters 约束出的标准 JSON。
                    tool_args = json.loads(tool_call.function.arguments)
                    # 调用工具函数，开始执行命令或其他动作。
                    result = self.use_tool(tool_name, tool_args)

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                    history.append(tool_msg)
                    MEMORY_STORE.append_history(tool_msg)

                    # 核心补丁：记忆同步刷新机制。
                    # 如果刚才执行的工具修改了长期记忆文件，要立刻刷新系统提示词中的记忆内容。
                    if tool_name in ["write_memory", "write_user"]:
                        self._refresh_memory_prompt(history)
                        print("Klonet Agent：长期记忆已刷新。")
            else:
                # 没有调用工具，说明本次对话进入最终自然语言回答。
                reply = response.choices[0].message.content
                print(f"Klonet Agent：{reply}")

                # 对于有计划的任务，做一次检查，避免模型声称完成但 todo 状态仍未完成。
                if self.session.todos:
                    unfinished = [
                        todo for todo in self.session.todos if todo["status"] != "completed"
                    ]
                    if unfinished:
                        print("Klonet Agent：任务列表里还有未完成项，继续推进。")
                        print(render_todos(self.session.todos))
                        print()
                        continue_prompt = (
                            "以下任务仍未完成，请按计划继续执行，"
                            "并按规矩更新 todolist 状态：\n"
                            + render_todos(self.session.todos)
                        )
                        history.append({"role": "user", "content": continue_prompt})
                        MEMORY_STORE.append_history(
                            {"role": "user", "content": continue_prompt}
                        )
                        # continue 会跳过本次循环剩余逻辑，重新回到 chat_with_llm。
                        continue

                    print("Klonet Agent：任务全部完成。")
                    print(render_todos(self.session.todos))
                    print()
                    self.session.todos.clear()

                assistant_msg = {"role": "assistant", "content": reply}
                if self._has_reasoning(response.choices[0].message):
                    assistant_msg["reasoning_content"] = (
                        response.choices[0].message.reasoning_content
                    )

                history.append(assistant_msg)
                MEMORY_STORE.append_history(assistant_msg)
                break

        # 条件触发对话压缩。这里沿用旧版逻辑，用本轮 context token 判断。
        current_context_size = response.usage.total_tokens
        if current_context_size >= MAX_TOKEN:
            print(f"\nKlonet Agent：当前上下文约 {current_context_size} token，开始整理记忆。")
            history, token = self.compress_memory(history, token)

        return reply, history, token

    def _visible_tools(self) -> list[dict]:
        """根据 profile 过滤模型可见工具。"""

        return [
            tool
            for tool in TOOLS
            if tool["function"]["name"] in self.profile.allowed_tools
        ]

    def _refresh_memory_prompt(self, history: list[dict]):
        """刷新上下文里的记忆系统提示词。"""

        for msg in history:
            if msg.get("role") == "system" and "MEMORY.md" in msg.get("content", ""):
                msg["content"] = MEMORY_STORE.memory_prompt()
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
