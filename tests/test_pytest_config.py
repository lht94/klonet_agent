"""pytest 配置测试。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pytest_ignores_runtime_directories():
    """根目录 pytest 不应该收集 workspace、journal 等运行时产物。"""

    config = PROJECT_ROOT / "pytest.ini"
    text = config.read_text(encoding="utf-8")

    assert "norecursedirs" in text
    assert "workspaces" in text
    assert "journals" in text
    assert "memory" in text
