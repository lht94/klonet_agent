"""Outcome-driven replanning for read-only operational goals."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.v4.contracts import (
    EvidenceBundle,
    EvidenceConclusion,
    ProbeRequest,
    normalize_probe_request,
)
from klonet_agent.ops.privileged.v4.discovery import parse_json_object


DIAGNOSTIC_OUTCOME_PROMPT = """
You are the outcome evaluator and replanner for a read-only operational goal.
Determine whether the user's requested result has actually been obtained.

Return exactly one JSON object:
{
  "status": "achieved|continue|needs_user_decision|blocked",
  "reason": "short reason",
  "user_question": "question only for a genuine user choice",
  "probe_requests": [{"probe":"registered probe","args":{},"purpose":"..."}]
}

Rules:
- `achieved` requires evidence that directly answers the goal. For diagnosis,
  require a supported causal chain from symptom to failure point to underlying
  cause; a list of uncertainties is not a diagnosis.
- `continue` whenever a missing fact can be obtained with registered read-only
  probes. Reading logs/source/config, locating files, checking process state,
  reconstructing a traceback, and comparing repository changes are technical
  work, never user decisions.
- `needs_user_decision` only for an actual target/scope/product choice that
  cannot be inferred from evidence. Never ask the user to perform a probe.
- `blocked` only when evidence proves that every safe registered route needed
  for the goal is unavailable. A single refused path or failed probe is not
  enough if another registered route can locate the same fact.
- Request at most four probes and do not repeat an attempted probe key.
- Use only registered probes. Never propose mutations or shell commands.
- Write user-visible reason and user_question in Chinese.

Registered probes:
%s
""".strip()


@dataclass(frozen=True)
class DiagnosticDecision:
    status: str
    probe_requests: list[ProbeRequest] = field(default_factory=list)
    reason: str = ""
    user_question: str = ""

    def __post_init__(self) -> None:
        if self.status not in {
            "achieved", "continue", "needs_user_decision", "blocked",
        }:
            raise ValueError("invalid diagnostic decision status")
        if self.status == "continue" and not self.probe_requests:
            raise ValueError("continue requires probe_requests")
        if self.status == "needs_user_decision" and not self.user_question:
            raise ValueError("needs_user_decision requires user_question")


class V4DiagnosticPlannerAgent:
    """Turn synthesized gaps into another bounded read-only evidence round."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def assess(
        self,
        goal: str,
        bundle: EvidenceBundle,
        conclusion: EvidenceConclusion,
        *,
        attempted_keys: set[str] | None = None,
    ) -> DiagnosticDecision:
        attempted_keys = set(attempted_keys or set())
        if (
            not _is_causal_diagnosis_goal(goal)
            and not conclusion.uncertainties
            and not conclusion.missing_decisions
        ):
            return DiagnosticDecision("achieved")
        if self.llm is None:
            return self._fallback(conclusion)
        payload = {
            "goal": goal,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "probe": item.request.probe,
                    "args": item.request.args,
                    "status": item.status,
                    "collected_at": item.collected_at,
                    "output": item.output[:7000],
                }
                for item in bundle.records
            ],
            "conclusion": {
                "confirmed_facts": [
                    {"text": item.text, "evidence_refs": item.evidence_refs}
                    for item in conclusion.confirmed_facts
                ],
                "uncertainties": [
                    {"text": item.text, "evidence_refs": item.evidence_refs}
                    for item in conclusion.uncertainties
                ],
                "missing_decisions": list(conclusion.missing_decisions),
            },
            "attempted_probe_keys": sorted(attempted_keys),
        }
        messages = [
            {
                "role": "system",
                "content": DIAGNOSTIC_OUTCOME_PROMPT
                % DEFAULT_READONLY_PROBES.render(),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self.llm.complete(
                    messages=messages,
                    tools=None,
                    reasoning_effort="low",
                    temperature=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                content = response.choices[0].message.content or ""
                return self._decision(
                    parse_json_object(content),
                    attempted_keys,
                    goal=goal,
                    conclusion=conclusion,
                )
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    messages.extend([
                        {"role": "assistant", "content": content if "content" in locals() else ""},
                        {
                            "role": "user",
                            "content": (
                                "修复 JSON 合同，不要改变证据事实。错误：%s"
                                % type(exc).__name__
                            ),
                        },
                    ])
        fallback = self._fallback(conclusion)
        if fallback.status == "achieved":
            return fallback
        return DiagnosticDecision(
            "blocked",
            reason="诊断完成度评估器无法形成有效的补证合同：%s"
            % type(last_error).__name__,
        )

    @staticmethod
    def _decision(
        data: dict[str, Any],
        attempted_keys: set[str],
        *,
        goal: str,
        conclusion: EvidenceConclusion,
    ) -> DiagnosticDecision:
        status = str(data.get("status") or "").strip().lower()
        question = str(data.get("user_question") or "").strip()
        if status == "achieved" and _is_causal_diagnosis_goal(goal):
            facts = " ".join(item.text for item in conclusion.confirmed_facts)
            if not re.search(
                r"根因|原因(?:是|为)|导致|由于|caused by|due to|root cause|"
                r"未发现.{0,20}(?:报错|异常|故障|error|failure)",
                facts,
                re.I,
            ):
                raise ValueError("diagnostic goal lacks a supported causal conclusion")
        if status == "needs_user_decision":
            if _question_offloads_discoverable_work(question):
                raise ValueError("discoverable technical work cannot be offloaded")
            if not re.search(
                r"目标|范围|实例|项目根目录|哪个|哪一个|选择|授权|是否允许|"
                r"target|scope|instance|project root|choose|authorize",
                question,
                re.I,
            ):
                raise ValueError("needs_user_decision lacks a genuine choice")
        requests: list[ProbeRequest] = []
        for raw in (data.get("probe_requests") or [])[:4]:
            if not isinstance(raw, dict):
                continue
            probe, args = normalize_probe_request(
                str(raw.get("probe") or ""),
                dict(raw.get("args") or {}),
            )
            if DEFAULT_READONLY_PROBES.get(probe) is None:
                continue
            request = ProbeRequest(
                probe,
                args,
                str(raw.get("purpose") or "补齐诊断证据"),
            )
            if request.cache_key not in attempted_keys:
                requests.append(request)
        return DiagnosticDecision(
            status,
            probe_requests=requests,
            reason=str(data.get("reason") or "").strip(),
            user_question=question,
        )

    @staticmethod
    def _fallback(conclusion: EvidenceConclusion) -> DiagnosticDecision:
        if not conclusion.uncertainties and not conclusion.missing_decisions:
            return DiagnosticDecision("achieved")
        return DiagnosticDecision(
            "blocked",
            reason="仍存在未解决的证据缺口，且诊断规划器不可用。",
        )


def _is_causal_diagnosis_goal(goal: str) -> bool:
    return bool(re.search(
        r"为什么|根因|报错|异常|故障|诊断|排查|why|root cause|error|"
        r"failure|diagnos|troubleshoot",
        str(goal or ""),
        re.I,
    ))


def _question_offloads_discoverable_work(question: str) -> bool:
    return bool(re.search(
        r"(?:提供|获取|读取|查看|检查|确认).{0,30}"
        r"(?:日志|堆栈|源码|配置|PID|进程|端口|Screen|文件)|"
        r"(?:日志|堆栈|源码|配置|PID|进程|端口|Screen|文件).{0,30}"
        r"(?:提供|获取|读取|查看|检查|确认)|"
        r"provide|fetch|read|inspect|check.{0,30}"
        r"(?:log|traceback|source|config|process|port|file)",
        str(question or ""),
        re.I,
    ))
