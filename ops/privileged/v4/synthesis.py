"""In-memory evidence synthesis for Ops-Privilege V4."""

from __future__ import annotations

import json
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
                return conclusion
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
        return self._fallback(bundle)

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
