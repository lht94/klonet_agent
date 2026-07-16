"""受约束的项目日志维护器。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from klonet_agent.journal.project_journal import ProjectJournal
from klonet_agent.llm.embeddings import build_default_embedding_provider

EmbeddingProvider = Callable[[str], Sequence[float]]

@dataclass
class JournalUpdateDecision:
    """项目日志维护决策。"""

    should_update: bool = False
    confidence: str = "low"
    goal: str = ""
    current_status: str = ""
    events: list[str] = field(default_factory=list)
    next_step: str = ""
    evidence: list[str] = field(default_factory=list)


class ProjectJournalMaintainer:
    """LLM 驱动的项目日志维护子 Agent。

    它只负责输出结构化维护决策，不直接回答用户，也不直接自由写文件。
    主程序负责把决策应用到 ProjectJournal 的安全更新 API。
    """

    def __init__(
        self,
        journal: ProjectJournal,
        llm: Any | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self.journal = journal
        self.llm = llm
        self.embedding_provider = embedding_provider
        self._default_embedding_provider_loaded = embedding_provider is not None

    def maintain_turn(
        self,
        user_input: str,
        assistant_reply: str,
        mode: str = "mentor",
    ) -> JournalUpdateDecision:
        """判断并应用本轮项目日志更新。"""

        decision = self.decide(user_input, assistant_reply, mode=mode)
        if decision.should_update:
            self.apply(decision)
        return decision

    def decide(
        self,
        user_input: str,
        assistant_reply: str,
        mode: str = "mentor",
    ) -> JournalUpdateDecision:
        """生成结构化日志更新决策。"""

        if not _should_consider_update(user_input, assistant_reply):
            return JournalUpdateDecision()
        if self.llm is not None:
            llm_decision = self._decide_with_llm(user_input, assistant_reply, mode)
            if llm_decision is not None:
                return llm_decision
        return JournalUpdateDecision()

    def _decide_with_llm(
        self,
        user_input: str,
        assistant_reply: str,
        mode: str,
    ) -> JournalUpdateDecision | None:
        """调用真实 LLM 子 Agent 生成项目日志维护决策。"""

        prompt = _build_maintainer_prompt(
            journal_summary=self.journal.summary(max_chars=1600),
            user_input=user_input,
            assistant_reply=assistant_reply,
            mode=mode,
        )
        try:
            response = self.llm.complete(
                [{"role": "user", "content": prompt}],
                tools=None,
            )
            content = response.choices[0].message.content or ""
            data = _parse_json_object(content)
            return _decision_from_dict(data)
        except Exception:
            return None

    def apply(self, decision: JournalUpdateDecision) -> None:
        """把结构化决策写入项目日志。"""

        self.journal.ensure()
        if decision.goal:
            self.journal.merge_goal(
                decision.goal,
                embedding_provider=self._embedding_provider(),
            )
        if decision.current_status:
            self.journal.update_status(decision.current_status)
        for event in decision.events:
            self.journal.append_event_once("执行记录", event)
        if decision.next_step:
            self.journal.update_next_step_if_placeholder(decision.next_step)

    def _embedding_provider(self) -> EmbeddingProvider | None:
        """Return the configured embedding provider, loading defaults lazily."""

        if not self._default_embedding_provider_loaded:
            self.embedding_provider = build_default_embedding_provider()
            self._default_embedding_provider_loaded = True
        return self.embedding_provider


def _normalize(value: str) -> str:
    return " ".join((value or "").lower().split())


def _should_consider_update(user_input: str, assistant_reply: str) -> bool:
    user_text = _normalize(user_input)
    reply_text = _normalize(assistant_reply)
    if not user_text or user_text in {"你好", "hello", "hi", "在吗"}:
        return False
    if len(user_text) < 8 and len(reply_text) < 80:
        return False
    return True


def _build_maintainer_prompt(
    *,
    journal_summary: str,
    user_input: str,
    assistant_reply: str,
    mode: str,
) -> str:
    return f"""
你是 Klonet Agent 的 Project Journal Agent，只维护项目日志，不回答用户问题。

请根据“当前项目日志摘要”“当前 Agent 模式”“本轮用户输入”“Agent 最终回答”判断是否需要更新项目日志。

你可以维护两类内容：
1. 项目事实：项目目标、研究方向、当前阶段、下一步。
2. 过程记录：用户与 Agent 讨论过的实质性 Klonet 主题、关键结论、开发进展、运维发现和待确认事项。

不同模式的记录重点：
- mentor：记录学习主题、项目目标、研究方向、关键解释、用户纠正和下一步学习计划。
- coding：记录需求确认、设计决策、代码改动摘要、测试结果、遗留问题和下一步开发计划。
- ops：记录运维目标、实际工具证据、环境发现、执行过的安全操作、风险、阻塞和待复核事项。

只允许把以下来源写入日志：
- 用户明确表达的信息。
- Agent 回答中基于用户明确问题、工具结果或当前日志总结出的项目阶段。
- 已在当前日志中存在的信息。

不要把不确定推测写成事实；不确定时 should_update=false。
ops 模式下，只有工具结果或用户明确确认支持的运行态信息才能写成事实；历史记忆和模型推测只能写成待确认事项。
普通寒暄、能力介绍、没有实质 Klonet 内容的问答不要更新日志。
不要输出 Markdown，不要解释，只输出一个 JSON object。

JSON schema:
{{
  "should_update": true,
  "confidence": "low|medium|high",
  "goal": "项目目标；没有就空字符串",
  "current_status": "当前状态；没有就空字符串",
  "events": ["需要去重追加的执行记录"],
  "next_step": "下一步；没有就空字符串",
  "evidence": ["依据，优先引用用户原话的简短片段"]
}}

当前项目日志摘要：
{journal_summary}

当前 Agent 模式：
{mode}

本轮用户输入：
{user_input}

Agent 最终回答：
{assistant_reply}
""".strip()


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("journal maintainer did not return JSON")
        text = match.group(0)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("journal maintainer JSON must be an object")
    return data


def _decision_from_dict(data: dict[str, Any]) -> JournalUpdateDecision:
    return JournalUpdateDecision(
        should_update=bool(data.get("should_update")),
        confidence=str(data.get("confidence") or "low"),
        goal=str(data.get("goal") or "").strip(),
        current_status=str(data.get("current_status") or "").strip(),
        events=[
            str(item).strip()
            for item in data.get("events", [])
            if str(item).strip()
        ][:5],
        next_step=str(data.get("next_step") or "").strip(),
        evidence=[
            str(item).strip()
            for item in data.get("evidence", [])
            if str(item).strip()
        ][:5],
    )
