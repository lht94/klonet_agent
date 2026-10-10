"""Agent 主编排流程。

这里承接旧版 runner.py 的职责：接收用户输入、组装上下文、调用 LLM、分发工具调用、
写入记忆和项目日志，并决定是否继续下一轮工具循环。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from time import perf_counter
from types import SimpleNamespace

from klonet_agent.agents import AgentProfile, get_profile
from klonet_agent.answer_policy import build_answer_policy
from klonet_agent.config import (
    CONTEXT_COMPILER_ENABLED,
    CONTEXT_COMPACTION_MIN_TOKENS,
    GOVERNANCE_TRACE_FILE,
    HISTORY_MAX_MESSAGES,
    LEGACY_MEMORY_COMPRESSION_ENABLED,
    MAX_TODO_CONTINUATIONS,
    MAX_TOKEN,
    MAX_TOOL_ROUNDS,
    MEMORY_AUTHORITY,
    MEMORY_BACKFILL_LIMIT,
    MEMORY_PACK_ENABLED,
    MEMORY_PACK_RECALL_LIMIT,
    MEMORY_PACK_TOKEN_BUDGET,
    markdown_memory_is_authoritative,
    MEMORY_DIR,
    MEMORY_WRITE_PIPELINE_ENABLED,
    OPS_MAX_TOOL_ROUNDS,
    OPS_PRIVILEGE_CLASSIFIER_MODEL,
    OPS_PRIVILEGE_CLASSIFIER_TIMEOUT_SECONDS,
    OPS_PRIVILEGE_PLANNER_MODEL,
    OPS_PRIVILEGE_PLANNER_TIMEOUT_SECONDS,
    RAG_QUERY_PLANNER_MODEL,
    RAG_QUERY_PLANNER_TIMEOUT_SECONDS,
    RAG_SEARCH_BUDGETS,
    RUNTIME_GOVERNANCE_ENABLED,
    TRACE_FILE,
    JEV_MIN_CONFIDENCE,
)
from klonet_agent.context.compiler import (
    CompiledContext,
    ContextCompiler,
    ContextOverflowError,
    assert_within_hard_limit,
    to_provider_messages,
)
from klonet_agent.context.tokens import estimate_messages_tokens
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
from klonet_agent.knowledge.models import RetrievalPlan
from klonet_agent.knowledge.query_planner import plan_as_dict
from klonet_agent.knowledge.semantic_understanding import IntentDecision
from klonet_agent.knowledge.turn_intent import (
    TurnDecision,
    TurnDecisionPlanner,
    TurnIntent,
    TurnIntentBuilder,
)
from klonet_agent.knowledge import SKILL_LOADER, route_query
from klonet_agent.journal import ProjectJournal, ProjectJournalMaintainer
from klonet_agent.llm import LLMClient
from klonet_agent.llm.decision import configured_jev_decision_model
from klonet_agent.memory import MemoryStore
from klonet_agent.memory.checkpoint_store import CheckpointStore
from klonet_agent.memory.compactor import CompactionError, MemoryCompactor
from klonet_agent.memory.models import TaskCheckpoint
from klonet_agent.memory.store import sanitize_openai_tool_history


def _parse_covered_rows(source_event_end: str) -> int | None:
    """解析 checkpoint 覆盖范围的 "rows-<n>" 标记。"""

    raw = str(source_event_end or "").strip()
    prefix = "rows-"
    if not raw.startswith(prefix):
        return None
    try:
        value = int(raw[len(prefix) :])
    except ValueError:
        return None
    return value if value >= 0 else None
from klonet_agent.ops.planner import build_ops_environment_plan
from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
from klonet_agent.ops.privileged.execution_agent import (
    PrivilegedExecutionAgent,
)
from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder
from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier
from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
from klonet_agent.ops.privileged.workflow.change_binding import ChangeBinder
from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
from klonet_agent.ops.privileged.workflow.operational_context import OperationalContextStore
from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent
from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
from klonet_agent.ops.privileged.workflow.response import ResponseAgent
from klonet_agent.ops.privileged.workflow.readonly_runtime import ValidatedReadonlyCommandRunner
from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore
from klonet_agent.ops.privileged.workflow.evidence_synthesis import EvidenceSynthesizer
from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow
from klonet_agent.ops.routing import OpsRoute, route_ops_request
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
        journal_maintainer: ProjectJournalMaintainer | None = None,
        privileged_workflow: object | None = None,
        privileged_supervisor: object | None = None,
        decision_model: object | None = None,
        answer_style: str = "default",
    ):
        self.profile = profile or get_profile("mentor")
        self.session = session or AgentSession(mode=self.profile.name)
        supplied_llm = llm
        self.llm = llm or LLMClient()
        self._usage_clients = [self.llm]
        self.decision_model = decision_model or configured_jev_decision_model(
            self.session.user_id, self.session.project_id,
        )
        self._ops_semantic_routing = intent_analyzer is not None or supplied_llm is None
        self.answer_style = answer_style
        if intent_analyzer is not None:
            self.intent_analyzer = intent_analyzer
        elif supplied_llm is not None:
            # Test/custom callers commonly provide one deterministic client.
            self.intent_analyzer = IntentAnalyzer(
                self.llm,
                decision_model=self.decision_model,
                min_decision_confidence=JEV_MIN_CONFIDENCE,
            )
        else:
            query_planner_llm = LLMClient(
                model=RAG_QUERY_PLANNER_MODEL,
                timeout=RAG_QUERY_PLANNER_TIMEOUT_SECONDS,
            )
            self._usage_clients.append(query_planner_llm)
            self.intent_analyzer = IntentAnalyzer(
                query_planner_llm,
                decision_model=self.decision_model,
                min_decision_confidence=JEV_MIN_CONFIDENCE,
            )
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
        self._retrieval_plan: RetrievalPlan | None = None
        self._conversation_state_manager = ConversationStateManager()
        self._knowledge_search_count = 0
        self._paused_turn_state: dict | None = None
        self._last_turn_state: dict | None = None
        self._ops_route: OpsRoute | None = None
        # 上下文编译器：调用前按模型预算组装上下文（见 docs/superpowers/plans）。
        self.context_compiler = ContextCompiler()
        # 结构化检查点：超过软阈值时生成 TaskCheckpoint，重启后可恢复。
        self.checkpoint_store = CheckpointStore(self.memory_store.memory_dir)
        self.memory_compactor = MemoryCompactor(self._llm_complete_text)
        # 事件水位：checkpoint 已覆盖到 history.jsonl 的哪一行之前。
        # None 表示尚未从 checkpoint 读取，首次使用时按 load_latest 解析。
        self._covered_rows: int | None = None
        # 下一个可分配的事件行号；惰性初始化为 history.jsonl 的当前行数。
        self._next_event_row: int | None = None
        # 本轮已持久化的事件行号。受控写入管线靠它核对"候选引用的来源是否真的存在"，
        # 因此只保留最近若干条，不随会话无限增长。
        self._recent_event_ids: list[str] = []
        # 受控写入管线实例（记忆系统阶段 3）。惰性构造：开关关闭、没有记忆库 DSN、
        # 或者数据库连不上时都保持 None，主链路完全不受影响。
        self._memory_pipeline = None
        self._memory_pipeline_error: str | None = None
        # 记忆召回链路（阶段 5）。读路径的仓库、召回器与构包器各自惰性构造：
        # 开关关闭或记忆库不可用时保持 None，编译器拿到的就是"这轮不注入记忆"。
        self._memory_read_repository = None
        self._memory_read_database = None
        self._memory_read_error: str | None = None
        self._memory_retriever_cache = None
        self._memory_retriever_error: str | None = None
        # cutover（默认）下"库不可用 → 降级 Markdown"只告警一次的标记。
        self._markdown_fallback_notified = False
        self._memory_pack_builder_cache = None
        # 本轮 history 列表的引用。工具循环里的记忆工具需要通过它拿到本轮事件，
        # 而 single_chat 只在开始时把引用放进来（列表本身是就地修改的）。
        self._turn_history_ref: list[dict] | None = None
        # 运行治理层（03 计划阶段 1-3）。惰性构造：开关关闭、没有 DSN 时保持
        # None，主链路逐字不变；打开后任务状态改变 fail closed。
        self._governance = None
        self._governance_error: str | None = None
        self.journal_maintainer = journal_maintainer or ProjectJournalMaintainer(
            ProjectJournal.from_session(self.session),
            # Test doubles and deterministic callers should not receive a
            # surprise third model call. Production LLMClient instances still
            # enable automatic journal maintenance.
            llm=self.llm if isinstance(self.llm, LLMClient) else None,
        )
        self.tool_executor = tool_executor or ToolExecutor(
            session=self.session,
            # 执行层再次检查工具权限，避免模型绕过可见工具列表。
            allowed_tools=self.profile.allowed_tools,
            trace_logger=self.trace_logger,
            memory_store=self.memory_store,
        )
        self.privileged_workflow = privileged_workflow

        def privileged_progress(role: str):
            def emit(message: str) -> None:
                text = str(message or "").strip()
                known_prefixes = (
                    "Planner：",
                    "Implementation Binding Agent：",
                    "Execution Agent：",
                    "Verifier：",
                    "Workflow Coordinator：",
                    "计划器：",
                    "实施绑定：",
                    "执行器：",
                    "验证器：",
                    "工作流协调器：",
                )
                if text.startswith(known_prefixes):
                    print(text, flush=True)
                else:
                    print("%s：%s" % (role, text), flush=True)

            return emit

        self.privileged_supervisor = privileged_supervisor
        if (
            self.profile.name == "ops"
            and self.privileged_supervisor is None
        ):
            planner_llm = self.llm
            classifier_llm = self.llm
            if supplied_llm is None:
                planner_llm = LLMClient(
                    model=OPS_PRIVILEGE_PLANNER_MODEL,
                    timeout=OPS_PRIVILEGE_PLANNER_TIMEOUT_SECONDS,
                    max_retries=0,
                )
                classifier_llm = LLMClient(
                    model=OPS_PRIVILEGE_CLASSIFIER_MODEL,
                    timeout=OPS_PRIVILEGE_CLASSIFIER_TIMEOUT_SECONDS,
                    max_retries=0,
                )
                self._usage_clients.extend([planner_llm, classifier_llm])
            context_builder = PrivilegedPlanContextBuilder(
                on_progress=privileged_progress("Discovery"),
            )
            probe_runner = context_builder.run_recovery_diagnostics
            executor = PrivilegedCommandExecutor(
                on_start=privileged_progress("执行"),
                on_output=lambda channel, chunk: print(chunk, end="", flush=True),
                environment_fingerprint_provider=(
                    context_builder.current_environment_fingerprint
                ),
            )
            discovery = DiscoveryAgent(
                planner_llm,
                probe_runner=probe_runner,
                readonly_command_runner=ValidatedReadonlyCommandRunner(executor),
                on_progress=privileged_progress("Discovery"),
                knowledge_search=context_builder.knowledge_search,
            )
            verifier = PrivilegedVerifierAgent(
                planner_llm,
                probe_runner=discovery.run_ad_hoc_requests,
            )
            synthesis = EvidenceSynthesizer(planner_llm)
            # Ops responses share the bounded workflow client so a secondary
            # presentation call can always fall back instead of hanging the
            # failure-reporting path indefinitely.
            response_agent = ResponseAgent(planner_llm)
            mutation_workflow = MutationWorkflow(
                planner=ChangePlannerAgent(planner_llm),
                binder=ChangeBinder(
                    PrivilegedExecutionAgent(
                        planner_llm,
                        probe_runner=discovery.run_ad_hoc_requests,
                        on_progress=privileged_progress(
                            "实施绑定"
                        ),
                    )
                ),
                store=ChangePlanStore(
                    MEMORY_DIR,
                    user_id=self.session.user_id,
                    project_id=self.session.project_id,
                ),
                executor=executor,
                verifier=verifier,
                discovery=discovery,
                synthesis=synthesis,
                response=response_agent,
            )
            self.privileged_workflow = mutation_workflow
            self.privileged_supervisor = PrivilegedOpsCoordinator(
                classifier=PrivilegedIntentClassifier(
                    classifier_llm,
                    decision_model=self.decision_model,
                    min_decision_confidence=JEV_MIN_CONFIDENCE,
                ),
                discovery=discovery,
                synthesis=synthesis,
                response=response_agent,
                mutation_workflow=mutation_workflow,
                verifier=verifier,
                context_store=OperationalContextStore(
                    MEMORY_DIR,
                    user_id=self.session.user_id,
                    project_id=self.session.project_id,
                ),
                on_progress=privileged_progress("Klonet Agent"),
            )

    def usage_snapshot(self) -> dict[str, int]:
        """Aggregate provider-reported usage across every orchestration client."""

        totals = {
            "total_tokens": 0,
            "successful_calls": 0,
            "unavailable_calls": 0,
        }
        seen: set[int] = set()
        for client in getattr(self, "_usage_clients", [self.llm]):
            if id(client) in seen:
                continue
            seen.add(id(client))
            snapshot_method = getattr(client, "usage_snapshot", None)
            if not callable(snapshot_method):
                continue
            snapshot = snapshot_method()
            for key in totals:
                totals[key] += int(snapshot.get(key, 0))
        return totals

    def init_history(self) -> list[dict]:
        """初始化对话记忆，包含系统提示词、记忆提示词、技能描述和任务规划规则。"""

        history = []
        # 把分层系统提示词加入上下文。Profile 决定 Mentor/Coding 的行为差异。
        for prompt in build_system_prompts(self.profile.mode_prompt):
            history.append({"role": "system", "content": prompt})

        # 把记忆设定加入到系统提示词中。
        # 打开 MemoryPack 开关后，这里不再常驻 MEMORY.md / USER.md 全文：正文改由
        # 每轮按问题召回的证据块承担（见 _memory_pack_message）。
        memory_prompt = self.memory_store.memory_prompt(
            mode=self.profile.name,
            include_long_term=self._markdown_memory_is_injected(),
        )
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

        # 载入上一次对话。优先使用最新任务检查点恢复：checkpoint 覆盖
        # 之前的历史行，只把其后的事件作为未压缩历史候选。
        last_history = self._load_recovered_history()
        history.extend(last_history)

        return history

    def _load_recovered_history(self) -> list[dict]:
        """从 checkpoint + 后续事件恢复工作历史。

        恢复不再使用固定消息条数：checkpoint 覆盖区间之后的事件全部作为
        候选，最终保留多少由 ContextCompiler 的 token 预算和完整消息组
        选择决定。

        checkpoint 损坏或覆盖范围无法解析时，回退到旧的 compact_event
        标记恢复方式（仍然不限条数）。
        """

        checkpoint = self.checkpoint_store.load_latest()
        if checkpoint is not None:
            covered = _parse_covered_rows(checkpoint.source_event_end)
            if covered is not None:
                self._covered_rows = covered
                recovered = self.memory_store.load_history_after(
                    covered,
                    max_messages=self._legacy_history_cap(),
                )
                if recovered:
                    return recovered
                # 区间之后没有新事件：checkpoint 已经代表全部有效历史。
                return []
        return self.memory_store.load_unarchived_history(
            max_messages=self._legacy_history_cap(),
        )

    @staticmethod
    def _legacy_history_cap() -> int:
        """旧路径回退时才按条数截断；新主链路为 0（不限条数）。"""

        return HISTORY_MAX_MESSAGES if LEGACY_MEMORY_COMPRESSION_ENABLED else 0

    def _current_covered_rows(self) -> int:
        """返回当前 checkpoint 已覆盖的事件行数（无 checkpoint 时为 0）。"""

        if self._covered_rows is None:
            checkpoint = self.checkpoint_store.load_latest()
            covered = (
                _parse_covered_rows(checkpoint.source_event_end)
                if checkpoint is not None
                else None
            )
            self._covered_rows = covered or 0
        return self._covered_rows

    def _next_history_row(self) -> int:
        """分配下一个 history.jsonl 事件行号。"""

        if self._next_event_row is None:
            self._next_event_row = self.memory_store.count_history_rows()
        ordinal = self._next_event_row
        self._next_event_row += 1
        return ordinal

    def _emit_turn_message(self, history: list[dict], message: dict) -> dict:
        """追加一条已持久化的对话事件，并绑定它的事件行号。

        同时写入内存 history 与 history.jsonl，保证两边的
        `event_id`（"rows-<n>"）一致：这样“checkpoint 覆盖到哪一行”既能在
        本轮编译时按行扣减，也能在重启后按同一行号恢复。
        """

        if not message.get("event_id"):
            # 注意不要用 setdefault：默认值会被提前求值，导致行号被白白消耗。
            message["event_id"] = f"rows-{self._next_history_row()}"
        history.append(message)
        self.memory_store.append_history(message)
        # 记录本轮事件行号，供写入管线核对候选引用的来源。只留最近 200 条。
        self._recent_event_ids.append(message["event_id"])
        if len(self._recent_event_ids) > 200:
            del self._recent_event_ids[:-200]
        return message


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
        # 治理层模型调用记录（telemetry 级：不可用时进缓冲，不阻断主链路）。
        governance = self._governance
        if governance is not None:
            actual_model = getattr(self.llm, "model", None) or "unknown"
            governance.record_model_call(
                model=actual_model,
                total_tokens=getattr(getattr(response, "usage", None), "total_tokens", 0) or 0,
                duration_ms=duration_ms,
                succeeded=True,
            )
            # 阶段 5：影子路由——策略选择 vs 实际模型，只记录不控制流量。
            try:
                governance.record_route_decision(actual_model=actual_model, shadow=True)
            except Exception:
                pass  # 影子路由失败绝不影响主链路。
        return response

    def _complete_llm(self, history: list[dict], *, stream: bool):
        sanitized_history = sanitize_openai_tool_history(history)
        if len(sanitized_history) != len(history):
            history[:] = sanitized_history
        tools = self._visible_tools()
        model = getattr(self.llm, "model", None) or "unknown"
        if CONTEXT_COMPILER_ENABLED:
            # 编译失败（必需区超过硬预算）不允许回退到完整历史，
            # 由 ContextOverflowError 向上抛出并在回合层给出确定性本地错误。
            compiled = self._compile_context(history, tools)
            send_messages = compiled.messages
        else:
            # 显式关闭编译器时的旧路径：仍然在发送前做硬预算断言，
            # 保证“供应商不会收到超过 hard input limit 请求”这条不变量成立。
            send_messages = history
        send_messages = to_provider_messages(send_messages)
        assert_within_hard_limit(send_messages, model, tools)
        if not stream:
            return self.llm.complete(messages=send_messages, tools=tools)
        try:
            return self.llm.complete(messages=send_messages, tools=tools, stream=True)
        except TypeError as exc:
            if "stream" not in str(exc):
                raise
            return self.llm.complete(messages=send_messages, tools=tools)
        except Exception as exc:
            if not self._is_timeout_error(exc):
                raise
            return self.llm.complete(messages=send_messages, tools=tools)

    def _compile_context(
        self,
        history: list[dict],
        tools: list[dict],
    ) -> CompiledContext:
        """编译本次请求的上下文视图。

        超过软阈值时先做一次结构化压缩，再携带 checkpoint 重新编译；重新编译
        时只保留 checkpoint 覆盖区间之后的事件候选，避免同一请求里
        checkpoint 与原始历史重复出现。

        必需区超过硬预算时不做任何回退：记录事件后抛出 ContextOverflowError，
        由回合层给出确定性本地错误，绝不把未编译的完整历史发给供应商。
        """

        model = getattr(self.llm, "model", None) or "unknown"
        # 记忆包在同一回合里只构造一次：压缩后重编译时复用同一份，
        # 不为同一句用户输入跑两遍召回与嵌入。
        memory_pack_message = self._memory_pack_message(history)
        try:
            compiled = self.context_compiler.compile_history(
                history,
                model=model,
                tool_definitions=tools,
                memory_pack_message=memory_pack_message,
            )
        except ContextOverflowError as exc:
            self.trace_logger.record_privileged_event(
                user_id=self.session.user_id,
                project_id=self.session.project_id,
                mode=self.session.mode,
                event="context_compile_overflow",
                payload={"model": model, "areas": exc.areas},
            )
            raise

        if compiled.compression_required:
            covered_prefix = self._covered_prefix_length(
                history, compiled.omitted_event_ids
            )
            coverable = self._non_system_messages(history)[:covered_prefix]
            coverable_tokens = estimate_messages_tokens(coverable)
            checkpoint_message = None
            if covered_prefix and coverable_tokens >= CONTEXT_COMPACTION_MIN_TOKENS:
                checkpoint_message = self._compact_context_once(
                    history, covered_prefix
                )
            else:
                # 没有可压缩的旧事件，或压缩收益低于阈值（软阈值可能由系统
                # 规则/证据区单独造成）：不重复压缩，避免每轮多一次模型调用。
                self.trace_logger.record_privileged_event(
                    user_id=self.session.user_id,
                    project_id=self.session.project_id,
                    mode=self.session.mode,
                    event="context_compression_skipped",
                    payload={
                        "reason": (
                            "no_covered_events"
                            if not covered_prefix
                            else "below_min_tokens"
                        ),
                        "coverable_tokens": coverable_tokens,
                        "estimated_input_tokens": compiled.estimated_input_tokens,
                        "areas": compiled.areas,
                    },
                )

            if checkpoint_message is not None:
                uncovered = self._history_after_covered(history, covered_prefix)
                compiled = self.context_compiler.compile_history(
                    uncovered,
                    model=model,
                    tool_definitions=tools,
                    checkpoint_message=checkpoint_message,
                    memory_pack_message=memory_pack_message,
                )
                if compiled.compression_required:
                    self.trace_logger.record_privileged_event(
                        user_id=self.session.user_id,
                        project_id=self.session.project_id,
                        mode=self.session.mode,
                        event="context_compression_still_required",
                        payload={
                            "estimated_input_tokens": compiled.estimated_input_tokens,
                            "areas": compiled.areas,
                        },
                    )

        self.trace_logger.record_context_compile(
            user_id=self.session.user_id,
            project_id=self.session.project_id,
            mode=self.session.mode,
            model=model,
            estimated_input_tokens=compiled.estimated_input_tokens,
            hard_input_limit=compiled.hard_input_limit,
            soft_input_limit=compiled.soft_input_limit,
            compression_required=compiled.compression_required,
            included_event_ids=len(compiled.included_event_ids),
            omitted_event_ids=len(compiled.omitted_event_ids),
            areas=compiled.areas,
            profile_source=compiled.profile_source,
        )
        return compiled

    @staticmethod
    def _row_of(message: dict) -> int | None:
        """读取消息绑定的 history.jsonl 行号（"rows-<n>"）。"""

        raw = str(message.get("event_id") or "").strip()
        if not raw.startswith("rows-"):
            return None
        try:
            return int(raw[len("rows-") :])
        except ValueError:
            return None

    @staticmethod
    def _non_system_messages(history: list[dict]) -> list[dict]:
        return [message for message in history if message.get("role") != "system"]

    @classmethod
    def _covered_prefix_length(
        cls,
        history: list[dict],
        omitted_event_ids: tuple[str, ...],
    ) -> int:
        """把编译器的 omitted 事件 id 映射成“需要被 checkpoint 覆盖的前缀长度”。

        omitted 事件 id 有两种来源：
        - "rows-<n>"：已持久化事件，用行号定位；
        - "msg-<index>"：旧格式/未持久化事件，用它在非 system 消息序列中的位置定位。

        返回覆盖前缀的非 system 消息条数；当前用户输入永远不计入，避免把本轮
        输入压缩掉。编译器没有报告被淘汰事件时，退化为覆盖当前用户输入之前的
        全部事件 —— 历史整体超过软阈值时，正需要把整段历史折叠成 checkpoint。
        出现无法映射的 id 时同样保守地整体覆盖：宁可多覆盖，也不能把未总结的
        事件当成已覆盖。
        """

        non_system = cls._non_system_messages(history)
        if not non_system:
            return 0
        newest_is_user = non_system[-1].get("role") == "user"
        max_coverable = len(non_system) - (1 if newest_is_user else 0)
        if max_coverable <= 0:
            return 0
        if not omitted_event_ids:
            return max_coverable

        row_to_position: dict[int, int] = {}
        for position, message in enumerate(non_system):
            row = cls._row_of(message)
            if row is not None:
                row_to_position[row] = position

        max_position = -1
        unmapped = False
        for event_id in omitted_event_ids:
            raw = str(event_id or "").strip()
            if raw.startswith("msg-"):
                try:
                    position = int(raw[len("msg-") :])
                except ValueError:
                    unmapped = True
                    continue
                max_position = max(max_position, position)
                continue
            row = cls._row_of({"event_id": raw})
            if row is not None and row in row_to_position:
                max_position = max(max_position, row_to_position[row])
            else:
                unmapped = True
        if max_position < 0 or unmapped:
            return max_coverable
        return min(max_position + 1, max_coverable)

    @classmethod
    def _history_after_covered(cls, history: list[dict], covered_prefix: int) -> list[dict]:
        """返回只保留未覆盖事件的历史视图。

        系统/本轮控制消息始终保留；非 system 消息丢掉被 checkpoint 覆盖的前
        `covered_prefix` 条，当前用户输入永远在尾部因而不受影响。
        """

        if covered_prefix <= 0:
            return list(history)
        remaining = covered_prefix
        kept: list[dict] = []
        for message in history:
            if message.get("role") == "system" or remaining <= 0:
                kept.append(message)
                continue
            remaining -= 1
        return kept


    def _llm_complete_text(self, messages: list[dict]) -> str:
        """供 MemoryCompactor 使用的纯文本完成函数。

        压缩是受控调用：显式传 tools=None，兼容把 tools 作为必填位置的
        LLM/FakeLLM 实现，避免压缩路径因接口差异直接 TypeError。
        """

        response = self.llm.complete(messages=messages, tools=None)
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        return str(getattr(choices[0].message, "content", None) or "")

    def _compact_context_once(
        self,
        history: list[dict],
        covered_prefix: int,
    ) -> dict | None:
        """超过软阈值时为已覆盖的旧事件生成一次结构化 checkpoint。

        压缩输入是“被 checkpoint 覆盖的那部分事件”（增量），而不是完整
        history：已覆盖区间由上一版 checkpoint 承载，作为 previous 传入。
        每次请求最多压缩一次；压缩失败保留旧 checkpoint，不阻断主流程。
        """

        previous = self.checkpoint_store.load_latest()
        covered_before = self._current_covered_rows()
        covered_messages = self._non_system_messages(history)[:covered_prefix]
        if not covered_messages:
            return None
        covered_rows = sum(
            1 for message in covered_messages if self._row_of(message) is not None
        )
        covered_until = covered_before + covered_rows
        try:
            checkpoint = self.memory_compactor.compact(
                covered_messages,
                previous,
                user_id=self.session.user_id,
                project_id=self.session.project_id,
                mode=self.session.mode,
                source_event_start=f"rows-{covered_before}",
                source_event_end=f"rows-{covered_until}",
            )
        except CompactionError as exc:
            self.trace_logger.record_privileged_event(
                user_id=self.session.user_id,
                project_id=self.session.project_id,
                mode=self.session.mode,
                event="context_compaction_failed",
                payload={"detail": str(exc)[:500]},
            )
            print("Klonet Agent：上下文压缩失败，保留原始历史继续。")
            return None

        if previous is not None and checkpoint.checkpoint_id == previous.checkpoint_id:
            # 压缩失败回退到了旧 checkpoint：不产生新版本。
            self._covered_rows = covered_before
            return previous.render_system_message()

        saved = self.checkpoint_store.save(checkpoint)
        self._covered_rows = covered_until
        # checkpoint 之后的事件未必都被写过候选：这里补扫一次未处理区间
        # （计划 §7.2："session 结束和 checkpoint 只负责扫描尚未处理的事件区间"）。
        self._backfill_memory_candidates()
        self.trace_logger.record_privileged_event(
            user_id=self.session.user_id,
            project_id=self.session.project_id,
            mode=self.session.mode,
            event="context_compaction",
            payload={
                "checkpoint_id": saved.checkpoint_id,
                "version": saved.version,
                "source_event_start": saved.source_event_start,
                "source_event_end": saved.source_event_end,
                "covered_messages": len(covered_messages),
            },
        )
        print(
            f"Klonet Agent：已生成任务检查点 v{saved.version}，"
            "旧上下文将以压缩状态保留。"
        )
        return saved.render_system_message()

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
            return self._execute_tool(tool_name, tool_args)

        budget = self._rag_search_budget(scope)
        if self._knowledge_search_count >= budget:
            return (
                f"本轮 {scope} 检索预算已用完（最多 {budget} 次）。"
                "请根据已有证据完成回答，不要继续改写查询。"
            )

        self._knowledge_search_count += 1
        result = self._execute_tool(tool_name, tool_args)
        if scope == "general":
            return (
                "【secondary Klonet evidence】\n"
                "以下内容只能用于辅助对比，不能改变通用技术问题的主要方向：\n"
                f"{result}"
            )
        return result

    def _execute_tool(self, tool_name: str, tool_args: dict) -> str:
        """执行工具并记录治理事件（03 计划阶段 1/3）。

        成功 → ``tool_call.completed``；异常 → ``tool_call.failed``（结果
        不明时 outcome 取 ``outcome_unknown``，阻断自动重试）并打开失败记录。
        治理存储不可用时按 fail-closed 语义抛出：副作用不明的失败不允许
        静默吞掉治理痕迹。
        """

        governance = self._governance
        start = perf_counter()
        try:
            result = self.tool_executor.run(tool_name, tool_args)
        except Exception as exc:
            duration_ms = int((perf_counter() - start) * 1000)
            if governance is not None:
                governance.record_tool_call(
                    tool_name=tool_name,
                    duration_ms=duration_ms,
                    outcome="outcome_unknown",
                    args_preview=self._safe_tool_args_preview(tool_args),
                    error_class=type(exc).__name__,
                )
                governance.record_failure(
                    stage="tool_execution",
                    error_class=(type(exc).__name__ or "UnknownError")[:80],
                    message=str(exc)[:500],
                    retryable=False,
                )
            raise
        duration_ms = int((perf_counter() - start) * 1000)
        if governance is not None:
            governance.record_tool_call(
                tool_name=tool_name,
                duration_ms=duration_ms,
                outcome="succeeded",
                args_preview=self._safe_tool_args_preview(tool_args),
            )
            # 阶段 4：成功的工具结果登记为证据（内容寻址哈希，观察只存预览）。
            # evidence 是观察记录，冲突检测走服务层（同主体矛盾 → contradicted 主张）。
            try:
                governance.record_evidence(
                    source_type="tool",
                    subject=tool_name,
                    observation=str(result),
                    source_uri=tool_name,
                    raw_content=str(result),
                    confidence=0.6,
                )
            except Exception:
                pass  # 证据登记失败不阻断工具结果返回（telemetry 级）。
        return result

    @staticmethod
    def _safe_tool_args_preview(tool_args: dict) -> dict:
        """工具参数预览：只保留键与脱敏后的标量值，防秘密进入治理库。"""

        preview: dict = {}
        for key, value in dict(tool_args or {}).items():
            if isinstance(value, str):
                from klonet_agent.runtime.governance.privacy import redact_text

                sanitized, _ = redact_text(value)
                preview[str(key)] = sanitized[:300]
            elif isinstance(value, (int, float, bool)) or value is None:
                preview[str(key)] = value
            else:
                preview[str(key)] = f"<{type(value).__name__}>"
        return preview

    def _rag_search_budget(self, scope: str) -> int:
        """Return the per-turn knowledge retrieval budget for the active profile."""

        base = RAG_SEARCH_BUDGETS.get(scope, RAG_SEARCH_BUDGETS["klonet"])
        if self.profile.name == "ops" and scope in {"klonet", "mixed"}:
            return max(base, 4)
        return base

    def compress_memory(self, history: list[dict], token: int):
        """触发记忆复盘与压缩（旧路径，默认关闭）。

        仅当 KLONET_AGENT_ENABLE_LEGACY_COMPRESSION 打开时由 single_chat 调用。
        它让模型自行决定调用 write_memory / write_user 整篇覆盖记忆，属于
        新版候选—决策写入管线要替换的行为；保留一个版本周期用于 A/B 对比和
        安全回退，不作为默认路径。
        """

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
                self._emit_turn_message(history, comp_assistant)

                # 执行归档压缩中的工具调用。
                for tool_call in compress_response.choices[0].message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args, parse_error = self._parse_tool_arguments(tool_call)
                    if parse_error:
                        comp_tool_msg = {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": parse_error,
                        }
                        self._emit_turn_message(history, comp_tool_msg)
                        continue

                    tool_result = self.use_tool(tool_name, tool_args)

                    comp_tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    }
                    self._emit_turn_message(history, comp_tool_msg)

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

                self._emit_turn_message(history, comp_assistant)

                print(f"Klonet Agent：{compress_reply}")
                # 打入压缩标记，表示前面的内容已经被压缩归档。
                self.memory_store.append_compact_marker()
                # 重新初始化记忆，并将压缩总结加入数组，保持上下文连贯。
                history = self.init_history()
                history.append({"role": "assistant", "content": compress_reply})
                break

        return history, token

    def _memory_recall_available(self) -> bool:
        """数据库召回链路是否可用（cutover 下决定要不要降级回 Markdown）。

        "不可用"指**链路坏了**：没配 DSN、连不上库。召回结果为空不算——
        那只是"这条问题没有相关记忆"，数据库仍然是权威，不该因此把
        Markdown 全文请回来。失败会被读路径缓存（每进程只试一次），
        所以这里的降级不会在每个回合反复撞库。
        """

        if not MEMORY_PACK_ENABLED:
            return False
        return self._memory_repository() is not None

    def _markdown_memory_is_injected(self) -> bool:
        """Markdown 记忆是否仍常驻注入系统提示词。

        三种情况注入：

        1. Markdown 还是权威（legacy / shadow / compare）且按需召回没接管
           （MemoryPack 开关没开）——旧行为；
        2. **cutover（默认）但数据库召回链路不可用**——"数据库优先、没有
           数据库再降级 Markdown"的可用性兜底。降级只发生一次并留痕
           （trace ``memory_markdown_fallback``），避免每轮重复告警。

        "召回结果为空"不属于降级：那只说明这条问题没有相关记忆，不说明
        数据库坏了。
        """

        if markdown_memory_is_authoritative():
            return not MEMORY_PACK_ENABLED

        available = self._memory_recall_available()
        if available:
            return False
        if not self._markdown_fallback_notified:
            self._markdown_fallback_notified = True
            reason = (
                self._memory_read_error
                or self._memory_pipeline_error
                or "未知原因"
            )
            print(f"Klonet Agent：（记忆库不可用，本轮起降级为 Markdown 记忆：{reason}）")
            # 事件名：memory_pack_markdown_fallback（沿用 _trace_memory_pack 的前缀）。
            self._trace_memory_pack(
                "markdown_fallback",
                {"reason": reason[:200], "authority": MEMORY_AUTHORITY},
            )
        return True

    # ------------------------------------------------------- 记忆召回（阶段 5） --

    def _memory_pack_message(self, history: list[dict]) -> dict | None:
        """按当前问题构建记忆包消息；不可用时返回 None。

        失败一律降级为"这轮不注入记忆"：记忆是增强项，不该成为回答的阻塞点，
        更不该在用户请求路径上抛异常。
        """

        if not MEMORY_PACK_ENABLED:
            return None
        query_text = self._memory_query_text(history)
        if not query_text.strip():
            return None
        retriever = self._memory_retriever()
        if retriever is None:
            return None
        try:
            from klonet_agent.memory.domain import MemoryQuery

            report = retriever.retrieve(
                MemoryQuery(text=query_text, limit=MEMORY_PACK_RECALL_LIMIT)
            )
        except Exception as exc:  # noqa: BLE001 - 召回失败不能影响回答
            self._trace_memory_pack(
                "recall_failed", {"error": type(exc).__name__}
            )
            return None

        pack = self._memory_pack_builder().build(report, mode=self.profile.name)
        if pack.empty:
            self._trace_memory_pack(
                "empty",
                {
                    "query_chars": len(query_text),
                    "degraded": list(report.degraded),
                    "dropped": len(pack.dropped),
                },
            )
            return None
        self._trace_memory_pack(
            "injected",
            {
                "memory_ids": list(pack.memory_ids),
                "tokens": pack.tokens,
                "dropped": [
                    {"id": item.memory_id, "reason": item.reason}
                    for item in pack.dropped
                ],
                "degraded": list(report.degraded),
                "rerank": report.rerank,
                "conflicts": list(pack.conflict_ids),
            },
        )
        return pack.to_message()

    def _memory_query_text(self, history: list[dict]) -> str:
        """召回用的查询文本：本轮用户输入。

        取 history 里最后一条 user 消息——编译器也是这么认"当前输入"的，两处口径
        必须一致，否则会出现"注入的记忆与正在回答的问题不是同一个"这种很难查的错位。
        """

        for message in reversed(history):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
        return ""

    def _memory_retriever(self):
        """惰性构造召回器；不可用时返回 None（只记一次原因）。"""

        if self._memory_retriever_cache is not None:
            return self._memory_retriever_cache
        if self._memory_retriever_error is not None:
            return None
        if not MEMORY_PACK_ENABLED:
            return None
        repository = self._memory_repository()
        if repository is None:
            return None
        try:
            from klonet_agent.llm.embeddings import (
                EmbeddingClient,
                get_embedding_api_key,
            )
            from klonet_agent.llm.reranker import RerankClient
            from klonet_agent.memory.repository import EMBEDDING_DIMENSIONS
            from klonet_agent.memory.retriever import MemoryRetriever

            embedder = None
            if get_embedding_api_key():
                # 显式给维度：schema 是 vector(1024)，维度不符要在客户端就变成
                # 可见的降级，而不是等 SQL 报一个看不懂的错。
                embedder = EmbeddingClient(
                    dimensions=EMBEDDING_DIMENSIONS
                ).embed_text
            retriever = MemoryRetriever(
                repository, embedder=embedder, reranker=RerankClient()
            )
            self._memory_retriever_cache = retriever
            return retriever
        except Exception as exc:  # noqa: BLE001 - 召回链路不可用不能影响主链路
            self._memory_retriever_error = f"{type(exc).__name__}: {exc}"
            return None

    def _memory_pack_builder(self):
        if self._memory_pack_builder_cache is None:
            from klonet_agent.memory.pack import MemoryPackBuilder

            self._memory_pack_builder_cache = MemoryPackBuilder(
                token_budget=MEMORY_PACK_TOKEN_BUDGET
            )
        return self._memory_pack_builder_cache

    def _memory_repository(self):
        """本会话的记忆仓库（读路径）。

        优先复用写入管线已经建好的那个——两个开关同时打开时不该为同一个租户开
        两个连接池。写管线没开时读路径自己建一个：读不需要写权限，不该被写入开关
        连带关掉。
        """

        pipeline = self._memory_write_pipeline()
        if pipeline is not None:
            return pipeline.repository
        if self._memory_read_repository is not None:
            return self._memory_read_repository
        if self._memory_read_error is not None:
            return None
        try:
            from klonet_agent.memory.database import MemoryDatabase, memory_dsn
            from klonet_agent.memory.domain import Tenant
            from klonet_agent.memory.postgres import PostgresMemoryRepository

            dsn = memory_dsn()
            if not dsn:
                self._memory_read_error = "未配置记忆库 DSN"
                return None
            database = MemoryDatabase(dsn)
            database.open()
            self._memory_read_database = database
            self._memory_read_repository = PostgresMemoryRepository(
                database,
                Tenant(
                    user_id=self.session.user_id, project_id=self.session.project_id
                ),
            )
            return self._memory_read_repository
        except Exception as exc:  # noqa: BLE001
            self._memory_read_error = f"{type(exc).__name__}: {exc}"
            return None

    def _trace_memory_pack(self, event: str, payload: dict) -> None:
        """记录一次记忆包事件。trace 失败同样不能影响主链路。"""

        try:
            self.trace_logger.record_privileged_event(
                user_id=self.session.user_id,
                project_id=self.session.project_id,
                mode=self.session.mode,
                event=f"memory_pack_{event}",
                payload=payload,
            )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------- 受控写入管线（阶段 3） --

    def _memory_write_pipeline(self):
        """惰性构造本会话的受控写入管线；不可用时返回 None。

        构造失败**只记录一次**原因：记忆库没配好的话，每个回合都去连一次数据库
        既慢又吵。开关关闭（默认）时直接返回 None，主链路一行都不走。
        """

        if self._memory_pipeline is not None:
            return self._memory_pipeline
        if self._memory_pipeline_error is not None:
            return None
        if not MEMORY_WRITE_PIPELINE_ENABLED:
            return None
        try:
            # 惰性导入：没装 psycopg / 没配库的环境不该因为"有记忆功能"而起不来。
            from klonet_agent.memory.candidate_extractor import CandidateExtractor
            from klonet_agent.memory.database import MemoryDatabase, memory_dsn
            from klonet_agent.memory.domain import Tenant
            from klonet_agent.memory.postgres import PostgresMemoryRepository
            from klonet_agent.memory.write_pipeline import (
                LegacyMemoryToolBridge,
                MemoryWritePipeline,
            )
            from klonet_agent.memory.write_policy import MemoryWritePolicy

            dsn = memory_dsn()
            if not dsn:
                self._memory_pipeline_error = "未配置记忆库 DSN"
                return None

            database = MemoryDatabase(dsn)
            database.open()
            tenant = Tenant(
                user_id=self.session.user_id, project_id=self.session.project_id
            )
            repository = PostgresMemoryRepository(database, tenant)
            pipeline = MemoryWritePipeline(
                repository,
                # 复用压缩路径已有的纯文本完成函数：显式 tools=None，
                # 提取不该看到工具表，否则模型可能回 tool_calls 而不是 JSON。
                extractor=CandidateExtractor(complete=self._llm_complete_text),
                policy=MemoryWritePolicy(),
                tracer=self.trace_logger,
                backfill_limit=MEMORY_BACKFILL_LIMIT,
            )
            self.tool_executor.set_memory_tool_bridge(
                LegacyMemoryToolBridge(
                    pipeline,
                    context_provider=self._memory_turn_context,
                    event_id_provider=lambda: (
                        self._recent_event_ids[-1] if self._recent_event_ids else ""
                    ),
                )
            )
            self._memory_pipeline = pipeline
            return pipeline
        except Exception as exc:  # noqa: BLE001 - 记忆库不可用不能影响主链路
            self._memory_pipeline_error = f"{type(exc).__name__}: {exc}"
            print(f"Klonet Agent：（记忆写入管线不可用：{self._memory_pipeline_error}）")
            return None

    def _memory_turn_context(self, history: list[dict] | None = None):
        """构造当前回合的写入上下文（含"哪些来源可以被引用"的白名单）。"""

        from klonet_agent.memory.domain import Tenant
        from klonet_agent.memory.write_policy import TurnContext, allowed_source_ids

        turn_history = history if history is not None else (self._turn_history_ref or [])
        allowed = set(self._recent_event_ids)
        messages = [
            message
            for message in turn_history
            if str(message.get("event_id") or "") in allowed
        ]
        return TurnContext(
            tenant=Tenant(
                user_id=self.session.user_id, project_id=self.session.project_id
            ),
            observed_at=datetime.now(timezone.utc),
            allowed_sources=allowed_source_ids(messages),
            project_id=self.session.project_id,
            # 只有 ops 档位允许写共享运维记忆；其它档位即使模型提了也会被策略拒。
            allow_shared_ops=self.profile.name == "ops",
        )

    def _capture_memory_candidates(
        self,
        *,
        user_input: str,
        reply: str,
        history: list[dict],
        tool_events: list[dict],
    ) -> None:
        """回合持久化之后触发候选提取（计划 §7.2 的正常触发点）。

        **任何失败都只打印一行提示**：候选提取是附加收益，绝不允许它把
        已经生成的回答变成异常（"候选失败不影响用户回答"）。
        """

        if not MEMORY_WRITE_PIPELINE_ENABLED:
            return
        pipeline = self._memory_write_pipeline()
        if pipeline is None:
            return
        try:
            from klonet_agent.memory.candidate_extractor import TurnDigest

            context = self._memory_turn_context(history)
            tool_names = ", ".join(
                str(item.get("name") or "") for item in tool_events
            )
            digest = TurnDigest(
                source_event_range=self._turn_event_range(),
                user_input=user_input,
                assistant_reply=reply,
                events=tuple(
                    {
                        "role": str(message.get("role") or ""),
                        "content": str(message.get("content") or ""),
                        "event_id": str(message.get("event_id") or ""),
                    }
                    for message in history
                    if str(message.get("event_id") or "") in set(self._recent_event_ids)
                ),
                source_ids=tuple(sorted(context.allowed_sources)),
                project_id=self.session.project_id,
                extra_context=(
                    f"本轮调用过的工具：{tool_names}" if tool_names else ""
                ),
            )
            outcome = pipeline.process_turn(digest, context)
            if outcome.error:
                print(f"Klonet Agent：（记忆候选提取跳过：{outcome.error}）")
            elif outcome.accepted:
                print(
                    f"Klonet Agent：已写入 {len(outcome.accepted)} 条长期记忆候选。"
                )
        except Exception as exc:  # noqa: BLE001 - 见方法说明
            print(f"Klonet Agent：（记忆候选提取失败，已忽略：{type(exc).__name__}）")

    def _turn_event_range(self) -> dict:
        """本轮事件区间。

        两个端点**都是包含关系**（与 checkpoint 的 ``source_event_end`` 半开语义不同）：
        这里只是"这一批事件处理过没有"的台账标识，不做覆盖区间计算。
        """

        rows = list(self._recent_event_ids)
        if not rows:
            return {"start": "", "end": ""}
        return {"start": rows[0], "end": rows[-1], "kind": "turn"}

    def _backfill_memory_candidates(self) -> None:
        """checkpoint / 会话结束时补扫尚未处理的事件区间（计划 §7.2）。

        水位取自 checkpoint 的已覆盖行数：它之后的事件如果还没被任何候选登记过，
        就在这里作为**一个区间**补扫。失败同样只打印提示。
        """

        if not MEMORY_WRITE_PIPELINE_ENABLED:
            return
        pipeline = self._memory_write_pipeline()
        if pipeline is None:
            return
        try:
            from klonet_agent.memory.candidate_extractor import TurnDigest

            covered = self._current_covered_rows()
            messages = [
                message
                for message in self.memory_store.load_history_after(covered)
                if message.get("event_id")
            ]
            if not messages:
                return
            context = self._memory_turn_context(messages)
            digest = TurnDigest(
                source_event_range={
                    "start": str(messages[0]["event_id"]),
                    "end": str(messages[-1]["event_id"]),
                    "kind": "backfill",
                },
                user_input="",
                assistant_reply="",
                events=tuple(
                    {
                        "role": str(message.get("role") or ""),
                        "content": str(message.get("content") or ""),
                        "event_id": str(message.get("event_id") or ""),
                    }
                    for message in messages
                ),
                source_ids=tuple(sorted(context.allowed_sources)),
                project_id=self.session.project_id,
                extra_context=f"补扫 checkpoint 之后的事件（已覆盖 {covered} 行）。",
            )
            outcomes = pipeline.backfill([(digest, context)])
            written = sum(len(item.accepted) for item in outcomes)
            if written:
                print(f"Klonet Agent：补扫写入 {written} 条长期记忆候选。")
        except Exception as exc:  # noqa: BLE001 - 补扫失败不影响主流程
            print(f"Klonet Agent：（记忆补扫失败，已忽略：{type(exc).__name__}）")

    def _ensure_governance(self):
        """惰性构造治理层（03 计划阶段 1）。

        开关关闭或没有 DSN 时返回 ``None`` 并保持静默——治理层是可选部署件，
        与记忆系统的降级哲学一致。构造成功后把 ``session.on_todos_updated``
        指向 ``apply_todos``：todos 的内存变更从此先过权威状态机（fail closed）。
        """

        if self._governance is not None or self._governance_error is not None:
            return self._governance
        if not RUNTIME_GOVERNANCE_ENABLED:
            self._governance_error = "disabled"
            return None
        try:
            from klonet_agent.memory.domain import Tenant
            from klonet_agent.runtime.governance.bootstrap import (
                runtime_governance_from_env,
            )

            tenant = Tenant(
                user_id=self.session.user_id,
                project_id=self.session.project_id,
            )
            governance = runtime_governance_from_env(
                tenant,
                session_id=f"{self.session.user_id}:{self.session.project_id}",
                mode=self.session.mode,
                trace_file=GOVERNANCE_TRACE_FILE,
            )
        except Exception as exc:  # 构造失败：记录原因，本轮起保持关闭。
            self._governance_error = str(exc)
            print(f"Klonet Agent：运行治理层初始化失败，已降级关闭（{self._governance_error}）")
            return None
        if governance is None:
            self._governance_error = "missing-dsn"
            return None
        self._governance = governance
        self.session.on_todos_updated = governance.apply_todos
        return governance

    def single_chat(self, user_input: str, history: list[dict], token: int):
        """实现一次完整的用户输入处理。

        这对应旧版 runner.py 中的内循环：
        用户输入 -> 调用 LLM -> 可能调用工具 -> 把工具结果送回 LLM -> 输出自然语言。
        """

        # 治理层：每轮一次 turn 事件（telemetry 级，失败不阻断对话）。
        governance = self._ensure_governance()
        if governance is not None:
            try:
                governance.start_turn()
            except Exception as exc:
                print(f"Klonet Agent：治理层本轮不可用（{exc}），telemetry 将缓冲补投。")

        # 设定对话消息。消息列表中的每个消息都包含 role 和 content。
        # role 可以是 system、user、assistant、tool。
        recent_history_for_intent = self._recent_dialogue_history(
            history,
            limit=20 if self.profile.name == "ops" else 6,
        )
        # 本轮事件从空开始计数：写入管线只允许引用本轮真实出现过的事件行号，
        # 上一轮的行号留在列表里会让"来源能不能核对"变成摆设。
        self._recent_event_ids = []
        self._turn_history_ref = history
        self._emit_turn_message(history, {"role": "user", "content": user_input})

        reply = ""
        tool_rounds = 0
        todo_continuations = 0
        tool_events: list[dict] = []
        reasoning_trace_printed = False
        self._query_intent = None
        self._intent_decision = None
        self._retrieval_plan = None
        self._turn_intent = None
        self._turn_decision = None
        self._knowledge_search_count = 0
        self._ops_route = None
        semantic_frame = None
        resume_state = self._resume_state_for(user_input)
        resume_paused_turn = (
            resume_state is not None
            and resume_state is self._paused_turn_state
        )
        resume_previous_turn = resume_state is not None
        effective_user_input = user_input
        turn_resume_message = None
        if hasattr(self.tool_executor, "set_user_authorization_context"):
            self.tool_executor.set_user_authorization_context(user_input)

        privileged_result = self._supervise_privileged_turn(
            user_input,
            recent_history=recent_history_for_intent,
        )
        if privileged_result is not None and privileged_result.handled:
            privileged_reply = privileged_result.message
            assistant_msg = {"role": "assistant", "content": privileged_reply}
            self._emit_turn_message(history, assistant_msg)
            self._record_privileged_turn(user_input, privileged_result)
            print(f"Klonet Agent：{privileged_reply}")
            return privileged_reply, history, token

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
            self._retrieval_plan = state.get("retrieval_plan")
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
        if (
            self.profile.name == "mentor"
            or self.profile.name == "ops" and self._ops_semantic_routing
        ) and not resume_previous_turn:
            try:
                print_progress("正在理解你的问题...")
                analysis = self.intent_analyzer.analyze(
                    user_input,
                    recent_history=recent_history_for_intent,
                )
                token += analysis.token_usage
                self._query_intent = analysis.intent
                self._intent_decision = analysis.decision
                self._retrieval_plan = analysis.retrieval_plan
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
                if self.profile.name == "ops":
                    self._ops_route = route_ops_request(
                        user_input, intent=self._turn_intent,
                    )
                print_progress(self._progress_intent_summary())
            except Exception:
                self._query_route = route_query(user_input)
                self._query_intent = None
                self._intent_decision = None
                self._retrieval_plan = None
                self._refresh_turn_plan(
                    user_input,
                    recent_history=recent_history_for_intent,
                )
                if self.profile.name == "ops":
                    self._ops_route = route_ops_request(
                        user_input, intent=self._turn_intent,
                    )
                print_progress(self._progress_intent_summary())
        elif not resume_previous_turn:
            self._query_route = route_query(user_input)
            self._refresh_turn_plan(
                user_input,
                recent_history=recent_history_for_intent,
            )
            if self.profile.name == "ops":
                self._ops_route = route_ops_request(
                    user_input, intent=self._turn_intent,
                )
            print_progress(self._progress_intent_summary())

        if (
            self.profile.name == "mentor"
            and self._query_intent is not None
            and self._query_intent.task_type == "credential_boundary"
            and not resume_previous_turn
        ):
            credential_boundary = decide_pre_llm_clarification(user_input)
            if credential_boundary.should_stop:
                reply = credential_boundary.reply
                assistant_msg = {"role": "assistant", "content": reply}
                self._emit_turn_message(history, assistant_msg)
                clear_thinking_prompt()
                print(f"Klonet Agent\uff1a{reply}")
                return reply, history, token

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
                self._emit_turn_message(history, assistant_msg)
                clear_thinking_prompt()
                print(f"Klonet Agent\uff1a{reply}")
                return reply, history, token

        turn_scope_message = self._build_turn_scope_message(effective_user_input)
        history.append(turn_scope_message)
        turn_answer_policy_message = None
        ops_environment_plan_message = None
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

        def refresh_ops_environment_plan():
            nonlocal ops_environment_plan_message
            if self.profile.name != "ops":
                return
            operation = (
                self._query_intent.operation
                if self._query_intent is not None
                else "unknown"
            )
            plan = build_ops_environment_plan(
                user_input=effective_user_input,
                operation=operation,
                tool_events=tool_events,
            )
            if not plan:
                return
            if ops_environment_plan_message is None:
                ops_environment_plan_message = {"role": "system", "content": plan}
                history.append(ops_environment_plan_message)
            else:
                ops_environment_plan_message["content"] = plan

        # 工具循环有明确上限，避免模型反复调用工具后阻塞 CLI。
        while tool_rounds < self._max_tool_rounds():
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

            refresh_ops_environment_plan()
            try:
                response = self.chat_with_llm(
                    history,
                    stream=True,
                    on_delta=print_reply_delta,
                )
            except ContextOverflowError as exc:
                # 上下文必需区超过硬预算：不回退、不截断，直接给出确定性本地
                # 错误并结束本回合，禁止向供应商发送超限请求。
                clear_thinking_prompt()
                areas = "、".join(
                    f"{name}={value}" for name, value in sorted((exc.areas or {}).items())
                )
                overflow_reply = (
                    "【本地错误】本轮上下文（系统规则 + 检查点 + 当前输入）"
                    "已超过当前模型的硬输入上限，为避免请求被供应商拒绝，"
                    "本轮未调用模型。请缩短当前输入，或开一个新的会话/项目继续。"
                )
                if areas:
                    overflow_reply += f"（预算占用：{areas}）"
                self.trace_logger.record_privileged_event(
                    user_id=self.session.user_id,
                    project_id=self.session.project_id,
                    mode=self.session.mode,
                    event="context_overflow_refused",
                    payload={"areas": exc.areas, "detail": str(exc)[:500]},
                )
                print(f"\nKlonet Agent：{overflow_reply}")
                return overflow_reply, history, token
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
                self._emit_turn_message(history, assistant_msg)

                for tool_call in response.choices[0].message.tool_calls:
                    # 工具名，即 schema 中 function.name。
                    tool_name = tool_call.function.name
                    # 工具参数，即 schema 中 function.parameters 约束出的标准 JSON。
                    tool_args, parse_error = self._parse_tool_arguments(tool_call)
                    if parse_error:
                        self._print_tool_loop_observation(tool_name, parse_error)
                        tool_events.append(
                            {"name": tool_name, "args": {}, "result": parse_error}
                        )
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": parse_error,
                        }
                        self._emit_turn_message(history, tool_msg)
                        continue
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
                        if self._retrieval_plan is not None:
                            # Reuse the front-loaded model call.  The retrieval
                            # layer must not make a second planning call.
                            tool_args["retrieval_plan"] = plan_as_dict(
                                self._retrieval_plan
                            )
                    # 调用工具函数，开始执行命令或其他动作。
                    self._print_tool_loop_action(tool_name, tool_args)
                    result = self.use_tool(tool_name, tool_args)
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
                    self._emit_turn_message(history, tool_msg)

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
                        self._emit_turn_message(
                            history,
                            {"role": "user", "content": continue_prompt},
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

                self._emit_turn_message(history, assistant_msg)
                self._maintain_project_journal(effective_user_input, reply)
                self._record_ops_shared_turn(effective_user_input, tool_events, reply)
                self._last_turn_state = self._snapshot_turn_state(effective_user_input)
                self._paused_turn_state = None
                break

        else:
            reply = "本轮工具调用已达到上限，任务已暂停，等待用户确认后继续。"
            self._set_unfinished_todo_status("waiting_user")
            self._paused_turn_state = self._snapshot_turn_state(effective_user_input)
            self._last_turn_state = self._paused_turn_state
            assistant_msg = {"role": "assistant", "content": reply}
            self._emit_turn_message(history, assistant_msg)
            self._maintain_project_journal(effective_user_input, reply)
            print(f"Klonet Agent\uff1a{reply}")

        # 本轮作用域只约束当前用户输入，不写入长期历史，避免影响下一轮。
        history = [
            message
            for message in history
            if (
                message is not turn_scope_message
                and message is not turn_answer_policy_message
                and message is not ops_environment_plan_message
                and message is not turn_resume_message
            )
        ]

        # 旧版后置压缩：默认关闭。新主链路在调用前由 ContextCompiler 按预算
        # 编译，超过软阈值时生成 TaskCheckpoint，不再依赖本轮 token 计数。
        # 只在显式打开 KLONET_AGENT_ENABLE_LEGACY_COMPRESSION 时保留该回退。
        if LEGACY_MEMORY_COMPRESSION_ENABLED:
            current_context_size = response.usage.total_tokens
            if current_context_size >= MAX_TOKEN:
                print(
                    f"\nKlonet Agent：当前上下文约 {current_context_size} token，"
                    "开始整理记忆（旧压缩路径）。"
                )
                history, token = self.compress_memory(history, token)

        # 受控写入管线（记忆系统阶段 3，默认关闭）：回合事件全部持久化之后再提取候选。
        # 放在这里而不是工具循环内部，是因为计划 §7.2 的触发点就是
        # "一个用户回合及其工具循环成功持久化之后"。任何失败都不影响上面的回答。
        self._capture_memory_candidates(
            user_input=effective_user_input,
            reply=reply,
            history=history,
            tool_events=tool_events,
        )

        return reply, history, token

    def _supervise_privileged_turn(
        self,
        user_input: str,
        *,
        recent_history: list[dict] | None = None,
    ):
        """Send every Ops-Privilege turn through the Supervisor control plane."""

        if self.profile.name != "ops":
            return None
        if self.privileged_supervisor is None:
            raise RuntimeError("Ops-Privilege Supervisor is unavailable")
        conversation_context = self._privileged_conversation_context(
            recent_history or []
        )
        handle_with_context = getattr(
            self.privileged_supervisor,
            "handle_with_context",
            None,
        )
        if handle_with_context is not None:
            return handle_with_context(
                user_input,
                environment_context="",
                conversation_context=conversation_context,
            )
        return self.privileged_supervisor.handle(user_input, environment_context="")

    @staticmethod
    def _privileged_conversation_context(history: list[dict]) -> str:
        """Return a small dialogue-only context for pronoun and continuation recovery."""

        lines = []
        for message in history[-20:]:
            role = str(message.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = " ".join(str(message.get("content") or "").split())
            if not content:
                continue
            lines.append("%s: %s" % (role, content[:900]))
        return "\n".join(lines)[-12000:]

    def _record_privileged_event(self, event: str, payload: dict) -> None:
        self.trace_logger.record_privileged_event(
            user_id=self.session.user_id,
            project_id=self.session.project_id,
            mode=self.session.mode,
            event=event,
            payload=payload,
        )

    def _record_privileged_turn(self, user_input: str, result) -> None:
        """Persist the canonical privileged result through existing memory/log stores."""

        plan = getattr(result, "plan", None)
        failure = getattr(result, "failure", None)
        outcome = getattr(result, "outcome", None)
        evidence = getattr(result, "evidence", None)
        records = list(getattr(evidence, "records", ()) or ())
        plan_id = str(getattr(plan, "plan_id", "") or "")
        failure_id = str(getattr(failure, "failure_id", "") or "")
        payload = {
            "kind": str(getattr(result, "kind", "") or "unknown"),
            "plan_id": plan_id,
            "plan_status": str(getattr(plan, "status", "") or ""),
            "failure_id": failure_id,
            "failure_stage": str(getattr(failure, "stage", "") or ""),
            "goal_status": str(getattr(outcome, "status", "") or ""),
            "evidence_count": len(records),
        }
        self._record_privileged_event("privileged_workflow_result", payload)

        if not any((plan is not None, failure is not None, records)):
            return
        goal = str(
            getattr(plan, "goal", "")
            or getattr(failure, "goal", "")
            or getattr(evidence, "goal", "")
            or user_input
        ).strip()
        roots = sorted({
            str(resource.value)
            for resource in list(getattr(plan, "resources", ()) or ())
            if str(getattr(resource, "role", "") or "")
            in {"instance_root", "target_root", "deployment_root"}
        })
        identifiers = sorted({
            str(resource.value)
            for resource in list(getattr(plan, "resources", ()) or ())
            if str(getattr(resource, "role", "") or "")
            in {
                "instance_identifier", "instance_name",
                "platform_instance_name",
            }
        })
        for record in records:
            request = getattr(record, "request", None)
            if str(getattr(request, "probe", "") or "") != "user_decision":
                continue
            args = dict(getattr(request, "args", {}) or {})
            target_root = str(args.get("target_directory") or "").strip()
            if target_root.startswith("/") and target_root not in roots:
                roots.append(target_root)
            target_name = str(args.get("instance_identifier") or "").strip()
            if target_name and target_name not in identifiers:
                identifiers.append(target_name)
        if not identifiers:
            match = re.search(
                r"(?:平台名|实例名|instance\s+name)\s*[:：=是为]?\s*"
                r"([A-Za-z0-9_.-]{2,64})",
                goal,
                re.I,
            )
            if match is not None:
                identifiers.append(match.group(1))
        roots = sorted(dict.fromkeys(roots))
        identifiers = sorted(dict.fromkeys(identifiers))
        target_identity = ", ".join([*identifiers, *roots])
        probe_names = list(dict.fromkeys(
            str(getattr(getattr(record, "request", None), "probe", "") or "")
            for record in records
            if str(getattr(getattr(record, "request", None), "probe", "") or "")
        ))
        episode_lines = [
            "## Ops-Privilege 工作流记录",
            "- goal: %s" % self._compact_observation_text(goal, 500),
            "- result: %s" % payload["kind"],
        ]
        if plan_id:
            episode_lines.append(
                "- plan: %s (%s)" % (plan_id, payload["plan_status"] or "unknown")
            )
        if failure_id:
            episode_lines.append(
                "- failure: %s (%s)" % (
                    failure_id, payload["failure_stage"] or "unknown",
                )
            )
        if roots:
            episode_lines.append("- target_roots: %s" % ", ".join(roots))
        if probe_names:
            episode_lines.append("- evidence_probes: %s" % ", ".join(probe_names))
        episode_lines.append(
            "- conclusion: %s"
            % self._compact_observation_text(str(getattr(result, "message", "") or ""), 700)
        )
        self.memory_store.append_episode("\n".join(episode_lines))

        if records:
            evidence_lines = [
                "%s: status=%s evidence_id=%s" % (
                    str(getattr(getattr(record, "request", None), "probe", "") or "unknown"),
                    str(getattr(record, "status", "") or "unknown"),
                    str(getattr(record, "evidence_id", "") or "none"),
                )
                for record in records[:12]
            ]
            self.memory_store.append_shared_ops_record(
                question=user_input,
                intent="ops / %s" % payload["kind"],
                target=target_identity or "未确认",
                tools=probe_names,
                evidence=evidence_lines,
                conclusion=str(getattr(result, "message", "") or ""),
                confidence=(
                    "high" if payload["goal_status"] == "achieved" else "medium"
                ),
                caveat=(
                    "运行态证据会过期；再次执行或引用端口、进程和 Screen 状态前必须刷新。"
                ),
            )

    def _parse_tool_arguments(self, tool_call) -> tuple[dict, str]:
        """Parse model tool JSON without allowing malformed output to crash the CLI."""

        raw = str(getattr(tool_call.function, "arguments", "") or "")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, (
                "Error: invalid_tool_arguments_json "
                f"tool={tool_call.function.name} position={exc.pos}. "
                "The tool was not executed. Resubmit one valid JSON object matching the tool schema; "
                "do not embed a Shell command, and prefer incremental write_ops_file edits over large file content."
            )
        if not isinstance(parsed, dict):
            return {}, (
                "Error: invalid_tool_arguments_type "
                f"tool={tool_call.function.name}. The tool was not executed; arguments must be one JSON object."
            )
        return parsed, ""

    def _show_visible_reasoning_trace(self) -> bool:
        """默认输出用户可见思考摘要；brief 模式只输出最终答案。"""

        return self.profile.name != "ops" and self.answer_style != "brief"

    def _max_tool_rounds(self) -> int:
        """Return the tool loop budget for the current profile."""

        if self.profile.name == "ops":
            return OPS_MAX_TOOL_ROUNDS
        return MAX_TOOL_ROUNDS

    def _show_progress_updates(self) -> bool:
        """Show safe CLI progress milestones without adding them to model context."""

        return self.profile.name in {"mentor", "ops"} and self.answer_style != "brief"

    def _print_tool_loop_action(self, tool_name: str, tool_args: dict) -> None:
        """Print one safe Ops action before a tool executes."""

        if self.profile.name != "ops" or self.answer_style == "brief":
            return
        print(f"Klonet Agent：{self._format_tool_action(tool_name, tool_args)}")

    def _format_tool_action(self, tool_name: str, tool_args: dict) -> str:
        """Return a deterministic action using only allowlisted arguments."""

        plan_action = self._format_ops_plan_action(tool_name, tool_args)
        if plan_action:
            return plan_action

        actions = {
            "search_knowledge": ("正在检索知识库", "query"),
            "search_shared_ops_memory": ("正在检索历史诊断", "query"),
            "search_code": ("正在搜索源码", "query"),
            "list_source_files": ("正在查看源码目录", "path"),
            "list_files": ("正在查看目录", "path"),
            "read_source_file": ("正在读取源码", "path"),
            "read_file": ("正在读取文件", "path"),
            "read_ops_file": ("正在读取运维文件", "path"),
            "read_klonet_logs": ("正在读取 Klonet 日志", "path"),
            "inspect_screen_session": ("正在检查 screen 会话", "session"),
            "inspect_system_environment": ("正在检查系统环境", "checks"),
            "inspect_docker_containers": ("正在查看 Docker 容器", "name"),
            "run_readonly_command": ("正在执行只读诊断命令", "program"),
            "inspect_ops_context": ("正在检查运维环境", "checks"),
            "inspect_platform_instances": ("正在盘点 Klonet 平台实例", "project_roots"),
            "inspect_klonet_runtime": ("正在检查 Klonet 运行状态", "checks"),
        }
        action, key = actions.get(tool_name, (f"正在执行工具：{tool_name}", ""))
        if not key or key not in tool_args:
            return action
        raw_value = tool_args[key]
        if isinstance(raw_value, list):
            raw_value = "、".join(str(item) for item in raw_value)
        value = " ".join(str(raw_value).split())
        if len(value) > 120:
            value = value[:117] + "..."
        return f"{action}：{value}" if value else action

    def _format_ops_plan_action(self, tool_name: str, tool_args: dict) -> str:
        """Return stable action text for controlled Ops operation-plan tools."""

        if tool_name == "create_ops_operation_plan":
            operation = self._safe_action_value(tool_args.get("operation"), 80)
            target = self._safe_action_value(tool_args.get("target"), 80) or "unknown"
            return f"创建运维计划：{operation or 'operation'} / {target}"
        if tool_name == "approve_ops_operation_plan":
            plan_id = self._safe_action_value(tool_args.get("plan_id"), 80) or "unknown"
            scope = self._safe_action_value(tool_args.get("scope"), 20) or "plan"
            if scope == "step":
                step_id = self._safe_action_value(tool_args.get("step_id"), 80) or "unknown-step"
                return f"确认高风险步骤：{step_id}（{plan_id}）"
            return f"确认运维计划：{plan_id}"
        if tool_name == "execute_ops_next_step":
            plan_id = self._safe_action_value(tool_args.get("plan_id"), 80) or "unknown"
            return f"执行下一步：{plan_id}"
        if tool_name == "execute_ops_operation_step":
            plan_id = self._safe_action_value(tool_args.get("plan_id"), 80) or "unknown"
            step_id = self._safe_action_value(tool_args.get("step_id"), 80) or "unknown-step"
            return f"执行步骤：{step_id}（{plan_id}）"
        return ""

    def _safe_action_value(self, value, limit: int) -> str:
        """Compact one tool argument for progress output."""

        text = " ".join(str(value or "").split())
        if len(text) > limit:
            return text[: limit - 3] + "..."
        return text

    def _print_tool_loop_observation(self, tool_name: str, result: str) -> None:
        """Print bounded real tool output as a user-facing Ops milestone."""

        if self.profile.name != "ops" or self.answer_style == "brief":
            return
        lines, omitted = self._tool_observation_lines(tool_name, result)
        llm_summary = self._llm_ops_milestone_summary(tool_name, result, lines, omitted)
        if llm_summary:
            print(f"Klonet Agent：{llm_summary}")
            return
        print(f"Klonet Agent：{self._format_ops_milestone_summary(tool_name, lines, omitted)}")

    def _llm_ops_milestone_summary(
        self,
        tool_name: str,
        result: str,
        lines: list[str],
        omitted: bool,
    ) -> str:
        """Ask the real LLM to turn an Ops tool result into a short user summary."""

        llm = getattr(self, "llm", None)
        if not isinstance(llm, LLMClient):
            return ""
        raw_result = self._compact_observation_text(result, 1800)
        extracted = "\n".join(lines[:6])
        prompt = (
            "你是 Klonet Agent 的命令行进度摘要器。"
            "请把工具结果改写成面向普通用户的自然语言中文摘要。\n"
            "要求：只输出一句话；不要输出 JSON、字段名、英文状态码或原始 stdout/stderr；"
            "不要编造工具结果里没有的结论；如果结果不足以判断，就明确说还需要继续确认。\n\n"
            f"工具名：{tool_name}\n"
            f"是否省略了部分结果：{omitted}\n"
            f"已抽取的关键行：\n{extracted}\n\n"
            f"原始工具结果：\n{raw_result}"
        )
        try:
            response = llm.complete(
                messages=[
                    {
                        "role": "system",
                        "content": "你只负责把运维工具结果改写成一句用户能看懂的中文进度摘要。",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                stream=False,
            )
        except Exception:
            return ""
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        summary = self._compact_observation_text(getattr(message, "content", ""), 180)
        prefix = "Klonet Agent："
        if summary.startswith(prefix):
            summary = summary[len(prefix):]
        summary = summary.strip()
        if self._ops_summary_leaks_machine_trace(summary):
            return ""
        return summary

    @staticmethod
    def _ops_summary_leaks_machine_trace(summary: str) -> bool:
        forbidden = {
            "returncode",
            "stdout",
            "stderr",
            "program_not_allowlisted",
            "evidence_type",
            "readonly_command",
            "argv_json",
            "current_state=",
            "project_root_status",
        }
        return any(token in summary for token in forbidden)

    def _format_ops_milestone_summary(
        self,
        tool_name: str,
        lines: list[str],
        omitted: bool,
    ) -> str:
        """Turn tool observations into a compact key-node summary."""

        action = self._human_tool_result_action(tool_name)
        if not lines:
            return f"{action}，这一步没有返回可展示结果。"

        raw_cleaned_lines = [
            cleaned
            for line in lines
            if (cleaned := self._clean_ops_summary_line(line))
        ]
        cleaned_lines = self._naturalize_ops_summary_lines(
            tool_name,
            raw_cleaned_lines,
        ) or [
            natural
            for cleaned in raw_cleaned_lines
            if (natural := self._naturalize_ops_summary_line(tool_name, cleaned))
        ]
        details = "；".join(cleaned_lines)
        details = self._compact_observation_text(details, 260)
        if not details:
            return f"{action}，这一步没有返回可展示结果。"

        if len(lines) == 1 and not omitted:
            return f"{action}：{details}"
        suffix = "；其余内容已省略" if omitted else ""
        return f"{action}，这一步的结论是：{details}{suffix}。"

    @staticmethod
    def _clean_ops_summary_line(line: str) -> str:
        """Remove trace-style bullets while keeping the evidence text readable."""

        text = " ".join(str(line or "").split())
        while text.startswith("- "):
            text = text[2:].strip()
        return text

    def _naturalize_ops_summary_line(self, tool_name: str, text: str) -> str:
        """Translate machine-oriented trace fragments into plain operational Chinese."""

        if not text:
            return ""
        if text.startswith("失败："):
            return self._naturalize_ops_failure(text)
        if "program_not_allowlisted=" in text:
            program = self._extract_after(text, "program_not_allowlisted=")
            return f"当前只读策略不允许执行 `{program}`，所以这一步没有拿到结果。"
        if "file does not exist or is not a file" in text:
            path = text.split(":", 1)[0].strip() if ":" in text else text.split()[0]
            return f"没有找到日志文件 `{path}`。"
        if "returncode=0" in text and "readonly_command" in text:
            stdout = self._extract_semicolon_value(text, "stdout")
            if stdout:
                return f"只读命令执行成功，输出为：{self._compact_observation_text(stdout, 120)}。"
            return "只读命令执行成功，但没有返回可用于判断的输出。"
        if text.startswith("platform="):
            fields = self._semicolon_key_values(text)
            platform = fields.get("platform") or "当前平台"
            project_root = fields.get("project_root")
            status = fields.get("project_root_status")
            if project_root and status == "detected":
                return f"识别到 {platform} 平台目录 `{project_root}`。"
        if "detected - evidence_type=screen_scrollback" in text:
            session = text.split(":", 1)[0].strip()
            return f"找到 `{session}` 的 screen 历史快照，但这不是实时运行状态，需要继续结合进程或端口确认。"
        if ": detected -" in text:
            name, detail = text.split(": detected -", 1)
            return self._naturalize_detected_line(name.strip(), detail.strip())
        return text

    def _naturalize_ops_summary_lines(
        self,
        tool_name: str,
        lines: list[str],
    ) -> list[str]:
        joined = "; ".join(lines)
        if "readonly_command" in joined and "returncode=0" in joined:
            stdout = self._extract_semicolon_value(joined, "stdout")
            if stdout:
                return [f"只读命令执行成功，输出为：{self._compact_observation_text(stdout, 120)}。"]
            return ["只读命令执行成功，但没有返回可用于判断的输出。"]
        return []

    def _naturalize_ops_failure(self, text: str) -> str:
        detail = text[len("失败：") :].strip()
        if detail.startswith("ip only allows"):
            return "这条 `ip` 命令超出只读白名单；当前只允许查看 addr、link 或 route。"
        if detail.startswith("source index unavailable"):
            return "源码索引暂时不可用。"
        return f"执行失败：{detail}"

    def _naturalize_detected_line(self, name: str, detail: str) -> str:
        if name == "redis":
            if "active" in detail:
                return "Redis 服务处于运行状态。"
            return f"Redis 状态：{detail}。"
        if name == "docker":
            return f"发现 Docker 容器信息：{detail}。"
        if name == "ports":
            return f"发现监听端口：{detail}。"
        if name == "screen":
            return f"发现 screen 会话：{detail}。"
        return f"{name} 已检测到：{detail}。"

    @staticmethod
    def _extract_semicolon_value(text: str, key: str) -> str:
        fields = AgentOrchestrator._semicolon_key_values(text)
        return fields.get(key, "")

    @staticmethod
    def _semicolon_key_values(text: str) -> dict[str, str]:
        fields = {}
        for part in str(text or "").split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
        return fields

    @staticmethod
    def _human_tool_result_action(tool_name: str) -> str:
        actions = {
            "search_knowledge": "我已经检索知识库",
            "search_shared_ops_memory": "我已经检索历史诊断",
            "search_code": "我已经搜索源码",
            "list_source_files": "我已经查看源码目录",
            "list_files": "我已经查看目录",
            "read_source_file": "我已经读取源码",
            "read_file": "我已经读取文件",
            "read_ops_file": "我已经读取运维文件",
            "read_klonet_logs": "我已经读取 Klonet 日志",
            "inspect_screen_session": "我已经检查 screen 会话",
            "inspect_system_environment": "我已经检查系统环境",
            "inspect_docker_containers": "我已经查看 Docker 容器",
            "run_readonly_command": "我已经执行只读诊断",
            "inspect_ops_context": "我已经检查运维环境",
            "inspect_platform_instances": "我已经盘点 Klonet 平台实例",
            "inspect_klonet_runtime": "我已经检查 Klonet 运行状态",
            "create_ops_operation_plan": "我已经创建运维计划",
            "list_ops_operation_plans": "我已经查看运维计划",
            "describe_ops_operation_plan": "我已经查看运维计划详情",
            "approve_ops_operation_plan": "我已经确认运维计划",
            "execute_ops_operation_step": "我已经执行运维步骤",
            "execute_ops_next_step": "我已经推进运维计划",
            "resolve_ops_blocked_step": "我已经处理阻塞步骤",
        }
        return actions.get(tool_name, "我已经完成这一步")

    def _tool_observation_lines(
        self,
        tool_name: str,
        result: str,
    ) -> tuple[list[str], bool]:
        """Extract meaningful, bounded lines from a tool result."""

        if tool_name == "search_knowledge":
            knowledge_lines = self._knowledge_observation_lines(result)
            if knowledge_lines:
                return knowledge_lines, False
        if tool_name in {
            "create_ops_operation_plan",
            "list_ops_operation_plans",
            "describe_ops_operation_plan",
            "approve_ops_operation_plan",
            "execute_ops_operation_step",
            "execute_ops_next_step",
            "resolve_ops_blocked_step",
        }:
            ops_lines = self._ops_operation_observation_lines(result)
            if ops_lines:
                return ops_lines

        candidates = []
        for raw_line in (result or "").splitlines():
            line = raw_line.strip()
            if not line or line == tool_name or line in {"```", "```text"}:
                continue
            if line.startswith("Error:"):
                line = "失败：" + line[len("Error:") :].strip()
            if len(line) > 160:
                line = line[:157] + "..."
            candidates.append(line)
        if not candidates:
            return ["工具未返回可展示结果。"], False
        return candidates[:3], len(candidates) > 3

    def _ops_operation_observation_lines(self, result: str) -> tuple[list[str], bool]:
        """Summarize OperationPlan state-machine output for humans."""

        blocks = []
        current = []
        for raw_line in (result or "").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped in {"```", "```text"}:
                continue
            if stripped == "---":
                if current:
                    blocks.append(current)
                    current = []
                continue
            current.append(stripped)
        if current:
            blocks.append(current)
        if not blocks:
            return [], False

        candidates = []
        for block in blocks:
            header = block[0] if block else ""
            if header == "ops_operation_plan":
                candidates.extend(self._summarize_ops_plan_block(block))
            elif header == "ops_operation_execution":
                candidates.extend(self._summarize_ops_execution_block(block))
            elif header == "ops_operation_resolution":
                candidates.extend(self._summarize_ops_resolution_block(block))
            elif header == "ops_operation_plan_list":
                candidates.extend(self._summarize_ops_plan_list_block(block))
            else:
                candidates.extend(block[:6])
        max_lines = 40
        return candidates[:max_lines], len(candidates) > max_lines

    def _summarize_ops_plan_block(self, block: list[str]) -> list[str]:
        fields = self._ops_key_values(block)
        lines = [
            (
                f"计划 {fields.get('plan_id', 'unknown')}："
                f"{self._human_operation(fields.get('operation', 'unknown'))} / "
                f"{fields.get('target', 'unknown')}，"
                f"{self._human_plan_status(fields.get('status', 'unknown'))}"
            )
        ]
        if fields.get("objective"):
            lines.append(f"目标：{self._compact_observation_text(fields['objective'], 180)}")
        progress = self._ops_progress_line(block)
        if progress:
            lines.append(progress)
        next_step = fields.get("next_step") or self._first_prefixed_value(block, "next_step=")
        if next_step and next_step != "none":
            lines.append(f"下一步：{next_step}")
        step_summaries = self._ops_step_summaries(block)
        if step_summaries:
            lines.append("步骤：")
            lines.extend(f"  - {item}" for item in step_summaries[:8])
        return lines

    def _summarize_ops_execution_block(self, block: list[str]) -> list[str]:
        fields = self._ops_key_values(block)
        step = fields.get("execute_step", "unknown")
        title = fields.get("step_title", "")
        raw_status = fields.get("result_status", fields.get("step_status", "unknown"))
        status = self._human_step_status(raw_status)
        prefix = f"执行 {step}"
        if title:
            prefix += f"（{title}）"
        lines = [f"{prefix}：{status}"]
        result = fields.get("execution_result") or fields.get("observation")
        if result:
            lines.extend(self._human_execution_result_lines(result, raw_status))
        next_action = fields.get("next_required_action")
        if next_action:
            lines.append(f"需要你操作：{next_action}")
        return lines

    def _summarize_ops_resolution_block(self, block: list[str]) -> list[str]:
        fields = self._ops_key_values(block)
        lines = [
            (
                f"解除阻塞 {fields.get('resolved_step', 'unknown')}："
                f"{fields.get('result_status', fields.get('step_status', 'unknown'))}"
            )
        ]
        if fields.get("resolution_evidence"):
            lines.append(
                "依据："
                + self._compact_observation_text(fields["resolution_evidence"], 220)
            )
        if fields.get("next_required_action"):
            lines.append(f"下一步：{fields['next_required_action']}")
        return lines

    def _summarize_ops_plan_list_block(self, block: list[str]) -> list[str]:
        fields = self._ops_key_values(block)
        lines = [f"计划列表：共 {fields.get('count', '0')} 个"]
        for line in block:
            if not line.startswith("plan "):
                continue
            lines.append(
                "  - "
                + self._compact_observation_text(line[len("plan ") :], 220)
            )
        return lines

    @staticmethod
    def _ops_key_values(lines: list[str]) -> dict[str, str]:
        fields = {}
        for line in lines:
            if "=" not in line or line.startswith("  - "):
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and re.fullmatch(r"[A-Za-z0-9_.-]+", key):
                fields[key] = value.strip()
        return fields

    @staticmethod
    def _first_prefixed_value(lines: list[str], prefix: str) -> str:
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip()
        return ""

    def _ops_step_summaries(self, lines: list[str]) -> list[str]:
        summaries = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("- ") or ":" not in stripped:
                continue
            if " risk=" not in stripped or " status=" not in stripped:
                continue
            step_id, rest = stripped[2:].split(":", 1)
            title = rest.split(" risk=", 1)[0].strip()
            status = self._ops_token_value(rest, "status")
            permission = self._ops_token_value(rest, "permission")
            pieces = [title, self._human_step_status(status)]
            if permission == "step_confirm_required":
                pieces.append("需要二次确认")
            summaries.append(f"{step_id}：" + "；".join(pieces))
        return summaries

    def _ops_binding_summaries(self, lines: list[str]) -> list[str]:
        summaries = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("- ") or ":" not in stripped:
                continue
            if " recipe=" not in stripped:
                continue
            step_id, rest = stripped[2:].split(":", 1)
            action = self._ops_token_value(rest, "action")
            recipe = self._ops_token_value(rest, "recipe")
            args = [
                part
                for part in rest.split()
                if part.startswith("recipe_args.")
            ]
            pieces = []
            if action:
                pieces.append(f"动作 {action}")
            if recipe:
                pieces.append(f"执行器 {recipe}")
            if args:
                pieces.append(self._compact_observation_text(" ".join(args), 180))
            summaries.append(f"{step_id}：" + "；".join(pieces))
        return summaries

    def _ops_progress_line(self, lines: list[str]) -> str:
        fields = self._ops_key_values(lines)
        total = fields.get("total_steps")
        completed = fields.get("completed", "0")
        failed = fields.get("failed", "0")
        blocked = fields.get("blocked", "0")
        running = fields.get("running", "0")
        parts = []
        if total:
            parts.append(f"进度：已完成 {completed}/{total}")
        if failed != "0":
            parts.append(f"失败 {failed}")
        if blocked != "0":
            parts.append(f"阻塞 {blocked}")
        if running != "0":
            parts.append(f"执行中 {running}")
        return "；".join(parts)

    @staticmethod
    def _human_operation(value: str) -> str:
        return {
            "deploy_platform": "部署/安装",
            "restart_platform": "重启",
            "destroy_platform": "销毁/清理",
        }.get(value, value or "未知操作")

    @staticmethod
    def _human_plan_status(value: str) -> str:
        return {
            "pending": "等待确认",
            "approved": "已确认，执行中",
            "completed": "已完成",
            "failed": "失败",
            "aborted": "已取消",
        }.get(value, value or "状态未知")

    @staticmethod
    def _human_step_status(value: str) -> str:
        return {
            "pending": "等待执行",
            "approved": "已二次确认",
            "running": "执行中",
            "completed": "已完成",
            "failed": "失败",
            "blocked": "已阻塞",
            "normal": "等待执行",
        }.get(value, value or "状态未知")

    def _human_execution_result_lines(self, result: str, status: str) -> list[str]:
        compact = self._compact_observation_text(result, 260)
        if "command_not_allowed=" in result:
            reason = self._extract_after(result, "command_not_allowed=")
            return [f"结果：未执行，命令不在当前策略范围内（{reason}）。"]
        if "program_not_found=" in result:
            program = self._extract_after(result, "program_not_found=")
            return [f"结果：未执行，系统找不到命令 `{program}`。"]
        if "helper_failed returncode=" in result:
            code = self._extract_after(result, "helper_failed returncode=")
            lines = [f"结果：命令执行失败，返回码 {code}。"]
            if "action=run-ops-command" in result:
                program = self._extract_after(result, "program=")
                if program:
                    lines.append(f"命令类型：{program}")
            return lines
        if "dry_run=true" in result:
            return ["结果：这是预览，尚未修改环境。"]
        if status == "completed":
            if "environment unchanged" in result:
                return ["结果：已完成，没有修改环境。"]
            return ["结果：已完成。"]
        if status in {"failed", "blocked"}:
            return [f"结果：{compact}"]
        return [f"结果：{compact}"]

    @staticmethod
    def _extract_after(text: str, marker: str) -> str:
        if marker not in text:
            return ""
        value = text.split(marker, 1)[1].strip()
        if not value:
            return ""
        return value.split()[0].strip("，。；;,.")

    @staticmethod
    def _ops_token_value(text: str, key: str) -> str:
        match = re.search(rf"(?:^|\s){re.escape(key)}=([^\s]+)", text)
        return match.group(1) if match else ""

    def _knowledge_observation_lines(self, result: str) -> list[str]:
        """Summarize knowledge retrieval by cited source and evidence snippet."""

        evidence_items = []
        current_path = ""
        current_snippet: list[str] = []
        collecting_snippet = False

        def flush_current() -> None:
            nonlocal current_path, current_snippet, collecting_snippet
            snippet = " ".join(" ".join(current_snippet).split())
            if current_path and snippet:
                evidence_items.append(
                    {
                        "path": current_path,
                        "snippet": snippet,
                    }
                )
            current_path = ""
            current_snippet = []
            collecting_snippet = False

        for raw_line in (result or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("[") and "]" in line:
                flush_current()
                title = line.split("]", 1)[1].strip()
                if title:
                    current_path = title.split(" / ", 1)[0].strip()
                continue
            if line.startswith("- path:"):
                current_path = line.split(":", 1)[1].strip()
                continue
            if line.startswith("- snippet:"):
                collecting_snippet = True
                inline_snippet = line.split(":", 1)[1].strip()
                if inline_snippet:
                    current_snippet.append(inline_snippet)
                continue
            if collecting_snippet:
                current_snippet.append(line)

        flush_current()
        if not evidence_items:
            return []

        first = evidence_items[0]
        lines = [
            f"- 来源：{self._compact_observation_text(first['path'])}",
            f"- 证据：{self._compact_observation_text(first['snippet'])}",
        ]
        omitted_count = len(evidence_items) - 1
        if omitted_count:
            lines.append(f"- 另有 {omitted_count} 条证据已省略")
        return lines

    @staticmethod
    def _compact_observation_text(text: str, limit: int = 160) -> str:
        compacted = " ".join(str(text or "").split())
        if len(compacted) > limit:
            return compacted[: limit - 3] + "..."
        return compacted

    def _record_ops_shared_turn(
        self,
        user_input: str,
        tool_events: list[dict],
        reply: str,
    ) -> None:
        """Persist one completed Ops diagnosis as structured shared memory."""

        if self.profile.name != "ops" or not tool_events:
            return
        reusable_tools = {
            "inspect_klonet_runtime",
            "inspect_system_environment",
            "inspect_screen_session",
            "read_klonet_logs",
            "search_shared_ops_memory",
            "search_knowledge",
            "search_code",
            "list_source_files",
        }
        useful_events = [
            event for event in tool_events if event.get("name") in reusable_tools
        ]
        if not useful_events:
            return
        evidence = []
        for event in useful_events:
            evidence_line = self._shared_evidence_line(
                str(event.get("name") or ""),
                str(event.get("result") or ""),
            )
            if evidence_line:
                evidence.append(f"{event.get('name')}: {evidence_line}")
        if not evidence:
            return
        self.memory_store.append_shared_ops_record(
            question=user_input,
            intent=self._ops_turn_intent_summary(),
            target=self._infer_ops_target(user_input),
            tools=[str(event.get("name") or "") for event in useful_events],
            evidence=evidence,
            conclusion=reply,
            confidence=self._ops_memory_confidence(useful_events),
            caveat=(
                "运行态证据会随进程、端口、日志和 screen 输出变化而过期；"
                "再次使用该记录前必须用当前工具结果确认。"
            ),
        )

    def _ops_turn_intent_summary(self) -> str:
        """Return a compact intent label for shared Ops memory."""

        if self._turn_intent is None:
            return "unknown"
        parts = [self._turn_intent.task_type or "unknown"]
        if self._turn_intent.operation and self._turn_intent.operation != "unknown":
            parts.append(self._turn_intent.operation)
        if self._turn_intent.phase and self._turn_intent.phase != "unknown":
            parts.append(self._turn_intent.phase)
        return " / ".join(parts)

    def _infer_ops_target(self, user_input: str) -> str:
        """Infer the rough runtime target from the user's question."""

        text = user_input or ""
        lowered = text.lower()
        targets = []
        for marker in ("102", "lht", "master", "worker", "celery", "web"):
            if marker in lowered and marker not in targets:
                targets.append(marker)
        if any(word in text for word in ("哪些平台", "所有平台", "全部平台", "冲突")):
            targets.append("全机平台")
        return ", ".join(targets) if targets else "未确认"

    def _ops_memory_confidence(self, useful_events: list[dict]) -> str:
        """Give a coarse trust level for a stored Ops diagnosis."""

        tool_names = {str(event.get("name") or "") for event in useful_events}
        runtime_tools = {
            "inspect_klonet_runtime",
            "inspect_system_environment",
            "inspect_screen_session",
            "read_klonet_logs",
            "read_ops_file",
        }
        if len(useful_events) >= 2 and tool_names & runtime_tools:
            return "medium-high"
        if tool_names & runtime_tools:
            return "medium"
        return "low"

    def _record_ops_shared_observation(
        self,
        tool_name: str,
        tool_args: dict,
        result: str,
    ) -> None:
        """Persist reusable Ops tool evidence in shared memory."""

        if self.profile.name != "ops":
            return
        if tool_name not in {
            "inspect_klonet_runtime",
            "inspect_system_environment",
            "inspect_screen_session",
            "read_klonet_logs",
            "read_ops_file",
            "search_knowledge",
            "search_code",
            "list_source_files",
        }:
            return
        evidence_line = self._shared_evidence_line(tool_name, result)
        if not evidence_line:
            return
        args_summary = self._safe_tool_args_summary(tool_args)
        self.memory_store.append_shared_episode(
            "\n".join(
                [
                    "## Ops 工具证据",
                    f"- tool: {tool_name}",
                    f"- args: {args_summary}",
                    f"- evidence: {evidence_line}",
                    "- status: tool_observation",
                ]
            )
        )

    def _safe_tool_args_summary(self, tool_args: dict) -> str:
        """Render a short, non-secret tool argument summary."""

        safe = {}
        for key, value in (tool_args or {}).items():
            if any(part in key.lower() for part in ("password", "token", "secret", "key")):
                safe[key] = "[REDACTED]"
            else:
                safe[key] = value
        text = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        return text[:300]

    def _shared_evidence_line(self, tool_name: str, result: str) -> str:
        """Return the first useful evidence line for shared Ops memory."""

        fallback = ""
        for line in (result or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("Error:"):
                continue
            if stripped == tool_name:
                fallback = stripped
                continue
            if len(stripped) > 240:
                stripped = stripped[:237] + "..."
            return stripped
        return fallback

    def _progress_intent_summary(self) -> str:
        """Return a short, non-sensitive summary of the current turn intent."""

        if self._turn_intent is None:
            return "已完成问题理解。"
        if self.profile.name == "ops" and self._ops_route is not None:
            return self._ops_route.summary()
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
            "retrieval_plan": self._retrieval_plan,
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

    def _maintain_project_journal(self, user_input: str, reply: str) -> None:
        """让项目日志维护子 Agent 在 Mentor 回答后沉淀项目事实。"""

        if self.profile.name != "mentor":
            return
        try:
            decision = self.journal_maintainer.maintain_turn(
                user_input,
                reply,
                mode=self.profile.name,
            )
            if decision.should_update:
                print("Klonet Agent：项目日志已自动更新。")
        except Exception:
            return

    def _visible_tools(self) -> list[dict]:
        """根据 profile 和当前问题范围过滤模型可见工具。"""

        tools = [
            tool
            for tool in TOOLS
            if tool["function"]["name"] in self.profile.allowed_tools
        ]
        if self.profile.name != "ops" and self._query_route.scope == "general":
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

    def _ops_route_key(self, route: OpsRoute) -> str:
        """Return a stable English key for Ops tool routing."""

        if route.action in {"restart", "deploy", "destroy", "stop"}:
            return f"controlled_{route.action}"
        if "inspect_process_detail" in route.recommended_tools and route.ports:
            return "port_conflict"
        if "read_klonet_logs" in route.recommended_tools:
            return "log_diagnosis"
        if "inspect_ops_context" in route.recommended_tools:
            return "runtime_inventory"
        return "ops_diagnosis"

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
        if self.profile.name == "ops" and self._ops_route is not None:
            ops_route = self._ops_route
            rules.extend(
                [
                    "【Ops tool routing】",
                    f"- goal: {self._ops_route_key(ops_route)}",
                    f"- mode: {ops_route.mode}",
                    f"- action: {ops_route.action}",
                    f"- risk: {ops_route.risk}",
                    f"- ports: {', '.join(str(port) for port in ops_route.ports) or 'none'}",
                    f"- paths: {', '.join(ops_route.paths[:3]) or 'none'}",
                    f"- components: {', '.join(ops_route.components[:5]) or 'none'}",
                    f"- recommended_tools: {', '.join(ops_route.recommended_tools) or 'none'}",
                    *self._ops_tool_arg_hints(ops_route),
                    "- Ops must treat these as tool-routing hints, not as final diagnosis.",
                ]
            )
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
            budget = self._rag_search_budget(route.scope)
            rules.extend(
                [
                    "- 通用知识与 Klonet 证据需要分区回答。",
                    f"- 本轮 Klonet 知识检索最多 {budget} 次。",
                ]
            )
        else:
            budget = self._rag_search_budget(route.scope)
            rules.extend(
                [
                    "- Klonet RAG 是主要事实依据。",
                    f"- 本轮 Klonet 知识检索最多 {budget} 次。",
                ]
            )
        return {"role": "system", "content": "\n".join(rules)}

    def _ops_tool_arg_hints(self, ops_route: OpsRoute) -> list[str]:
        """Return deterministic argument hints for high-risk Ops diagnostic routes."""

        hints = []
        if "inspect_process_detail" in ops_route.recommended_tools and ops_route.ports:
            port_list = ", ".join(str(port) for port in ops_route.ports)
            hints.append(
                "- tool_args_hint: inspect_process_detail "
                f'{{"ports":[{port_list}]}} '
                "purpose=confirm realtime listener PID/cmd/cwd before relying on screen or broad runtime output"
            )
        return hints

    def _refresh_memory_prompt(self, history: list[dict]):
        """刷新上下文里的记忆系统提示词。"""

        for msg in history:
            if msg.get("role") == "system" and "MEMORY.md" in msg.get("content", ""):
                msg["content"] = self.memory_store.memory_prompt(
                    mode=self.profile.name,
                    include_long_term=self._markdown_memory_is_injected(),
                )
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
