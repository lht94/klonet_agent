from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "program,argv,category,confirmation",
    (
        ("systemctl", ["status", "nginx"], "service_readonly", False),
        ("systemctl", ["restart", "nginx"], "service_change", True),
        ("docker", ["ps", "-a"], "container_readonly", False),
        ("docker", ["restart", "redis"], "container_change", False),
        ("nginx", ["-t"], "nginx_syntax_check", False),
        ("nginx", ["-s", "reload"], "nginx_reload", False),
        ("redis-cli", ["-h", "127.0.0.1", "-p", "6379", "PING"], "redis_readonly", False),
        ("journalctl", ["-u", "nginx", "-n", "100", "--no-pager"], "journal_readonly", False),
        ("curl", ["-fsS", "http://127.0.0.1:8080/health"], "http_health_readonly", False),
    ),
)
def test_extended_command_policy_allows_bounded_operations(
    program,
    argv,
    category,
    confirmation,
):
    from klonet_agent.ops.command_policy import decide_ops_command

    decision = decide_ops_command(
        {"program": program, "argv": argv, "cwd": "/tmp"}
    )

    assert decision.allowed, decision.reason
    assert decision.category == category
    assert decision.requires_step_confirmation is confirmation


@pytest.mark.parametrize(
    "program,argv",
    (
        ("systemctl", ["restart", "../../evil"]),
        ("docker", ["run", "--privileged", "ubuntu"]),
        ("redis-cli", ["AUTH", "secret"]),
        ("curl", ["https://example.com/"]),
        ("journalctl", ["--output", "export"]),
    ),
)
def test_extended_command_policy_rejects_unbounded_or_secret_operations(
    program,
    argv,
):
    from klonet_agent.ops.command_policy import decide_ops_command

    decision = decide_ops_command(
        {"program": program, "argv": argv, "cwd": "/tmp"}
    )

    assert not decision.allowed


def test_git_fetch_and_reset_have_distinct_risk_contracts(tmp_path):
    from klonet_agent.ops.command_policy import decide_ops_command

    fetch = decide_ops_command(
        {
            "program": "git",
            "argv": ["fetch", "--prune", "origin"],
            "cwd": str(tmp_path),
        }
    )
    reset = decide_ops_command(
        {
            "program": "git",
            "argv": ["reset", "--hard", "HEAD"],
            "cwd": str(tmp_path),
        }
    )

    assert fetch.allowed
    assert fetch.requires_step_confirmation is False
    assert reset.allowed
    assert reset.risk == "dangerous"
    assert reset.requires_step_confirmation is True
