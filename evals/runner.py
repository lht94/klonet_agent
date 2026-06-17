"""阶段一 eval runner。

当前 runner 先做离线检查：读取 eval case、校验字段、生成 Markdown 汇总。
后续接入真实模型后，可以在这里加入自动调用 Agent 和评分逻辑。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from klonet_agent.config import PROJECT_ROOT


CASE_FILES = {
    "mentor": "mentor_cases.jsonl",
    "coding": "coding_cases.jsonl",
    "error": "error_cases.jsonl",
}


class EvalRunner:
    """读取 eval case 并生成汇总报告。"""

    def __init__(
        self,
        eval_dir: Path = PROJECT_ROOT / "evals",
        output_file: Path = PROJECT_ROOT / "evals" / "summary.md",
    ):
        self.eval_dir = eval_dir
        self.output_file = output_file

    def run(self) -> dict:
        """执行离线评估汇总。"""

        cases = self.load_cases()
        by_suite = defaultdict(int)
        for case in cases:
            by_suite[case["suite"]] += 1
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.output_file.write_text(self._render_summary(cases, by_suite), encoding="utf-8")
        return {
            "total": len(cases),
            "by_suite": dict(by_suite),
            "output_file": str(self.output_file),
        }

    def load_cases(self) -> list[dict]:
        """读取所有 jsonl case。"""

        cases = []
        for suite, filename in CASE_FILES.items():
            path = self.eval_dir / filename
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    case = json.loads(line)
                    case["suite"] = suite
                    case["source_file"] = filename
                    case["line_no"] = line_no
                    _validate_case(case)
                    cases.append(case)
        return cases

    def _render_summary(self, cases: list[dict], by_suite: dict) -> str:
        """生成 Markdown 汇总。"""

        lines = [
            "# Klonet Agent Eval 汇总",
            "",
            f"- case 总数：{len(cases)}",
        ]
        for suite in CASE_FILES:
            lines.append(f"- {suite}：{by_suite.get(suite, 0)}")
        lines.append("")
        lines.append("## Cases")
        for case in cases:
            needs_code_change = case.get("needs_code_change", False)
            prompt = case.get("question") or case.get("task") or case.get("error")
            lines.append("")
            lines.append(f"### {case['id']}")
            lines.append(f"- suite: {case['suite']}")
            lines.append(f"- needs_code_change={needs_code_change}")
            lines.append(f"- input: {prompt}")
            lines.append(f"- expected_behavior: {case['expected_behavior']}")
            lines.append(f"- acceptance: {case['acceptance']}")
        return "\n".join(lines) + "\n"


def _validate_case(case: dict):
    """校验 eval case 的最小字段。"""

    required = {"id", "expected_behavior", "acceptance"}
    missing = [key for key in required if key not in case]
    if missing:
        raise ValueError(f"eval case {case.get('id', '<unknown>')} 缺少字段：{missing}")
    if not (case.get("question") or case.get("task") or case.get("error")):
        raise ValueError(f"eval case {case['id']} 缺少输入字段。")


def main():
    """命令行入口：生成 eval 汇总。"""

    summary = EvalRunner().run()
    print(f"Eval cases: {summary['total']}")
    print(f"Summary: {summary['output_file']}")


if __name__ == "__main__":
    main()
