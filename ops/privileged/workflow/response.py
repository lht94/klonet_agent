"""User-facing response rendering for Ops-Privilege."""

from __future__ import annotations

from typing import Any
import re

from klonet_agent.ops.privileged.workflow.contracts import (
    EvidenceBundle, EvidenceConclusion,
)
from klonet_agent.ops.privileged.workflow.runtime_inventory import (
    RuntimeInventory, render_runtime_goal, runtime_inventory_answers_goal,
)


class ResponseAgent:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def render_readonly(
        self, goal: str, conclusion: EvidenceConclusion,
        evidence_bundle: EvidenceBundle | None = None,
    ) -> str:
        inventory = RuntimeInventory.from_bundle(evidence_bundle)
        runtime_inventory = (
            render_runtime_goal(goal, inventory)
            if runtime_inventory_answers_goal(goal, inventory)
            else self._runtime_inventory_response(goal, conclusion)
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
                                "你是 Klonet Ops-Privilege 回答器。只根据已确认事实和"
                                "不确定项回答中文，不输出内部证据 ID，不建议或执行变更。"
                                "保持事实、顺序和含义不变；使用合理分段，列表项分别换行，"
                                "不要输出多余空行。"
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
                if text:
                    return text
            except Exception:
                pass
        return self._fallback(conclusion)

    @staticmethod
    def _runtime_inventory_response(
        goal: str,
        conclusion: EvidenceConclusion,
    ) -> str:
        if not any(
            marker in str(goal or "").lower()
            for marker in ("多少", "几个", "数量", "哪些", "how many", "which")
        ):
            return ""
        facts = [item.text for item in conclusion.confirmed_facts]
        count_text = next(
            (item for item in facts if item.startswith("runtime_inventory_counts ")),
            "",
        )
        if not count_text:
            return ""
        counts = {
            key: int(value)
            for key, value in re.findall(
                r"\b(healthy_count|abnormal_count|code_only_count)=(\d+)",
                count_text,
            )
        }
        instances = [
            item[len("runtime_instance "):]
            for item in facts
            if item.startswith("runtime_instance ")
        ]
        healthy = [item for item in instances if "backend_status=healthy" in item]
        abnormal = [item for item in instances if "backend_status=abnormal" in item]
        code_only = [
            item[len("runtime_code_only "):].partition("=")[2]
            for item in facts
            if item.startswith("runtime_code_only code_only_root=")
        ]

        def render_instance(line: str) -> str:
            def field(name: str, default: str = "unknown") -> str:
                match = re.search(r"(?:^|\s)%s=([^\s]+)" % re.escape(name), line)
                return match.group(1) if match else default
            return (
                "- project_root=%s；platform=%s；backend_status=%s；"
                "master_port=%s，master_endpoint=%s；worker_port=%s，worker_endpoint=%s"
                % (
                    field("project_root"), field("platform"), field("backend_status"),
                    field("master_port"), field("master_endpoint"),
                    field("worker_port"), field("worker_endpoint"),
                )
            )

        lines = ["正常运行实例（%s）：" % counts.get("healthy_count", len(healthy))]
        lines.extend(render_instance(item) for item in healthy)
        if not healthy:
            lines.append("- 无")
        lines.append("后端异常的运行候选（%s）：" % counts.get("abnormal_count", len(abnormal)))
        lines.extend(render_instance(item) for item in abnormal)
        if not abnormal:
            lines.append("- 无")
        lines.append("只有代码、没有后端运行证据的目录（%s）：" % counts.get("code_only_count", len(code_only)))
        lines.extend("- %s" % path for path in code_only)
        if not code_only:
            lines.append("- 无")
        return "\n".join(lines)

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
