"""User-facing response rendering for Ops-Privilege V4."""

from __future__ import annotations

from typing import Any

from klonet_agent.ops.privileged.v4.contracts import EvidenceConclusion


class V4ResponseAgent:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def render_readonly(self, goal: str, conclusion: EvidenceConclusion) -> str:
        if self.llm is not None:
            try:
                response = self.llm.complete(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是 Klonet Ops-Privilege V4 回答器。只根据已确认事实和"
                                "不确定项回答中文，不输出内部证据 ID，不建议或执行变更。"
                                "保持事实、顺序和含义不变；使用合理分段，列表项分别换行，"
                                "不要输出多余空行。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": self._prompt(goal, conclusion),
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
        return self._fallback(conclusion)

    @staticmethod
    def _prompt(goal: str, conclusion: EvidenceConclusion) -> str:
        facts = "\n".join("- %s" % item.text for item in conclusion.confirmed_facts)
        uncertain = "\n".join("- %s" % item.text for item in conclusion.uncertainties)
        missing = "\n".join("- %s" % item for item in conclusion.missing_decisions)
        return "目标：%s\n已确认：\n%s\n不确定：\n%s\n缺失决策：\n%s" % (
            goal,
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
