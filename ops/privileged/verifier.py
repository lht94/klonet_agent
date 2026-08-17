"""独立、证据驱动且受确定性规则约束的 Verifier。"""

from __future__ import annotations

from typing import Any
import json
import re

from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry
from klonet_agent.ops.privileged.contracts import (
    PrivilegedPlan,
    PrivilegedStep,
    VerificationDecision,
)
from klonet_agent.ops.privileged.action_contracts import _parse_json_object
from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.workflow.contracts import (
    EvidenceBundle,
    EvidenceConclusion,
    GoalOutcome,
    ProbeRequest,
    normalize_probe_request,
)
from klonet_agent.ops.privileged.workflow.runtime_inventory import (
    RuntimeInventory, runtime_inventory_answers_goal,
)


VERIFIER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Verifier Agent.
Judge whether one semantic step achieved its observable success criteria.
Deterministic failures and non-zero/unknown execution outcomes are hard lower
bounds: you may explain them but never change them to passed.

Return one JSON object. If more evidence is materially required, use:
{"status":"need_evidence","probe_requests":[
 {"probe":"registered read-only probe","args":{},"purpose":"..."}
]}
Otherwise use:
{
 "status":"passed|failed|inconclusive",
 "summary":"concise Chinese result",
 "confirmed_facts":[],
 "failed_criteria":[],
 "missing_evidence":[],
 "reflection":"what the execution result teaches the next Planner",
 "recommended_next_focus":"what evidence or state to address next"
}
Never request mutation, invent facts, or expose secrets.
""".strip()

MAX_VERIFICATION_PROBE_ROUNDS = 2


GOAL_VERIFIER_SYSTEM_PROMPT = """
You are the single goal-level outcome Verifier for Klonet Ops-Privilege.
Evaluate whether the user's requested outcome has actually been obtained and
select the next workflow transition. Do not generate or execute mutations.

Return exactly one JSON object:
{
  "status": "achieved|need_evidence|need_replan|needs_user_decision|blocked",
  "reason": "short reason in Chinese",
  "user_question": "only for a genuine user choice",
  "evidence_requests": [
    {"probe":"registered probe","args":{},"purpose":"..."}
  ]
}

Rules:
- `achieved` requires evidence that directly answers the whole user goal. For
  diagnosis, diagnosis.status must be cause_confirmed or no_failure_confirmed.
  cause_confirmed requires a supported causal chain from symptom to failure
  point to underlying cause; a list of uncertainties is not a diagnosis.
- Once that causal chain is supported, uncertainties about historical timing
  or which future fix to choose do not prevent a diagnostic goal from being
  achieved. Those are separate change decisions, not missing diagnosis facts.
- `need_evidence` whenever a missing fact can be obtained with registered
  read-only probes. Reading logs/source/config, locating files, checking
  process state, reconstructing a traceback, and comparing repository changes
  are technical work, never user decisions.
- `needs_user_decision` only for an actual target, scope, product, or
  authorization choice that cannot be inferred from evidence. Never ask the
  user to perform a probe.
- `blocked` only when evidence proves every safe registered route required for
  the goal is unavailable. One refused path or failed probe is insufficient
  when another registered route can find the same fact.
- `need_replan` is valid only in `post_execution` phase, after evidence proves
  the approved plan did not achieve the goal and identifies enough cause to
  safely revise only the unmet effects.
- Request at most four probes and never repeat an attempted probe key.
- Use only registered probes. Write user-visible text in Chinese.

