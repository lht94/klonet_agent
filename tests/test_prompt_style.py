"""Prompt 与运行文案风格测试。"""

import sys
from pathlib import Path

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


FORBIDDEN_LEGACY_WORDS = ("小鸡毛", "小白")


def test_system_prompts_use_klonet_agent_identity():
    """系统提示词应该保持 Klonet 教学协作 Agent 定位。"""

    from klonet_agent.prompts import MENTOR_PROMPT, build_system_prompts

    text = "\n".join(build_system_prompts(MENTOR_PROMPT))

    assert "Klonet 专用教学协作 Agent" in text
    assert "Klonet Mentor Agent" in text
    for word in FORBIDDEN_LEGACY_WORDS:
        assert word not in text


def test_memory_prompt_uses_teaching_agent_language():
    """记忆提示词不应该残留旧个人 Agent 称呼。"""

    from klonet_agent.memory.store import MemoryStore

    with local_temp_dir() as temp_dir:
        store = MemoryStore(temp_dir / "memory", temp_dir / "memory" / "USER.md")
        text = store.memory_prompt()

    assert "长期记忆" in text
    assert "用户画像与偏好" in text
    for word in FORBIDDEN_LEGACY_WORDS:
        assert word not in text


def test_runtime_text_does_not_use_legacy_persona_words():
    """运行时输出文案不再使用旧个人 Agent 称呼。"""

    runtime_files = [
        "app/cli.py",
        "memory/store.py",
        "orchestrator.py",
        "session.py",
        "tools/executor.py",
    ]

    for relative_path in runtime_files:
        text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for word in FORBIDDEN_LEGACY_WORDS:
            assert word not in text, f"{relative_path} 仍包含旧称呼：{word}"
