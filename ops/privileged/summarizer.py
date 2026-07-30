"""Small-model summaries for privileged execution evidence."""

from __future__ import annotations

import json
from typing import Any

from klonet_agent.ops.privileged.contracts import PrivilegedPlan, PrivilegedStep
from klonet_agent.tools.environment import redact_sensitive_text


SUMMARY_SYSTEM_PROMPT = """
你是 Klonet Agent 的运维执行结果摘要器。
你只根据提供的确定性状态和已脱敏证据，输出一句普通用户能理解的中文。
说明这一步实际完成了什么，或者失败的直接原因；有组件名、缺失模块、退出码等关键事实时保留。
不得编造原因、解决方案或成功状态。不要输出 JSON、字段名、命令、stdout/stderr、recipe、
Traceback、哈希或内部状态码。最多 100 个汉字，不要加前缀。
""".strip()


class PrivilegedEvidenceSummarizer:
    """Use a small LLM for wording while deterministic code owns state."""

    def __init__(self, llm: Any, *, max_evidence_chars: int = 6000) -> None:
        self.llm = llm
        self.max_evidence_chars = max(500, int(max_evidence_chars))

    def summarize(
        self,
        step: PrivilegedStep,
        *,
        status: str,
        decision_reason: str = "",
    ) -> str:
        evidence = step.evidence
        if evidence is None:
            return _fallback_summary(step, status)
        raw = evidence.stderr or evidence.stdout
        safe_evidence = redact_sensitive_text(raw)[: self.max_evidence_chars]
        binding = step.execution_binding
        action_name = (
            binding.action
            if binding is not None and binding.kind == "registered_action"
            else (
                "一次性 Shell 脚本"
                if binding is not None and binding.kind == "shell_artifact"
                else step.action or "受控操作"
            )
        )
        prompt = (
            "步骤：%s\n"
            "动作：%s\n"
            "确定性状态：%s\n"
            "退出码：%s\n"
            "是否超时：%s\n"
            "是否修改环境：%s\n"
            "校验结论：%s\n"
            "已脱敏执行证据：\n%s"
            % (
                step.title,
                action_name,
                status,
                evidence.return_code,
                evidence.timed_out,
                evidence.environment_changed,
                decision_reason or "无补充",
                safe_evidence or "无输出",
            )
        )
        try:
            response = self.llm.complete(
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                reasoning_effort="low",
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            )
            choices = getattr(response, "choices", None) or []
            message = getattr(choices[0], "message", None) if choices else None
            summary = _one_line(getattr(message, "content", ""), 180)
        except Exception:
            summary = ""
        if not summary or _looks_like_machine_trace(summary):
            return _fallback_summary(step, status)
        if summary.startswith("Klonet Agent："):
            summary = summary[len("Klonet Agent：") :].strip()
        return summary

    def describe_execution(
        self,
        step: PrivilegedStep,
        *,
        index: int,
        total: int,
    ) -> str:
        """Explain an approved step before execution without predicting its result."""

        binding = step.execution_binding
        action_name = step.action or "受控操作"
        args = step.args
        if binding is not None:
            if binding.kind == "registered_action":
                action_name = binding.action
                args = binding.args
            elif binding.kind == "shell_artifact":
                action_name = "一次性 Shell 脚本（需单步确认）"
                args = {
                    "cwd": binding.shell_artifact.cwd,
                    "run_as": binding.shell_artifact.run_as,
                    "declared_changes": binding.shell_artifact.declared_changes,
                }
        safe_args = redact_sensitive_text(
            json.dumps(args, ensure_ascii=False)
        )[:3000]
        prompt = (
            "请用一到两句简洁中文说明即将执行的运维步骤，让普通用户知道操作对象、"
            "具体会检查或改变什么、是否会修改服务器环境。不得声称操作已经成功，"
            "不得输出命令、JSON、内部字段名或敏感值。\n\n"
            "步骤序号：%s/%s\n"
            "步骤标题：%s\n"
            "注册动作：%s\n"
            "风险：%s\n"
            "预期变化：%s\n"
            "已脱敏参数：%s"
            % (
                index,
                total,
                step.title,
                action_name,
                step.risk,
                "；".join(step.expected_changes) or "未声明环境变化",
                safe_args or "{}",
            )
        )
        try:
            response = self.llm.complete(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你负责在运维步骤执行前，用普通用户能理解的中文说明"
                            "即将做什么；只描述，不判断结果。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                reasoning_effort="low",
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            )
            choices = getattr(response, "choices", None) or []
            message = getattr(choices[0], "message", None) if choices else None
            description = _one_line(getattr(message, "content", ""), 240)
        except Exception:
            description = ""
        if not description or _looks_like_machine_trace(description):
            return _fallback_execution_description(step, index, total)
        if description.startswith("Klonet Agent："):
            description = description[len("Klonet Agent：") :].strip()
        return "第 %s/%s 步：%s" % (index, total, description)

    def describe_plan(self, plan: PrivilegedPlan) -> str:
        """Render a detailed user-facing plan, never the persisted audit JSON."""

        safe_plan = {
            "plan_id": plan.plan_id,
            "goal": plan.goal,
            "risk": plan.risk,
            "status": plan.status,
            "verification_level": plan.verification_level,
            "steps": [
                {
                    "index": index,
                    "title": step.title,
                    "objective": step.objective or step.title,
                    "reason": step.reason,
                    "success_criteria": step.success_criteria,
                    "implementation": _safe_implementation(step),
                    "risk": step.risk,
                    "status": step.status,
                    "expected_changes": step.expected_changes,
                    "rollback": step.rollback,
                    "result_summary": _visible_observation(step.observation),
                }
                for index, step in enumerate(plan.steps, start=1)
            ],
        }
        prompt = (
            "请把下面的高权限操作计划写成详细、清晰的中文计划，面向普通用户。\n"
            "必须包含：目标、整体风险、当前状态，以及每一步的操作对象、准备做什么、"
            "可能影响、当前结果和回退方式（有则写）。使用分段和编号。\n"
            "不要输出 JSON、内部字段名、哈希、命令、Schema 或校验器；不要编造输入中"
            "不存在的动作。结尾按当前状态给出以下可用控制方式：%s；"
            "原始审计数据命令为 audit-priv %s。\n\n已脱敏计划：\n%s"
            % (
                _plan_controls(plan),
                plan.plan_id,
                redact_sensitive_text(
                    json.dumps(safe_plan, ensure_ascii=False)
                )[:12000],
            )
        )
        try:
            response = self.llm.complete(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你负责把已确定的运维计划改写成详细自然语言，"
                            "不得改变计划含义或新增操作。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                reasoning_effort="low",
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            )
            choices = getattr(response, "choices", None) or []
            message = getattr(choices[0], "message", None) if choices else None
            description = str(getattr(message, "content", "") or "").strip()
        except Exception:
            description = ""
        if not description or _looks_like_machine_trace(description):
            raise ValueError("detailed plan summary unavailable")
        return description[:8000]


