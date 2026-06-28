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


def test_environment_tools_are_registered_for_llm():
    from klonet_agent.tools.registry import TOOLS

    tool_names = {item["function"]["name"] for item in TOOLS}

    assert "inspect_system_environment" in tool_names
    assert "inspect_klonet_runtime" in tool_names
    assert "read_klonet_logs" in tool_names


def test_executor_dispatches_environment_tool():
    from klonet_agent.tools.executor import ToolExecutor

    result = ToolExecutor(allowed_tools={"inspect_system_environment"}).run(
        "inspect_system_environment",
        {"checks": ["os"]},
    )

    assert "inspect_system_environment" in result
    assert any(status in result for status in ("detected", "missing", "unchecked"))
