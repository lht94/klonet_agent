"""Server-side Ops helper contract tests."""

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER = PROJECT_ROOT / "scripts" / "klonet-agent-op"


def test_restart_screen_component_helper_dry_run_outputs_command_contract():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "restart-screen-component",
            "--dry-run",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
            "--project-root",
            "/home/adminis/lht/102_project",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "klonet_agent_op" in result.stdout
    assert "action=restart-screen-component" in result.stdout
    assert "dry_run=true" in result.stdout
    assert "platform=102" in result.stdout
    assert "component=master" in result.stdout
    assert "screen_session=102_m" in result.stdout
    assert "project_root=/home/adminis/lht/102_project" in result.stdout
    assert "environment_changed=false" in result.stdout


def test_restart_screen_component_helper_rejects_unknown_component():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "restart-screen-component",
            "--dry-run",
            "--platform",
            "102",
            "--component",
            "database",
            "--screen",
            "102_db",
            "--project-root",
            "/home/adminis/lht/102_project",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 2
    assert "unsupported_component=database" in result.stderr
    assert "environment_changed=false" in result.stderr
