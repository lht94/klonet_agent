"""评估集 runner 测试。"""

import sys
from pathlib import Path

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_eval_runner_loads_cases_and_writes_summary():
    """eval runner 应该读取 jsonl case 并写出汇总报告。"""

    from klonet_agent.evals.runner import EvalRunner

    with local_temp_dir() as temp_dir:
        eval_dir = temp_dir / "evals"
        output_file = temp_dir / "summary.md"
        eval_dir.mkdir()
        (eval_dir / "mentor_cases.jsonl").write_text(
            '{"id":"m1","question":"问题","expected_behavior":"检索","needs_code_change":false,"acceptance":"包含证据"}\n',
            encoding="utf-8",
        )
        (eval_dir / "coding_cases.jsonl").write_text(
            '{"id":"c1","task":"任务","expected_behavior":"测试","needs_code_change":true,"acceptance":"有 diff"}\n',
            encoding="utf-8",
        )

        summary = EvalRunner(eval_dir=eval_dir, output_file=output_file).run()
        text = output_file.read_text(encoding="utf-8")

    assert summary["total"] == 2
    assert summary["by_suite"]["mentor"] == 1
    assert summary["by_suite"]["coding"] == 1
    assert "m1" in text
    assert "c1" in text
    assert "needs_code_change=True" in text


def test_eval_runner_loads_default_eval_files():
    """仓库自带 eval case 应该满足最小字段要求。"""

    from klonet_agent.evals.runner import EvalRunner

    cases = EvalRunner(eval_dir=PROJECT_ROOT / "evals").load_cases()
    ids = {case["id"] for case in cases}

    assert {"mentor_001", "mentor_002", "coding_001", "coding_002", "error_001"} <= ids
