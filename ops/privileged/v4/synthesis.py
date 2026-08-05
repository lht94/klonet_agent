"""In-memory evidence synthesis for Ops-Privilege V4."""

from __future__ import annotations

import json
import re
from typing import Any

from klonet_agent.ops.privileged.v4.contracts import (
    EvidenceBundle,
    EvidenceClaim,
    EvidenceConclusion,
)
from klonet_agent.ops.privileged.v4.discovery import parse_json_object


SYNTHESIS_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege V4 Evidence Synthesizer.
Use only the supplied read-only evidence. Never request tools, propose commands,
write files, or claim facts without evidence references.
Return one JSON object with confirmed_facts, uncertainties, missing_decisions.
Each fact is {"text":"...","evidence_refs":["ev-..."]}.
""".strip()


class V4EvidenceSynthesizer:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def synthesize(self, goal: str, bundle: EvidenceBundle) -> EvidenceConclusion:
        if self.llm is None:
            return self._fallback(bundle)
        payload = [
            {
                "evidence_id": item.evidence_id,
                "probe": item.request.probe,
                "purpose": item.request.purpose,
                "status": item.status,
                "output": item.output[:7000],
            }
            for item in bundle.records
        ]
        messages = [
            {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
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
            try:
                response = self.llm.complete(
                    messages=messages,
                    tools=None,
                    reasoning_effort="low",
                    temperature=0,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                content = response.choices[0].message.content or ""
                conclusion = self._conclusion(parse_json_object(content))
                conclusion.validate_against(bundle)
                return self._with_deterministic_claims(goal, bundle, conclusion)
            except Exception as exc:
                if attempt == 0:
                    messages.append({"role": "assistant", "content": content if 'content' in locals() else ""})
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
        return EvidenceConclusion(
            confirmed_facts=claims("confirmed_facts"),
            uncertainties=claims("uncertainties"),
            missing_decisions=[str(item) for item in missing]
            if isinstance(missing, list)
            else [],
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
        )