Registered probes:
%s
""".strip()


class PrivilegedVerifierAgent:
    def __init__(
        self,
        llm: Any | None,
        registry: DefaultCheckerRegistry | None = None,
        probe_runner: Any | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry or DefaultCheckerRegistry()
        self.probe_runner = probe_runner

    @staticmethod
    def verify_execution_outcome(plan: Any) -> GoalOutcome:
        """Convert fully verified execution evidence into the goal transition."""

        incomplete: list[str] = []
        failed: list[str] = []
        for change in getattr(plan, "steps", ()) or ():
            if str(getattr(change, "status", "")) != "completed":
                incomplete.append(str(getattr(change, "step_id", "unknown")))
            implementation = getattr(change, "implementation_plan", None)
            execution_steps = (
                getattr(implementation, "steps", ())
                if implementation is not None else (change,)
            )
            for step in execution_steps:
                step_id = str(getattr(step, "step_id", "unknown"))
                if str(getattr(step, "status", "")) != "completed":
                    incomplete.append(step_id)
                evidence = getattr(step, "evidence", None)
                if evidence is not None and (
                    getattr(evidence, "return_code", None) != 0
                    or bool(getattr(evidence, "timed_out", False))
                ):
                    failed.append(step_id)
                if any(
                    str(getattr(check, "status", "")) == "failed"
                    for check in getattr(step, "checks", ()) or ()
                ):
                    failed.append(step_id)
        if incomplete or failed:
            criteria = sorted(set([*incomplete, *failed]))
            return GoalOutcome(
                "need_replan",
                reason="执行计划尚未形成全部通过的验证证据。",
                failed_criteria=criteria,
            )
        return GoalOutcome(
            "achieved",
            reason="全部已审批变更及其后置条件均已通过验证。",
        )

    def verify_goal(
        self,
        goal: str,
        bundle: EvidenceBundle,
        conclusion: EvidenceConclusion,
        *,
        attempted_keys: set[str] | None = None,
        phase: str = "readonly",
        goal_kind: str = "health_check",
    ) -> GoalOutcome:
        """Evaluate the whole user goal, independently of any single step."""

        attempted_keys = set(attempted_keys or set())
        if (
            phase == "readonly"
            and runtime_inventory_answers_goal(
                goal, RuntimeInventory.from_bundle(bundle),
            )
        ):
            return GoalOutcome("achieved")
        if (
            phase == "readonly"
            and
            goal_kind != "causal_diagnosis"
            and not conclusion.uncertainties
            and not conclusion.missing_decisions
        ):
            return GoalOutcome("achieved")
        if self.llm is None:
            return self._goal_fallback(
                goal, conclusion, phase=phase, goal_kind=goal_kind,
            )
        payload = {
            "goal": goal,
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
                "diagnosis": {
                    "status": conclusion.diagnosis.status,
                    "symptom": conclusion.diagnosis.symptom,
                    "failure_point": conclusion.diagnosis.failure_point,
                    "root_cause": conclusion.diagnosis.root_cause,
                    "evidence_refs": conclusion.diagnosis.evidence_refs,
                },
            },
            "evidence": self._goal_evidence_payload(bundle, conclusion),
            "attempted_probe_keys": sorted(attempted_keys),
            "workflow_phase": phase,
            "goal_kind": goal_kind,
        }
        messages = [
            {
                "role": "system",
                "content": GOAL_VERIFIER_SYSTEM_PROMPT
                % DEFAULT_READONLY_PROBES.render(),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            content = ""
            try:
                response = self.llm.complete(
                    messages=messages,
                    tools=None,
                    reasoning_effort="low",
                    temperature=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                content = response.choices[0].message.content or ""
                return self._goal_outcome(
                    _parse_json_object(content),
                    attempted_keys,
                    goal=goal,
                    conclusion=conclusion,
                    phase=phase,
                    goal_kind=goal_kind,
                )
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    messages.extend([
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "修复 GoalOutcome JSON 合同，不要改变证据事实。错误：%s"
                                % type(exc).__name__
                            ),
                        },
                    ])
        fallback = self._goal_fallback(
            goal, conclusion, phase=phase, goal_kind=goal_kind,
        )
        if fallback.status == "achieved":
            return fallback
        return GoalOutcome(
            "blocked",
            reason="Verifier 无法形成有效的目标级补证合同：%s"
            % type(last_error).__name__,
        )

    @staticmethod
    def _goal_outcome(
        data: dict[str, Any],
        attempted_keys: set[str],
        *,
        goal: str,
        conclusion: EvidenceConclusion,
        phase: str,
        goal_kind: str,
    ) -> GoalOutcome:
        status = str(data.get("status") or "").strip().lower()
        question = str(data.get("user_question") or "").strip()
        if (
            status == "achieved"
            and phase == "readonly"
            and goal_kind == "causal_diagnosis"
        ):
            if conclusion.diagnosis.status not in {
                "cause_confirmed", "no_failure_confirmed",
            }:
                raise ValueError("diagnostic goal lacks causal evidence")
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
        if status == "need_replan":
            if phase != "post_execution":
                raise ValueError("need_replan is only valid after execution")
            facts = " ".join(item.text for item in conclusion.confirmed_facts)
            if not re.search(
                r"失败|未达到|根因|原因(?:是|为)|导致|由于|failed|unmet|"
                r"caused by|due to|root cause",
                facts,
                re.I,
            ):
                raise ValueError("need_replan lacks supported failure evidence")
        requests: list[ProbeRequest] = []
        for raw in (data.get("evidence_requests") or [])[:4]:
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
                str(raw.get("purpose") or "补齐目标证据"),
            )
            if request.cache_key not in attempted_keys:
                requests.append(request)
        return GoalOutcome(
            status,
            reason=str(data.get("reason") or "").strip(),
            evidence_requests=requests,
            user_question=question,
            failed_criteria=[
                str(item) for item in data.get("failed_criteria", [])
                if str(item).strip()
            ],
            next_objective=str(data.get("next_objective") or "").strip(),
        )

    @staticmethod
    def _goal_fallback(
        goal: str, conclusion: EvidenceConclusion, *, phase: str,
        goal_kind: str = "health_check",
    ) -> GoalOutcome:
        if (
            phase == "readonly"
            and goal_kind == "causal_diagnosis"
            and conclusion.diagnosis.status not in {
                "cause_confirmed", "no_failure_confirmed",
            }
        ):
            return GoalOutcome(
                "blocked",
                reason="诊断尚未形成经过证据支持的完整因果链。",
            )
        if not conclusion.uncertainties and not conclusion.missing_decisions:
            return GoalOutcome("achieved")
        return GoalOutcome(
            "blocked",
            reason="仍存在未解决的目标证据缺口，且 Verifier 不可用。",
        )

    @staticmethod
    def _goal_evidence_payload(
        bundle: EvidenceBundle,
        conclusion: EvidenceConclusion,
        *,
        output_budget: int = 32000,
    ) -> list[dict[str, Any]]:
        """Keep goal evidence bounded and prioritize synthesized references."""

        referenced = {
            evidence_id
            for claim in [
                *conclusion.confirmed_facts,
                *conclusion.uncertainties,
            ]
            for evidence_id in claim.evidence_refs
        }
        ordered = [
            record for record in bundle.records
            if record.evidence_id in referenced
        ] + [
            record for record in bundle.records
            if record.request.probe == "klonet_knowledge"
            and record.evidence_id not in referenced
        ] + [
            record for record in reversed(bundle.records)
            if record.evidence_id not in referenced
            and record.request.probe != "klonet_knowledge"
        ]
        result: list[dict[str, Any]] = []
        remaining = max(1000, int(output_budget))
        for item in ordered:
            if remaining <= 0:
                break
            output = str(item.output or "")
            limit = min(6000, remaining)
            if len(output) > limit:
                half = max(1, (limit - 48) // 2)
                output = (
                    output[:half]
                    + "\n...[goal evidence compacted]...\n"
                    + output[-half:]
                )
            remaining -= len(output)
            result.append({
                "evidence_id": item.evidence_id,
                "probe": item.request.probe,
                "args": item.request.args,
                "status": item.status,
                "collected_at": item.collected_at,
                "output": output,
            })
        return result

    def verify_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
    ) -> VerificationDecision:
        deterministic = self._deterministic_gate(step)
        if deterministic is not None:
            if self.llm is None:
                return deterministic
            return self._agent_review(
                plan,
                step,
                deterministic_floor=deterministic,
            )
        if self._has_strong_deterministic_success(step):
            return VerificationDecision(
                status="passed",
                step_achieved=True,
                verification_level=plan.verification_level,
                reason="all deterministic state checks passed",
                confirmed_facts=[
                    "%s=%s" % (item.checker, item.observed)
                    for item in step.checks
                ],
            )
        if self.llm is not None:
            return self._agent_review(
                plan,
                step,
                deterministic_floor=None,
            )
        return VerificationDecision(
            status="passed",
            step_achieved=True,
            verification_level=plan.verification_level,
            reason=(
                "execution completed and all configured deterministic checks passed"
            ),
        )

    def _agent_review(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
        *,
        deterministic_floor: VerificationDecision | None,
    ) -> VerificationDecision:
        payload = {
            "goal": plan.goal,
            "semantic_step": {
                "step_id": step.step_id,
                "objective": step.objective or step.title,
                "reason": step.reason,
                "success_criteria": step.success_criteria,
                "expected_effects": step.expected_changes,
            },
            "execution_binding": (
                step.execution_binding.to_dict()
                if step.execution_binding is not None
                else None
            ),
            "execution_evidence": (
                step.evidence.to_dict() if step.evidence is not None else None
            ),
            "deterministic_checks": [
                item.to_dict() for item in step.checks
            ],
            "deterministic_floor": (
                deterministic_floor.to_dict()
                if deterministic_floor is not None
                else None
            ),
            "registered_probe_catalog": DEFAULT_READONLY_PROBES.render(),
        }
        messages = [
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                )[:24000],
            },
        ]
        probe_round = 0
        probe_history = []
        for _attempt in range(4):
            try:
                data = self._json_call(messages)
            except Exception:
                decision = deterministic_floor or VerificationDecision(
                    status="inconclusive",
                    step_achieved=False,
                    verification_level=plan.verification_level,
                    missing_evidence=["Verifier Agent response unavailable"],
                    reason=(
                        "deterministic checks were insufficient and the"
                        " Verifier Agent did not return a valid decision"
                    ),
                )
                decision.probe_history = list(probe_history)
                return decision
            status = str(data.get("status") or "").strip().lower()
            if status == "need_evidence":
                if (
                    self.probe_runner is None
                    or probe_round >= MAX_VERIFICATION_PROBE_ROUNDS
                ):
                    decision = deterministic_floor or VerificationDecision(
                        status="inconclusive",
                        step_achieved=False,
                        verification_level=plan.verification_level,
                        missing_evidence=[
                            "Verifier requested unavailable additional evidence"
                        ],
                        reason="verification evidence remains insufficient",
                    )
                    decision.probe_history = list(probe_history)
                    return decision
                requests = self._probe_requests(
                    data.get("probe_requests")
                )
                evidence = self.probe_runner(requests)
                probe_round += 1
                probe_history.append(
                    {
                        "round": probe_round,
                        "requests": requests,
                        "evidence": str(evidence)[:16000],
                    }
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": json.dumps(data, ensure_ascii=False),
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Additional read-only verification evidence:\n%s"
                            % str(evidence)[:16000]
                        ),
                    }
                )
                continue
            if status not in {"passed", "failed", "inconclusive"}:
                raise ValueError("invalid Verifier Agent status")
            if (
                deterministic_floor is not None
                and deterministic_floor.status in {"failed", "blocked"}
            ):
                status = deterministic_floor.status
            return VerificationDecision(
                status=status,
                step_achieved=status == "passed",
                verification_level=plan.verification_level,
                failures=(
                    list(deterministic_floor.failures)
                    if deterministic_floor is not None
                    else []
                ),
                missing_evidence=_strings(data.get("missing_evidence")),
                reason=str(data.get("summary") or "").strip()[:1200],
                next_action=str(
                    data.get("recommended_next_focus") or ""
                ).strip()[:1000],
                confirmed_facts=_strings(data.get("confirmed_facts")),
                failed_criteria=_strings(data.get("failed_criteria")),
                reflection=str(data.get("reflection") or "").strip()[:2000],
                recommended_next_focus=str(
                    data.get("recommended_next_focus") or ""
                ).strip()[:1000],
                probe_history=probe_history,
            )
        return deterministic_floor or VerificationDecision(
            status="inconclusive",
            step_achieved=False,
            verification_level=plan.verification_level,
            reason="Verifier Agent did not reach a conclusion",
            probe_history=probe_history,
        )

    def _json_call(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            response = self.llm.complete(
                messages=messages,
                tools=None,
                reasoning_effort="medium",
                temperature=0,
                response_format={"type": "json_object"},
            )
        except TypeError:
            response = self.llm.complete(messages=messages, tools=None)
        content = response.choices[0].message.content or ""
        return _parse_json_object(content)

    @staticmethod
    def _probe_requests(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValueError("Verifier need_evidence requires probes")
        known = {
            spec.name for spec in DEFAULT_READONLY_PROBES.describe()
        }
        result = []
        for item in value[:3]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("probe") or "").strip()
            if name not in known:
                raise ValueError("Verifier probe not registered=%s" % name)
            args = item.get("args")
            result.append(
                {
                    "probe": name,
                    "args": args if isinstance(args, dict) else {},
                    "purpose": str(item.get("purpose") or "").strip()[:500],
                }
            )
        if not result:
            raise ValueError("Verifier probes are empty")
        return result

    @staticmethod
    def _has_strong_deterministic_success(step: PrivilegedStep) -> bool:
        """A real state checker is stronger than an LLM interpretation."""

        required_state_checks = [
            result
            for result in step.checks
            if result.checker != "exit_code_zero"
        ]
        return bool(required_state_checks) and all(
            result.status == "passed" for result in required_state_checks
        )

    def verify_deterministic_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
    ) -> VerificationDecision:
        """Verify a read-only action from execution evidence and Checkers only."""

        deterministic = self._deterministic_gate(step)
        if deterministic is not None:
            return deterministic
        return VerificationDecision(
            status="passed",
            step_achieved=True,
            verification_level=plan.verification_level,
            reason="all deterministic checks passed",
        )

    def verify_recovered_step(
        self,
        plan: PrivilegedPlan,
        step: PrivilegedStep,
    ) -> VerificationDecision:
        """只检查重启后的当前状态，不依赖也不伪造原执行返回码。"""

        del plan
        usable = [
            specification
            for specification in step.postconditions
            if specification.get("checker") != "exit_code_zero"
        ]
        if not usable:
            return VerificationDecision(
                status="blocked",
                step_achieved=False,
                missing_evidence=["postcondition independent of exit code"],
                reason="interrupted step has no current-state checker",
                next_action="inspect current state; do not auto-reexecute",
            )
        step.checks = [self.registry.run(item, evidence=None) for item in usable]
        failures = [item.checker for item in step.checks if item.status == "failed"]
        unavailable = [
            item.checker for item in step.checks if item.status == "unavailable"
        ]
        if failures:
            return VerificationDecision(
                status="failed",
                step_achieved=False,
                failures=failures,
                reason="current state does not satisfy recovered postconditions",
                next_action="create a new plan; do not replay the interrupted step",
            )
        if unavailable:
            return VerificationDecision(
                status="inconclusive",
                step_achieved=False,
                missing_evidence=unavailable,
                reason="current-state checker unavailable",
                next_action="inspect current state; do not auto-reexecute",
            )
        return VerificationDecision(
            status="passed",
            step_achieved=True,
            verification_level="recovered-current-state",
            reason="current state satisfies all independent postconditions",
        )

    def _deterministic_gate(
        self,
        step: PrivilegedStep,
    ) -> VerificationDecision | None:
        evidence = step.evidence
        if evidence is None:
            return VerificationDecision(
                status="blocked",
                step_achieved=False,
                failures=["missing execution evidence"],
                reason="step has no execution evidence",
                next_action="inspect current state; do not auto-reexecute",
            )
        if evidence.timed_out or evidence.return_code is None:
            return VerificationDecision(
                status="blocked",
                step_achieved=False,
                failures=["execution outcome unknown"],
                reason="execution timed out or was interrupted",
                next_action="inspect current state; do not auto-reexecute",
            )

        step.checks = [
            self.registry.run(specification, evidence=evidence)
            for specification in step.postconditions
        ]
        failures = []
        if evidence.return_code != 0:
            failures.append("return_code=%s" % evidence.return_code)
        failures.extend(
            result.checker for result in step.checks if result.status == "failed"
        )
        unavailable = [
            result.checker for result in step.checks if result.status == "unavailable"
        ]
        if failures:
            return VerificationDecision(
                status="failed",
                step_achieved=False,
                failures=failures,
                reason="execution or required checks failed",
                next_action="diagnose evidence and create a revised plan",
            )
        if unavailable:
            return VerificationDecision(
                status="inconclusive",
                step_achieved=False,
                missing_evidence=unavailable,
                reason="required checker unavailable",
                next_action="install or select a deterministic checker",
            )
        return None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item).strip()[:1000]
        for item in value[:20]
        if str(item).strip()
    ]


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
