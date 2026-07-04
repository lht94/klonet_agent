"""pytest 配置测试。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pytest_ignores_runtime_directories():
    """根目录 pytest 不应该收集 workspace、journal 等运行时产物。"""

    config = PROJECT_ROOT / "pytest.ini"
    text = config.read_text(encoding="utf-8")

    assert "norecursedirs" in text
    assert "--basetemp=C:/tmp/klonet_agent_pytest_tmp" in text
    assert "workspaces" in text
    assert "journals" in text
    assert "memory" in text
    assert "klonet_knowledge" in text


def test_local_temp_dir_avoids_onedrive_project_tmp_by_default():
    """Test helper temp root should avoid OneDrive ACL/sync locks."""

    from tests import helpers

    assert helpers.TEST_TMP_ROOT != PROJECT_ROOT / ".test_tmp"
    assert helpers.TEST_TMP_ROOT.name == "klonet_agent_test_tmp"
