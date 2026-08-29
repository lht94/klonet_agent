"""User-facing response rendering for Ops-Privilege."""

from __future__ import annotations

import re
from typing import Any

from klonet_agent.ops.privileged.workflow.contracts import (
    EvidenceBundle, EvidenceConclusion, FailureRecord,
)
from klonet_agent.ops.privileged.workflow.runtime_inventory import (
    RuntimeInventory, render_runtime_goal, runtime_inventory_answers_goal,
)
from klonet_agent.tools.environment import redact_sensitive_text
from klonet_agent.ops.privileged.context import klonet_domain_context


class ResponseAgent:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def render_readonly(
        self, goal: str, conclusion: EvidenceConclusion,
        evidence_bundle: EvidenceBundle | None = None,
        goal_kind: str = "health_check",
    ) -> str:
        inventory = RuntimeInventory.from_bundle(evidence_bundle)
        runtime_inventory = (
            render_runtime_goal(goal, inventory)
            if goal_kind == "health_check"
            and runtime_inventory_answers_goal(goal, inventory)
            else ""
        )
        if runtime_inventory:
            return runtime_inventory
        if self.llm is not None:
            try:
                response = self.llm.complete(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是 Klonet Ops-Privilege 的终端展示器，不是 Planner、"
                                "Discovery 或 Verifier。只回答用户原始目标，只能整理输入中"
                                "已经确认的结论。不得新增事实、证据缺口、后续检查、建议命令"
                                "或让用户执行任何操作；不得输出内部证据 ID、工作流阶段、"
                                "补证过程或机器字段转储。先直接给结论，再用自然语言分组；"
                                "列表项分别换行，不输出多余空行。\n\n"
                                + klonet_domain_context("response")
                            ),
                        },
                        {
                            "role": "user",
                            "content": self._prompt(
                                goal, conclusion, evidence_bundle=evidence_bundle,
                            ),
                        },
                    ],
                    tools=None,
                    reasoning_effort="low",
                    temperature=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                text = (response.choices[0].message.content or "").strip()
                if text and self._is_terminal_answer(text):
                    return text
            except Exception:
                pass
        return self._fallback(conclusion)

    def render_plan_turn(
        self,
        question: str,
        *,
        conversation_context: str,
        plan_context: str,
        fallback: str,
    ) -> str:
        """Answer a plan-domain question from history and persisted truth."""

        if self.llm is not None:
            try:
                response = self.llm.complete(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是 Klonet 计划管理界面的回答器。结合最近对话与"
                                "持久化计划事实回答当前问题。对话可以证明用户讨论过"
                                "修订，但只有新的持久化 Plan ID 才证明正式计划已被"
                                "替换。直接回答用户真正的问题，不要因为出现‘计划’就"
                                "机械复述计划。不得自行修改、批准、执行或虚构计划；"
                                "需要落实修订时，只说明应进入现有 Replan 流程。\n\n"
                                + klonet_domain_context("response")
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "当前问题：\n%s\n\n最近对话：\n%s\n\n"
                                "持久化计划事实（权威）：\n%s"
                            ) % (
                                question,
                                conversation_context or "无",
                                plan_context,
                            ),
                        },
                    ],
                    tools=None,
                    reasoning_effort="low",
                    temperature=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                text = (response.choices[0].message.content or "").strip()
                if text:
                    return text
            except Exception:
                pass
        return fallback

    def render_failure(
        self,
        failure: FailureRecord,
        *,
        fallback: str,
    ) -> str:
        """Explain persisted failure facts without controlling recovery state."""

        if self.llm is None:
            return fallback
        try:
            response = self.llm.complete(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 Klonet Ops-Privilege 的失败说明展示器。你只能把"
                            "输入中的已持久化失败事实改写成两到四句清晰中文，不是"
                            " Planner、Replanner、Coordinator 或决策器。直接说明"
                            "为什么失败、系统已经尝试了什么，以及这属于系统缺口"
                            "还是确实存在用户未决事项。不得新增事实、根因、端口、"
                            "组件、权限或用户条件；不得生成恢复选项、序号列表、命令、"
                            "计划或建议操作。missing_decisions 为空时必须明确说明"
                            "当前记录没有要求用户补充条件，不得要求用户猜测边界。\n\n"
                            + klonet_domain_context("response")
                        ),
                    },
                    {
                        "role": "user",
                        "content": self._failure_prompt(failure),
                    },
                ],
                tools=None,
                reasoning_effort="low",
                temperature=0,
                max_tokens=320,
                extra_body={"thinking": {"type": "disabled"}},
            )
            text = (response.choices[0].message.content or "").strip()
            if self._is_failure_explanation(text, failure):
                return text
        except Exception:
            pass
        return fallback

    @staticmethod
    def _failure_prompt(failure: FailureRecord) -> str:
        evidence_facts = [
            "%s[%s]" % (fact.predicate, fact.fact_id)
            for request in failure.evidence_requests
            for fact in request.required_facts
        ]
        fields = (
            "失败阶段：%s\n失败摘要：%s\n技术原因：%s\n"
            "失败步骤：%s\n失败校验：%s\n已尝试恢复：%s\n"
            "环境变化：%s\n系统仍需补齐的证据：%s\n用户未决事项：%s"
        ) % (
            failure.stage,
            failure.summary,
            failure.technical_reason,
            failure.failed_step or "无",
            "；".join(failure.failed_checks) or "无",
            "；".join(failure.attempted_recoveries) or "无",
            failure.environment_changed,
            "；".join(evidence_facts) or "无",
            "；".join(failure.missing_decisions) or "无",
        )
        return redact_sensitive_text(fields)

    @staticmethod
    def _is_failure_explanation(text: str, failure: FailureRecord) -> bool:
        """Reject model output that tries to become a recovery controller."""

        value = str(text or "").strip()
        if (
            not value
            or len(value) < 12
            or len(value) > 1200
            or value.lower() in {"false", "true", "null", "none"}
        ):
            return False
        forbidden = (
            r"(?m)^\s*\d+[.)、]\s*",
            r"choose-priv-option|confirm-priv-plan|show-priv-",
            r"```|(?m:^\s*(?:sudo|ps|ss|docker|screen|systemctl)\s+)",
            r"(?:请选择|回复|输入).{0,12}(?:选项|序号|1|2|3)",
        )
        if any(re.search(pattern, value, re.I) for pattern in forbidden):
            return False
        if not failure.missing_decisions:
            asks_for_boundary = (
                re.search(r"请.{0,12}(?:补充|提供|确认|决定)", value)
                or re.search(r"需要你.{0,12}(?:补充|提供|确认|决定)", value)
            )
            if asks_for_boundary and not re.search(r"不需要|无需|没有要求", value):
                return False
        return True

    @staticmethod
    def _is_terminal_answer(text: str) -> bool:
        """Reject presentation output that tries to reopen the workflow."""

        value = str(text or "")
        forbidden = (
            r"```(?:bash|sh|shell)?",
            r"(?:建议|需要|请).{0,16}(?:执行|运行|补充|查询|检查).{0,8}(?:命令|指令|操作)?",
            r"(?:证据缺口|补证循环|Verifier|Discovery|collection_goal)",
            r"(?m)^\s*(?:sudo|ps|ss|df|free|docker|ovs-vsctl)\s+",
        )
        return not any(re.search(pattern, value, re.I) for pattern in forbidden)

    @staticmethod
    def _prompt(
        goal: str, conclusion: EvidenceConclusion,
        *, evidence_bundle: EvidenceBundle | None = None,
    ) -> str:
        facts = "\n".join("- %s" % item.text for item in conclusion.confirmed_facts)
        uncertain = "\n".join("- %s" % item.text for item in conclusion.uncertainties)
        missing = "\n".join("- %s" % item for item in conclusion.missing_decisions)
        knowledge = "\n".join(
            str(item.output or "")[:6000]
            for item in getattr(evidence_bundle, "knowledge_records", [])
            if item.status == "available"
        )
        return (
            "目标：%s\nKlonet 知识证据：\n%s\n已确认：\n%s\n"
            "不确定：\n%s\n缺失决策：\n%s"
        ) % (
            goal,
            knowledge or "无",
            facts or "无",
            uncertain or "无",
            missing or "无",
        )

    @staticmethod
    def _fallback(conclusion: EvidenceConclusion) -> str:
        lines = []
        if conclusion.confirmed_facts:
            lines.append("已确认：" + "；".join(item.text for item in conclusion.confirmed_facts))
        if conclusion.uncertainties:
            lines.append("仍有不确定项：" + "；".join(item.text for item in conclusion.uncertainties))
        if conclusion.missing_decisions:
            lines.append("需要补充决定：" + "；".join(conclusion.missing_decisions))
        return "\n".join(lines) or "当前没有取得可用于回答的可靠只读证据。"
