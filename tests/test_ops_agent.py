"""Ops agent profile and mode tests."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_ops_profile_uses_read_only_environment_tools():
    from klonet_agent.agents import get_profile

    profile = get_profile("ops")

    assert profile.name == "ops"
    assert "search_knowledge" in profile.allowed_tools
    assert "inspect_system_environment" in profile.allowed_tools
    assert "inspect_klonet_runtime" in profile.allowed_tools
    assert "read_klonet_logs" in profile.allowed_tools
    assert "run_command" not in profile.allowed_tools
    assert "write_file" not in profile.allowed_tools


def test_mentor_profile_can_recommend_ops_without_shell_access():
    from klonet_agent.agents import get_profile

    profile = get_profile("mentor")

    assert "inspect_system_environment" in profile.allowed_tools
    assert "inspect_klonet_runtime" in profile.allowed_tools
    assert "read_klonet_logs" in profile.allowed_tools
    assert "run_command" not in profile.allowed_tools


def test_agent_cli_accepts_ops_mode(monkeypatch):
    from klonet_agent.agent import main

    captured = {}

    def fake_run_chat(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("klonet_agent.app.run_chat", fake_run_chat)
    monkeypatch.setattr("sys.argv", ["agent.py", "--mode", "ops"])

    main()

    assert captured["mode"] == "ops"
