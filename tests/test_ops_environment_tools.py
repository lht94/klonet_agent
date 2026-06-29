"""Read-only environment diagnostic tool tests."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_redacts_common_secret_shapes():
    from klonet_agent.tools.environment import redact_sensitive_text

    text = "PASSWORD=abc123\nAuthorization: Bearer token-value\napi_key = sk-test"

    redacted = redact_sensitive_text(text)

    assert "abc123" not in redacted
    assert "token-value" not in redacted
    assert "sk-test" not in redacted
    assert "[REDACTED]" in redacted


def test_read_only_probe_rejects_unregistered_command():
    from klonet_agent.tools.environment import run_read_only_probe

    result = run_read_only_probe("rm -rf /")

    assert result.status == "unchecked"
    assert "not allowlisted" in result.detail


def test_log_reader_refuses_env_files():
    from klonet_agent.tools.environment import read_klonet_logs
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        env_file = temp_dir / ".env"
        env_file.write_text("PASSWORD=abc123", encoding="utf-8")

        result = read_klonet_logs({"path": str(env_file)})

    assert result.startswith("Error:")
    assert "refused" in result.lower()


def test_log_reader_reports_resolved_path_mtime_and_size():
    from klonet_agent.tools.environment import read_klonet_logs
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        log_file = temp_dir / "error.log"
        log_file.write_text("first line\nlatest line\n", encoding="utf-8")

        result = read_klonet_logs({"path": str(log_file), "max_chars": 100})

    assert "resolved_path=" in result
    assert "mtime=" in result
    assert "size_bytes=" in result
    assert "latest line" in result


def test_environment_tools_are_registered_for_llm():
    from klonet_agent.tools.registry import TOOLS

    tool_names = {item["function"]["name"] for item in TOOLS}

    assert "inspect_system_environment" in tool_names
    assert "inspect_klonet_runtime" in tool_names
    assert "read_klonet_logs" in tool_names
    assert "inspect_screen_session" in tool_names


def test_ops_profile_allows_screen_inspection():
    from klonet_agent.agents import get_profile

    profile = get_profile("ops")

    assert "inspect_screen_session" in profile.allowed_tools


def test_screen_inspection_rejects_unsafe_session_name():
    from klonet_agent.tools.environment import inspect_screen_session

    result = inspect_screen_session({"session": "102_m; rm -rf /"})

    assert result.startswith("Error:")
    assert "unsafe" in result.lower()


def test_runtime_probe_supports_process_cwd_evidence():
    """Ops diagnosis needs process cwd evidence before tying a platform to source."""

    from klonet_agent.tools.environment import _probe_command
    from klonet_agent.tools.registry import TOOLS

    command = _probe_command("processes")
    runtime_tool = next(
        item
        for item in TOOLS
        if item["function"]["name"] == "inspect_klonet_runtime"
    )
    checks = runtime_tool["function"]["parameters"]["properties"]["checks"]["items"]["enum"]

    assert command is not None
    assert "processes" in checks


def test_executor_dispatches_environment_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_system_environment"}).run(
        "inspect_system_environment",
        {"checks": ["os"]},
    )

    assert "inspect_system_environment" in result
    assert any(status in result for status in ("detected", "missing", "unchecked"))


def test_executor_dispatches_screen_inspection_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_screen_session"}).run(
        "inspect_screen_session",
        {"session": "102_m; rm -rf /"},
    )

    assert result.startswith("Error:")
    assert "unsafe" in result.lower()
