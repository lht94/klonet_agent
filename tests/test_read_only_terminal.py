def test_readonly_terminal_runs_allowlisted_program_without_shell():
    from klonet_agent.tools.read_only_terminal import run_readonly_command

    result = run_readonly_command({"program": "which", "argv": ["python3"]})

    assert "readonly_command" in result
    assert "returncodes=0" in result
    assert "python3" in result


def test_readonly_terminal_supports_structured_pipeline():
    from klonet_agent.tools.read_only_terminal import run_readonly_command

    result = run_readonly_command(
        {
            "pipeline": [
                {"program": "which", "argv": ["python3"]},
                {"program": "grep", "argv": ["python"]},
            ],
            "stderr": "discard",
        }
    )

    assert "returncodes=0,0" in result
    assert "python" in result


def test_readonly_terminal_rejects_python_code_and_package_install():
    from klonet_agent.tools.read_only_terminal import run_readonly_command

    code = run_readonly_command({"program": "python3", "argv": ["-c", "print(1)"]})
    install = run_readonly_command({"program": "pip", "argv": ["install", "flask"]})

    assert "python only allows" in code
    assert "pip only allows" in install


def test_readonly_terminal_rejects_mutating_find_and_unknown_program():
    from klonet_agent.tools.read_only_terminal import run_readonly_command

    find = run_readonly_command({"program": "find", "argv": ["/tmp", "-delete"]})
    shell = run_readonly_command({"program": "bash", "argv": ["-lc", "id"]})

    assert "find mutating" in find
    assert "program_not_allowlisted=bash" in shell


def test_readonly_terminal_rejects_ss_socket_kill():
    from klonet_agent.tools.read_only_terminal import run_readonly_command

    result = run_readonly_command({"program": "ss", "argv": ["-K", "dst", "127.0.0.1"]})

    assert "ss socket-kill mode is not allowed" in result


def test_ops_profile_exposes_readonly_terminal():
    from klonet_agent.agents.profile import get_profile

    assert "run_readonly_command" in get_profile("ops").allowed_tools
    assert "run_readonly_command" not in get_profile("mentor").allowed_tools
