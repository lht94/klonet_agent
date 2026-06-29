"""Source-evidence boundary tests."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_mentor_prompt_distinguishes_source_tree_from_source_index():
    """Mentor should not equate an empty workspace with no source evidence."""

    from klonet_agent.prompts import MENTOR_PROMPT

    assert "没有完整源码树" in MENTOR_PROMPT
    assert "机器索引" in MENTOR_PROMPT


def test_mentor_prompt_requires_source_tools_for_code_facts():
    """源码、接口、配置和报错类问题应优先用真实源码工具确认。"""

    from klonet_agent.prompts import MENTOR_PROMPT

    assert "search_code" in MENTOR_PROMPT
    assert "read_source_file" in MENTOR_PROMPT
    assert "代码、接口、配置、启动脚本或报错" in MENTOR_PROMPT


def test_ops_prompt_separates_workspace_from_runtime_source():
    """Ops must not treat uploaded workspace code as the running platform source."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "workspace != runtime source" in OPS_PROMPT
    assert "process cwd" in OPS_PROMPT
    assert "traceback" in OPS_PROMPT


def test_ops_prompt_requires_target_evidence_scope_for_logs():
    """Ops should separate workspace project evidence from external runtime evidence."""

    from klonet_agent.prompts import OPS_PROMPT

    assert "运维目标" in OPS_PROMPT
    assert "当前 workspace 项目" in OPS_PROMPT
    assert "workspace 之外" in OPS_PROMPT
    assert "error.log 只能证明历史错误" in OPS_PROMPT
    assert "不能单独证明当前仍然故障" in OPS_PROMPT
