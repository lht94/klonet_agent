"""Unified turn-level intent and decision planning.

This module is the control-plane adapter between the older intent pieces
(`QueryIntent`, `SemanticFrame`, `ConversationState`) and the newer desired
single turn object used by clarification, retrieval, source lookup, and answer
policy decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from klonet_agent.knowledge.clarification import ClarificationDecision
from klonet_agent.knowledge.conversation_state import ConversationState
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.semantic_understanding import IntentDecision, SemanticFrame


CONTEXT_NONE = "none"
CONTEXT_CONTINUE = "continue"
CONTEXT_OPTION_SELECT = "option_select"
CONTEXT_ACCEPT_ANY = "accept_any"
CONTEXT_LATE_ENTITY_FILL = "late_entity_fill"
CONTEXT_CORRECTION = "correction"

CLARIFICATION_NONE = "none"
CLARIFICATION_LOW_INFORMATION = "low_information"
CLARIFICATION_MODEL_REQUESTED = "model_requested"
CLARIFICATION_LOW_CONFIDENCE = "low_confidence"
CLARIFICATION_SOFT_NOTE = "soft_note"

SOURCE_NONE = "none"
SOURCE_INDEX = "source_index"


@dataclass(frozen=True)
class TurnIntent:
    """Single trusted intent object for one user turn."""

    scope: str = "klonet"
    task_type: str = "concept"
    operation: str = "unknown"
    target: str = ""
    symptom: str = ""
    user_role: str = "unknown"
    machine_role: str = "unspecified"
    phase: str = "unknown"
    context_ref: str = CONTEXT_NONE
    excluded_meanings: tuple[str, ...] = ()
    confidence: float = 0.0
    clarification_type: str = CLARIFICATION_NONE
    clarification_question: str = ""
    source_need: str = SOURCE_NONE
    requires_retrieval: bool = True
    requires_environment_diagnosis: bool = False
    original_user_input: str = ""
    effective_user_input: str = ""
    prerequisites: tuple[str, ...] = ()
    is_correction: bool = False
    answer_mode: str = "general"
    soft_note: str = ""
    defaulted_assumptions: tuple[str, ...] = ()

    def to_query_intent(self) -> QueryIntent:
        """Return the legacy intent shape used by existing retrieval code."""

        excluded = tuple(
            item
            for item in self.excluded_meanings
            if item
            in {
                "environment_setup",
                "dependency_install",
                "platform_start",
                "platform_stop",
                "platform_restart",
                "topology_deploy",
            }
        )
        return QueryIntent.from_mapping(
            {
                "scope": self.scope,
                "task_type": self.task_type,
                "operation": self.operation,
                "target": self.target,
                "symptom": self.symptom,
                "excluded_intents": excluded,
                "prerequisites": list(self.prerequisites),
                "requires_retrieval": self.requires_retrieval,
                "requires_environment_diagnosis": self.requires_environment_diagnosis,
                "clarification_required": self.clarification_type
                in {CLARIFICATION_MODEL_REQUESTED, CLARIFICATION_LOW_CONFIDENCE},
                "clarification_question": self.clarification_question,
                "is_correction": self.is_correction,
                "confidence": self.confidence,
            }
        )


@dataclass(frozen=True)
class TurnDecision:
    """Actions that the rest of the orchestrator should take for this turn."""

    intent: TurnIntent
    should_resume: bool = False
    should_clarify: bool = False
    clarification_reason: str = ""
    clarification_reply: str = ""
    source_required: bool = False
    answer_task_type: str = "concept"
    answer_mode: str = "general"

    def to_clarification_decision(self) -> ClarificationDecision:
        return ClarificationDecision(
            should_stop=self.should_clarify,
            reason=self.clarification_reason,
            reply=self.clarification_reply,
        )


class TurnIntentBuilder:
    """Merge model intent, semantic frame, and conversation state."""

    def build(
        self,
        user_input: str,
        *,
        recent_history: list[dict] | None = None,
        intent: QueryIntent | None = None,
        semantic_frame: SemanticFrame | None = None,
        decision: IntentDecision | None = None,
        conversation_state: ConversationState | None = None,
        resume_state: Mapping[str, Any] | None = None,
        effective_user_input: str | None = None,
    ) -> TurnIntent:
        if resume_state is not None:
            restored = _restore_turn_intent(resume_state)
            if restored is not None:
                return _inherit_for_continue(
                    restored,
                    user_input=user_input,
                    effective_user_input=effective_user_input,
                )

        if decision is not None:
            intent = decision.intent
        intent = intent or QueryIntent()
        history = recent_history or []
        state = conversation_state or ConversationState()
        frame = semantic_frame or SemanticFrame()
        context_ref = _context_ref_for(user_input, history, state, intent)
        phase = _phase_for(intent, frame, state)
        task_type = _task_type_for(intent, phase, context_ref)
        operation = _operation_for(intent, phase, context_ref)
        target = _target_for(intent, frame, state)
        clarification_type = _clarification_type_for(
            user_input=user_input,
            history=history,
            intent=intent,
            decision=decision,
            context_ref=context_ref,
        )
        excluded = _unique(
            list(intent.excluded_intents)
            + list(frame.excluded_meanings)
            + list(state.excluded_meanings)
            + _phase_exclusions(phase)
        )
        answer_mode = (
            decision.answer_mode
            if decision is not None and decision.answer_mode
            else _answer_mode_for(phase, task_type)
        )
        return TurnIntent(
            scope=_first_known(intent.scope, frame.scope, "klonet"),
            task_type=task_type,
            operation=operation,
            target=target,
            symptom=intent.symptom or frame.symptom,
            user_role=_first_known(state.user_role, frame.user_role, "unknown"),
            machine_role=_first_known(
                state.machine_role,
                frame.machine_role,
                "unspecified",
            ),
            phase=phase,
            context_ref=context_ref,
            excluded_meanings=excluded,
            confidence=max(intent.confidence, frame.confidence),
            clarification_type=clarification_type,
            clarification_question=intent.clarification_question,
            source_need=_source_need_for(user_input),
            requires_retrieval=intent.requires_retrieval,
            requires_environment_diagnosis=intent.requires_environment_diagnosis,
            original_user_input=user_input,
            effective_user_input=effective_user_input or user_input,
            prerequisites=intent.prerequisites,
            is_correction=intent.is_correction,
            answer_mode=answer_mode,
            soft_note=decision.soft_note if decision is not None else "",
            defaulted_assumptions=(
                decision.defaulted_assumptions if decision is not None else ()
            ),
        )


class TurnDecisionPlanner:
    """Plan turn actions from the unified intent object."""

    def plan(self, intent: TurnIntent) -> TurnDecision:
        if intent.clarification_type == CLARIFICATION_LOW_INFORMATION:
            return TurnDecision(
                intent=intent,
                should_clarify=True,
                clarification_reason="low_information_input",
                clarification_reply=(
                    "我还没理解你的意思。你可以补充一下想问 Klonet 的哪类问题吗？"
                    "比如环境安装、平台启动、拓扑操作、报错排查或源码实现。"
                ),
                answer_task_type=intent.task_type,
                answer_mode=intent.answer_mode,
                source_required=intent.source_need != SOURCE_NONE,
            )
        if intent.clarification_type == CLARIFICATION_MODEL_REQUESTED:
            return TurnDecision(
                intent=intent,
                should_clarify=True,
                clarification_reason="model_requested_clarification",
                clarification_reply=intent.clarification_question
                or "这个问题的意图还不够明确，能否先补充你的具体目标？",
                answer_task_type=intent.task_type,
                answer_mode=intent.answer_mode,
                source_required=intent.source_need != SOURCE_NONE,
            )
        if intent.clarification_type == CLARIFICATION_LOW_CONFIDENCE:
            return TurnDecision(
                intent=intent,
                should_clarify=True,
                clarification_reason="low_intent_confidence",
                clarification_reply="我还不确定你要的结果类型，能否先补充一下具体目标？",
                answer_task_type=intent.task_type,
                answer_mode=intent.answer_mode,
                source_required=intent.source_need != SOURCE_NONE,
            )
        return TurnDecision(
            intent=intent,
            should_resume=intent.context_ref == CONTEXT_CONTINUE,
            source_required=intent.source_need != SOURCE_NONE,
            answer_task_type=intent.task_type,
            answer_mode=intent.answer_mode,
        )


def _restore_turn_intent(state: Mapping[str, Any]) -> TurnIntent | None:
    value = state.get("turn_intent")
    if isinstance(value, TurnIntent):
        return value
    legacy = state.get("intent")
    if isinstance(legacy, QueryIntent):
        return TurnIntentBuilder().build(
            str(state.get("original_user_input") or ""),
            intent=legacy,
            conversation_state=state.get("conversation_state")
            if isinstance(state.get("conversation_state"), ConversationState)
            else None,
        )
    return None


def _inherit_for_continue(
    previous: TurnIntent,
    *,
    user_input: str,
    effective_user_input: str | None,
) -> TurnIntent:
    return TurnIntent(
        scope=previous.scope,
        task_type=previous.task_type,
        operation=previous.operation,
        target=previous.target,
        symptom=previous.symptom,
        user_role=previous.user_role,
        machine_role=previous.machine_role,
        phase=previous.phase,
        context_ref=CONTEXT_CONTINUE,
        excluded_meanings=previous.excluded_meanings,
        confidence=previous.confidence,
        clarification_type=CLARIFICATION_NONE,
        clarification_question="",
        source_need=_source_need_for(user_input) or previous.source_need,
        requires_retrieval=previous.requires_retrieval,
        requires_environment_diagnosis=previous.requires_environment_diagnosis,
        original_user_input=user_input,
        effective_user_input=effective_user_input or previous.effective_user_input,
        prerequisites=previous.prerequisites,
        is_correction=previous.is_correction,
        answer_mode=previous.answer_mode,
        soft_note=previous.soft_note,
        defaulted_assumptions=previous.defaulted_assumptions,
    )


def _context_ref_for(
    user_input: str,
    history: list[dict],
    state: ConversationState,
    intent: QueryIntent,
) -> str:
    text = (user_input or "").strip().lower()
    compact = "".join(text.split())
    if intent.is_correction:
        return CONTEXT_CORRECTION
    if compact in {"继续", "接着说", "继续说", "继续上面", "goon", "continue"}:
        return CONTEXT_CONTINUE
    if _is_accept_any_reply(compact, history):
        return CONTEXT_ACCEPT_ANY
    if _is_late_entity_fill(compact, history, state):
        return CONTEXT_LATE_ENTITY_FILL
    if _is_option_selection_reply(text, compact, history):
        return CONTEXT_OPTION_SELECT
    return CONTEXT_NONE


def _is_late_entity_fill(
    compact_text: str,
    history: list[dict],
    state: ConversationState,
) -> bool:
    if compact_text not in {"klonet", "klonet平台", "平台", "这个平台", "那个平台"}:
        return False
    if state.deployment_phase == "platform_usage":
        return True
    recent_text = "\n".join(
        str(message.get("content") or "")
        for message in history[-6:]
        if message.get("role") in {"user", "assistant"}
    )
    return any(
        term in recent_text
        for term in ("使用平台", "浏览器", "普通用户", "场景一", "电脑里需要下载")
    )


def _is_accept_any_reply(compact_text: str, history: list[dict]) -> bool:
    accept_terms = {
        "都可以",
        "都行",
        "都可以的",
        "都行吧",
        "都可",
        "都好",
        "哪个都行",
        "哪种都行",
        "我说了都行",
        "随便",
        "都无所谓",
    }
    if compact_text not in accept_terms:
        return False
    return _recent_history_has_options(history)


def _recent_history_has_options(history: list[dict]) -> bool:
    recent_assistant_text = "\n".join(
        str(message.get("content") or "")
        for message in history[-6:]
        if message.get("role") == "assistant"
    )
    if not recent_assistant_text:
        return False
    option_markers = (
        "A：",
        "B：",
        "C：",
        "D：",
        "E：",
        "F：",
        "A:",
        "B:",
        "C:",
        "D:",
        "E:",
        "F:",
        "1.",
        "2.",
        "3.",
        "4.",
        "5.",
        "6.",
        "1：",
        "2：",
        "3：",
        "4：",
        "5：",
        "6：",
        "第一种",
        "第二种",
        "第三种",
        "第四种",
        "第五种",
        "第六种",
        "场景一",
        "场景二",
        "场景三",
        "场景四",
        "场景五",
        "场景六",
        "方案一",
        "方案二",
        "方案三",
        "方案四",
        "方案五",
        "方案六",
        "路径一",
        "路径二",
        "路径三",
        "路径四",
        "路径五",
        "路径六",
        "路线一",
        "路线二",
        "路线三",
        "路线四",
        "路线五",
        "路线六",
    )
    return sum(1 for marker in option_markers if marker in recent_assistant_text) >= 2


def _is_option_selection_reply(text: str, compact_text: str, history: list[dict]) -> bool:
    if not _recent_history_has_options(history):
        return False
    if compact_text in {
        "a",
        "b",
        "c",
        "d",
        "e",
        "f",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "选a",
        "选b",
        "选c",
        "选d",
        "选e",
        "选f",
        "选1",
        "选2",
        "选3",
        "选4",
        "选5",
        "选6",
        "a方案",
        "b方案",
        "c方案",
        "d方案",
        "e方案",
        "f方案",
    }:
        return True
    option_terms = (
        "第一种",
        "第二种",
        "第三种",
        "第四种",
        "第五种",
        "第六种",
        "第一个",
        "第二个",
        "第三个",
        "第四个",
        "第五个",
        "第六个",
        "第1个",
        "第2个",
        "第3个",
        "第4个",
        "第5个",
        "第6个",
        "场景一",
        "场景二",
        "场景三",
        "场景四",
        "场景五",
        "场景六",
        "方案一",
        "方案二",
        "方案三",
        "方案四",
        "方案五",
        "方案六",
        "路径一",
        "路径二",
        "路径三",
        "路径四",
        "路径五",
        "路径六",
        "路线一",
        "路线二",
        "路线三",
        "路线四",
        "路线五",
        "路线六",
    )
    return any(term in text for term in option_terms)


def _phase_for(
    intent: QueryIntent,
    frame: SemanticFrame,
    state: ConversationState,
) -> str:
    return (
        _known(state.deployment_phase)
        or _phase_from_operation(intent.operation)
        or _known(frame.deployment_phase)
        or "unknown"
    )


def _task_type_for(intent: QueryIntent, phase: str, context_ref: str) -> str:
    if context_ref == CONTEXT_ACCEPT_ANY and _looks_like_deploy_choice_intent(intent):
        return "deployment_guidance"
    if context_ref == CONTEXT_LATE_ENTITY_FILL or phase == "platform_usage":
        return "operation_guide"
    if phase == "local_tool_preparation":
        return "deployment_preparation"
    return intent.task_type


def _looks_like_deploy_choice_intent(intent: QueryIntent) -> bool:
    text = f"{intent.target} {intent.clarification_question}".lower()
    return (
        "klonet" in text
        and any(term in text for term in ("安装", "环境", "首次"))
        and any(term in text for term in ("启动", "平台服务", "已经安装"))
    )


def _operation_for(intent: QueryIntent, phase: str, context_ref: str) -> str:
    if context_ref == CONTEXT_LATE_ENTITY_FILL or phase == "platform_usage":
        return "unknown"
    if phase == "platform_startup" and intent.operation == "unknown":
        return "platform_start"
    return intent.operation


def _target_for(
    intent: QueryIntent,
    frame: SemanticFrame,
    state: ConversationState,
) -> str:
    if intent.target:
        return intent.target
    if frame.target_component:
        return frame.target_component
    if state.current_topic:
        return state.current_topic
    return ""


def _clarification_type_for(
    *,
    user_input: str,
    history: list[dict],
    intent: QueryIntent,
    decision: IntentDecision | None,
    context_ref: str,
) -> str:
    if context_ref in {
        CONTEXT_CONTINUE,
        CONTEXT_OPTION_SELECT,
        CONTEXT_ACCEPT_ANY,
        CONTEXT_LATE_ENTITY_FILL,
    }:
        return CLARIFICATION_NONE
    if _looks_like_low_information(user_input, history):
        return CLARIFICATION_LOW_INFORMATION
    if decision is not None and decision.clarification_action == "soft_note":
        return CLARIFICATION_SOFT_NOTE
    if intent.clarification_required:
        return CLARIFICATION_MODEL_REQUESTED
    if intent.confidence and intent.confidence < 0.5:
        return CLARIFICATION_LOW_CONFIDENCE
    return CLARIFICATION_NONE


def _looks_like_low_information(user_input: str, history: list[dict]) -> bool:
    text = (user_input or "").strip()
    compact = "".join(text.lower().split())
    if not compact:
        return True
    if compact in {"a", "b", "1", "2"} and history:
        return False
    if compact in {"ip", "vm", "kvm", "ovs", "ssh", "api", "gpu", "cpu", "ram"}:
        return False
    if any("\u4e00" <= char <= "\u9fff" for char in compact):
        return False
    if any(char in compact for char in ("/", "_", "-", ".")):
        return False
    return len(compact) <= 3


def _source_need_for(user_input: str) -> str:
    text = (user_input or "").lower()
    if any(
        term in text
        for term in (
            "源码",
            "代码",
            "实现",
            "函数",
            "类",
            "哪个文件",
            "哪里写",
            ".py",
            ".js",
            "topomanager",
        )
    ):
        return SOURCE_INDEX
    return SOURCE_NONE


def _phase_from_operation(operation: str) -> str:
    return {
        "environment_setup": "environment_setup",
        "dependency_install": "environment_setup",
        "platform_start": "platform_startup",
        "platform_stop": "platform_shutdown",
        "platform_restart": "platform_restart",
        "topology_deploy": "topology_deploy",
    }.get(operation, "")


def _phase_exclusions(phase: str) -> list[str]:
    if phase == "platform_usage":
        return ["deployment_preparation", "environment_setup", "platform_startup"]
    if phase == "local_tool_preparation":
        return ["environment_setup", "dependency_install"]
    return []


def _answer_mode_for(phase: str, task_type: str) -> str:
    if phase != "unknown":
        return phase
    return task_type or "general"


def _first_known(*values: str) -> str:
    for value in values:
        known = _known(value)
        if known:
            return known
    return values[-1] if values else ""


def _known(value: str) -> str:
    return "" if value in {"", "unknown", "unspecified"} else value


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))
