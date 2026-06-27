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
