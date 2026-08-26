"""In-memory evidence synthesis for Ops-Privilege."""

from __future__ import annotations

import json
import re
from typing import Any

from klonet_agent.ops.privileged.workflow.contracts import (
    DiagnosisAssessment,
    EvidenceBundle,
    EvidenceClaim,
    EvidenceConclusion,
)
from klonet_agent.ops.privileged.workflow.discovery import parse_json_object
from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory
from klonet_agent.ops.privileged.context import klonet_domain_context


SYNTHESIS_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Evidence Synthesizer.
Use only the supplied read-only evidence. Never request tools, propose commands,
write files, or claim facts without evidence references.
Return one JSON object with confirmed_facts, uncertainties, missing_decisions,
and gap_assessments. Each gap assessment is
{"gap_id":"gap-...","status":"resolved|unresolved|ambiguous",
 "evidence_refs":["ev-..."]}. A gap is resolved only when the evidence proves
the requested result, not merely because a Probe returned output. Reuse the
gap_id supplied with the evidence. Use ambiguous when evidence proves multiple
valid values require a user choice.
Each fact is {"text":"...","evidence_refs":["ev-..."]}.
Also return diagnosis as
{"status":"not_applicable|incomplete|symptom_confirmed|cause_confirmed|no_failure_confirmed",
 "symptom":"", "failure_point":"", "root_cause":"", "evidence_refs":[]}.