def _fallback_summary(step: PrivilegedStep, status: str) -> str:
    evidence = step.evidence
    if status == "completed":
        return "“%s”已执行完成并通过当前检查。" % step.title
    if evidence is not None and evidence.timed_out:
        return "“%s”执行超时，当前结果无法确定。" % step.title
    if evidence is not None and evidence.return_code is not None:
        return "“%s”执行失败，退出码为 %s；完整原因可在审计证据中查看。" % (
            step.title,
            evidence.return_code,
        )
    return "“%s”未按预期完成，当前结果无法确定。" % step.title


def _fallback_execution_description(
    step: PrivilegedStep,
    index: int,
    total: int,
) -> str:
    impact = (
        "这是只读检查，不会修改服务器环境"
        if step.risk == "readonly"
        else "这一步可能修改服务器环境，执行内容已在当前计划中获得确认"
    )
    return "第 %s/%s 步：正在执行“%s”；%s。" % (
        index,
        total,
        step.title,
        impact,
    )


def _safe_implementation(step: PrivilegedStep) -> dict[str, Any]:
    binding = step.execution_binding
    if binding is None:
        return {
            "kind": "legacy",
            "action": step.action,
            "args": step.args,
        }
    if binding.kind == "registered_action":
        return {
            "kind": "registered_action",
            "action": binding.action,
            "args": binding.args,
            "binding_reason": binding.binding_reason,
        }
    artifact = binding.shell_artifact
    return {
        "kind": binding.kind,
        "cwd": artifact.cwd if artifact else "",
        "run_as": artifact.run_as if artifact else "",
        "declared_changes": artifact.declared_changes if artifact else [],
        "binding_reason": binding.binding_reason,
    }


def _one_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _looks_like_machine_trace(summary: str) -> bool:
    lowered = summary.lower()
    return any(
        marker in lowered
        for marker in (
            "traceback",
            "recipe_id=",
            "stdout=",
            "stderr=",
            "environment_changed=",
            '{"',
        )
    )


def _visible_observation(observation: str) -> str:
    text = str(observation or "").strip()
    marker = "；自动只读诊断已完成，但暂时无法生成可靠修复计划："
    if marker in text:
        text = text.split(marker, 1)[0]
        text += "；此前未形成可执行修复计划，可重新进行证据驱动诊断。"
    return text


def _plan_controls(plan: PrivilegedPlan) -> str:
    if plan.status in {"awaiting_confirmation", "draft"}:
        return "确认执行 confirm-priv %s" % plan.plan_id
    if plan.status == "paused":
        return (
            "重新诊断并规划 replan-priv %s；重试 retry-priv %s；"
            "终止 abort-priv %s"
            % (plan.plan_id, plan.plan_id, plan.plan_id)
        )
    if plan.status == "completed":
        return "计划已经完成，无需再次确认"
    return "查看当前状态后再决定是否操作"
