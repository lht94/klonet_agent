"""Single control-plane entry for every Ops-Privilege user turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from klonet_agent.ops.privileged.goal_guard import GoalSafetyGuard


@dataclass
class SupervisorResult:
    handled: bool
    kind: str
    message: str = ""
    workflow_result: Any | None = None


class PrivilegedOpsSupervisor:
    def __init__(
        self,
        *,
        workflow: Any,
        classifier: Any,
        goal_guard: GoalSafetyGuard | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.workflow = workflow
        self.classifier = classifier
        self.goal_guard = goal_guard or GoalSafetyGuard()
        self.on_progress = on_progress

    def handle(self, text: str, *, environment_context: str = "") -> SupervisorResult:
        return self.handle_with_context(
            text,
            environment_context=environment_context,
            conversation_context="",
        )

    def handle_with_context(
        self,
        text: str,
        *,
        environment_context: str = "",
        conversation_context: str = "",
    ) -> SupervisorResult:
        normalized = " ".join((text or "").split())
        if self.workflow.is_control_command(normalized):
            result = self.workflow.handle_command(normalized)
            return self._handled(result)

        safety = self.goal_guard.check(normalized)
        if safety.denied:
            return SupervisorResult(True, "denied", "Denied: %s" % safety.reason)

        self._progress("正在分析请求并规划下一步…")
        decision = self.classifier.classify(
            normalized,
            conversation_context=conversation_context,
        )
        if decision.intent == "classifier_error":
            return SupervisorResult(
                True,
                "blocked",
                "当前无法可靠判断这条请求属于问答、只读检查还是变更操作。"
                "这是分类服务异常，不是你的表达问题；当前没有执行任何操作，请稍后重试。",
            )
        if decision.should_clarify:
            question = decision.clarification_question
            if not any("\u4e00" <= char <= "\u9fff" for char in question):
                question = (
                    "我还无法确定你想达到的结果或要操作的对象，请补充具体目标；"
                    "当前没有执行任何操作。"
                )
            return SupervisorResult(
                True,
                "clarification",
                question,
            )
        if decision.intent == "conversation":
            return SupervisorResult(False, "conversation")
        if decision.intent == "readonly_action":
            if not decision.command:
                self._progress("正在检索 Klonet 知识并读取服务器环境，然后生成只读计划…")
                return self._handled(
                    self.workflow.submit(
                        normalized,
                        environment_context=self._planning_context(
                            environment_context,
                            decision.goal_clarity,
                        ),
                    )
                )
            return self._handled(
                self.workflow.submit_readonly(normalized, decision.command)
            )
        self._progress("正在检索 Klonet 知识并读取服务器环境，然后生成操作计划…")
        return self._handled(
            self.workflow.submit(
                normalized,
                environment_context=self._planning_context(
                    environment_context,
                    decision.goal_clarity,
                ),
            )
        )

    @staticmethod
    def _planning_context(environment_context: str, goal_clarity: str) -> str:
        if goal_clarity != "discoverable":
            return environment_context
        guidance = (
            "Some implementation details are not yet known but can be discovered. "
            "Begin with the smallest safe read-only inspection steps. Do not invent "
            "paths, services, hosts, or current state. Any later state change remains "
            "subject to normal authorization."
        )
        if environment_context:
            return guidance + "\n\n" + environment_context
        return guidance

    def _progress(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    @staticmethod
    def _handled(result: Any) -> SupervisorResult:
        return SupervisorResult(
            handled=True,
            kind=result.kind,
            message=result.message,
            workflow_result=result,
        )
