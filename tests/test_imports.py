"""最小导入测试。

这个测试用来保证 agent_v7 -> klonet_agent 迁移后，核心模块至少可以被导入。
"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_core_imports():
    import klonet_agent
    from klonet_agent.agents import get_profile
    from klonet_agent.session import AgentSession

    assert klonet_agent.__version__
    assert get_profile("mentor").name == "mentor"
    assert get_profile("coding").name == "coding"
    assert AgentSession(user_id="u1", project_id="p1").user_id == "u1"