Use cause_confirmed only when evidence supports the symptom, exact failure
point, and underlying cause. Use symptom_confirmed when an error is proven but
its cause is not. Use no_failure_confirmed only when current evidence directly
proves the investigated failure is absent. Never encode an unknown cause as a
confirmed cause.
When evidence supports a causal chain, include one explicit confirmed fact that
states the root cause and references every supporting link. Uncertainties and
missing_decisions must be necessary to answer the supplied goal. Do not list
future repair choices when the current goal only asks for diagnosis.
""".strip()


class EvidenceSynthesizer:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def synthesize(self, goal: str, bundle: EvidenceBundle) -> EvidenceConclusion:
        if self.llm is None:
            return self._fallback(bundle)
        payload = [
            {
                "evidence_id": item.evidence_id,
                "probe": item.request.probe,
                "args": item.request.args,
                "purpose": item.request.purpose,
                "required_facts": list(item.request.required_facts),
                "freshness": item.request.freshness,
                "gap_id": item.request.need_key,
                "affected_steps": list(item.request.affected_steps),
                "status": item.status,
                "output": item.output[:7000],
            }
            for item in bundle.records
        ]
        messages = [
            {
                "role": "system",
                "content": SYNTHESIS_SYSTEM_PROMPT
                + "\n\n" + klonet_domain_context("synthesis"),
            },
            {
                "role": "user",
                "content": "Goal:\n%s\n\nEvidence:\n%s\n\nBudget exhausted: %s"
                % (
                    goal,
                    json.dumps(payload, ensure_ascii=False),
                    bundle.budget_exhausted,
                ),
            },
        ]
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
            except Exception:
                # No model response means there is no JSON contract to repair.
                # The deterministic synthesis fallback is authoritative here.
                break
            try:
                content = response.choices[0].message.content or ""
                conclusion = self._conclusion(parse_json_object(content))
                conclusion.validate_against(bundle)
                return self._with_deterministic_claims(goal, bundle, conclusion)
            except Exception as exc:
                if attempt == 0:
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": "Repair the synthesis JSON. Use only listed evidence IDs. Error: %s"
                            % exc,
                        }
                    )
        return self._with_deterministic_claims(goal, bundle, self._fallback(bundle))

    @staticmethod
    def _with_deterministic_claims(
        goal: str,
        bundle: EvidenceBundle,
        conclusion: EvidenceConclusion,
    ) -> EvidenceConclusion:
        promoted: list[EvidenceClaim] = []
        goal_text = str(goal or "").lower()
        inventory = RuntimeInventory.from_bundle(bundle)
        complete_runtime_inventory_ids = set(inventory.evidence_ids) if inventory.complete else set()
        if inventory.complete:
            count_fields = {
                "runtime_candidate_count": inventory.declared_runtime_count,
                "healthy_count": inventory.declared_healthy_count,
                "abnormal_count": inventory.declared_abnormal_count,
                "code_only_count": inventory.declared_code_only_count,
            }
            refs = list(inventory.evidence_ids)
            promoted.append(EvidenceClaim(
                "runtime_inventory_counts " + " ".join(
                    "%s=%s" % (key, value)
                    for key, value in count_fields.items()
                    if value is not None
                ),
                refs,
            ))
            promoted.extend(
                EvidenceClaim(
                    "runtime_instance " + item.raw_line,
                    [item.evidence_id],
                )
                for item in inventory.instances
            )
            promoted.extend(
                EvidenceClaim(
                    "runtime_code_only code_only_root=" + root,
                    refs,
                )
                for root in inventory.code_only_roots
            )
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "screen":
                continue
            output = record.output
            selected_roots: set[str] = set()
            for match in re.finditer(
                r"(?m)^session=([A-Za-z0-9_.-]+).*?\bgit_roots=([^\s]+)",
                output,
            ):
                session = match.group(1)
                prefix = re.sub(
                    r"_(?:web|m|c|w|worker|master|controller)$",
                    "",
                    session,
                    flags=re.I,
                )
                if prefix.lower() not in goal_text:
                    continue
                selected_roots.update(
                    root
                    for root in match.group(2).split(",")
                    if root and root != "unknown"
                )
            for root in sorted(selected_roots):
                section = re.search(
                    r"path=%s\s+inside_work_tree=true\s+revision=([^\s]+)\s+"
                    r"status=##\s+([^\s]+).*?remotes=origin\s+([^\s]+)"
                    % re.escape(root),
                    output,
                    re.S,
                )
                if section is None:
                    continue
                revision, branch_value, remote = section.groups()
                branch = branch_value.split("...", 1)[0]
                promoted.append(
                    EvidenceClaim(
                        (
                            "User-selected Screen source maps authoritatively to Git "
                            "repository %s; remote=%s; branch=%s; revision=%s"
                            % (root, remote, branch, revision)
                        ),
                        [record.evidence_id],
                    )
                )
        existing = {item.text for item in conclusion.confirmed_facts}
        conclusion.confirmed_facts = [
            item for item in promoted if item.text not in existing
        ] + conclusion.confirmed_facts
        if complete_runtime_inventory_ids and any(
            marker in goal_text
            for marker in ("多少", "几个", "数量", "哪些", "how many", "which")
        ):
            conclusion.uncertainties = [
                item for item in conclusion.uncertainties
                if not set(item.evidence_refs).issubset(complete_runtime_inventory_ids)
            ]
        conclusion.validate_against(bundle)
        return conclusion

    @staticmethod
    def _conclusion(data: dict[str, Any]) -> EvidenceConclusion:
        def claims(name: str) -> list[EvidenceClaim]:
            value = data.get(name)
            if not isinstance(value, list):
                return []
            return [
                EvidenceClaim(
                    str(item.get("text") or ""),
                    [str(ref) for ref in item.get("evidence_refs", [])],
                )
                for item in value
                if isinstance(item, dict)
            ]

        missing = data.get("missing_decisions")
        assessments = data.get("gap_assessments")
        assessments = assessments if isinstance(assessments, list) else []
        resolved_gaps = {
            str(item.get("gap_id")): [
                str(ref) for ref in item.get("evidence_refs") or []
            ]
            for item in assessments
            if isinstance(item, dict)
            and str(item.get("status") or "") == "resolved"
            and str(item.get("gap_id") or "").startswith("gap-")
        }
        unresolved_gaps = [
            str(item.get("gap_id"))
            for item in assessments
            if isinstance(item, dict)
            and str(item.get("status") or "") in {"unresolved", "ambiguous"}
            and str(item.get("gap_id") or "").startswith("gap-")
        ]
        raw_diagnosis = data.get("diagnosis")
        raw_diagnosis = raw_diagnosis if isinstance(raw_diagnosis, dict) else {}
        return EvidenceConclusion(
            confirmed_facts=claims("confirmed_facts"),
            uncertainties=claims("uncertainties"),
            missing_decisions=[str(item) for item in missing]
            if isinstance(missing, list)
            else [],
            diagnosis=DiagnosisAssessment(
                status=str(raw_diagnosis.get("status") or "incomplete"),
                symptom=str(raw_diagnosis.get("symptom") or ""),
                failure_point=str(raw_diagnosis.get("failure_point") or ""),
                root_cause=str(raw_diagnosis.get("root_cause") or ""),
                evidence_refs=[
                    str(ref) for ref in raw_diagnosis.get("evidence_refs", [])
                ] if isinstance(raw_diagnosis.get("evidence_refs"), list) else [],
            ),
            resolved_gaps=resolved_gaps,
            unresolved_gaps=unresolved_gaps,
        )

    @staticmethod
    def _fallback(bundle: EvidenceBundle) -> EvidenceConclusion:
        facts = [
            EvidenceClaim(
                "%s 返回了只读证据：%s"
                % (item.request.probe, " ".join(item.output.split())[:240]),
                [item.evidence_id],
            )
            for item in bundle.records
            if item.status == "available"
        ]
        uncertainties = [
            EvidenceClaim(
                "%s 检查不可用" % item.request.probe,
                [item.evidence_id],
            )
            for item in bundle.records
            if item.status != "available"
        ]
        return EvidenceConclusion(
            confirmed_facts=facts,
            uncertainties=uncertainties,
            missing_decisions=[bundle.blocked_reason] if bundle.blocked_reason else [],
            unresolved_gaps=list(dict.fromkeys(
                item.request.need_key for item in bundle.records
                if item.request.gap_id and item.status != "available"
            )),
        )
