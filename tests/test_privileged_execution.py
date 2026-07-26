from __future__ import annotations

import os
import sys


def _step(command, **overrides):
    from klonet_agent.ops.privileged.contracts import PrivilegedStep

    values = {
        "step_id": "step-1",
        "title": "run command",
        "command": command,
        "risk": "low",
        "timeout": 5,
    }
    values.update(overrides)
    return PrivilegedStep(**values)


def test_executor_captures_bounded_stdout_stderr_and_return_code():
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    command = (
        '"%s" -c "import sys; print(\'hello\'); '
        'print(\'problem\', file=sys.stderr)"' % sys.executable
    )
    executor = PrivilegedCommandExecutor(max_output_chars=100)

    evidence = executor.execute(_step(command))

    assert evidence.return_code == 0
    assert "hello" in evidence.stdout
    assert "problem" in evidence.stderr
    assert evidence.started_at
    assert evidence.finished_at
    assert evidence.timed_out is False
    assert evidence.environment_changed is True


def test_executor_timeout_is_execution_unknown_evidence_and_never_retries():
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    calls = []
    executor = PrivilegedCommandExecutor(on_start=lambda command: calls.append(command))
    command = '"%s" -c "import time; time.sleep(2)"' % sys.executable

    evidence = executor.execute(_step(command, timeout=1))

    assert evidence.timed_out is True
    assert evidence.return_code is None
    assert calls == [command]


def test_executor_inherits_stdin_for_interactive_sudo_contract(monkeypatch):
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    seen = {}

    class FakeProcess:
        stdout = None
        stderr = None

        def communicate(self, timeout):
            return "ok", ""

        @property
        def returncode(self):
            return 0

    def fake_popen(*args, **kwargs):
        seen.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("klonet_agent.ops.privileged.executor.subprocess.Popen", fake_popen)

    evidence = PrivilegedCommandExecutor().execute(_step("sudo whoami"))

    assert evidence.return_code == 0
    assert "stdin" not in seen
    assert seen["shell"] is True


def test_executor_turns_process_launch_error_into_failed_evidence(monkeypatch):
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    def fail_popen(*args, **kwargs):
        raise OSError("working directory missing")

    monkeypatch.setattr("klonet_agent.ops.privileged.executor.subprocess.Popen", fail_popen)

    evidence = PrivilegedCommandExecutor().execute(_step("echo ok"))

    assert evidence.return_code == 127
    assert "working directory missing" in evidence.stderr
    assert evidence.timed_out is False


def test_checker_registry_verifies_files_without_shell(tmp_path):
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    target = tmp_path / "nginx.conf"
    target.write_text("server_name example.test;\n", encoding="utf-8")
    registry = DefaultCheckerRegistry()

    exists = registry.run({"checker": "file_exists", "args": {"path": str(target)}})
    contains = registry.run(
        {
            "checker": "file_contains",
            "args": {"path": str(target), "text": "server_name example.test"},
        }
    )

    assert exists.status == "passed"
    assert contains.status == "passed"


def test_checker_registry_reports_unknown_or_missing_dependency_as_unavailable():
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    registry = DefaultCheckerRegistry()

    unknown = registry.run({"checker": "does_not_exist", "args": {}})
    unavailable = registry.run(
        {
            "checker": "command_available",
            "args": {"command": "definitely-not-a-real-command-93841"},
        }
    )

    assert unknown.status == "unavailable"
    assert unavailable.status == "failed"


def test_checker_registry_exit_code_zero_uses_execution_evidence():
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence

    evidence = ExecutionEvidence(return_code=7, stderr="failed")

    result = DefaultCheckerRegistry().run(
        {"checker": "exit_code_zero", "args": {}},
        evidence=evidence,
    )

    assert result.status == "failed"
    assert result.observed == "return_code=7"


def test_checker_inference_adds_required_postconditions_for_known_command_families():
    from klonet_agent.ops.privileged.checkers import infer_postconditions

    systemd = infer_postconditions("sudo systemctl restart nginx")
    pip_install = infer_postconditions("python -m pip install flask==3.0.0")
    nginx = infer_postconditions("sudo nginx -t && sudo systemctl reload nginx")

    assert {"checker": "service_active", "args": {"service": "nginx"}} in systemd
    assert any(item["checker"] == "package_version" for item in pip_install)
    assert any(item["checker"] == "python_import_succeeds" for item in pip_install)
    assert any(item["checker"] == "nginx_config_valid" for item in nginx)


def test_unknown_mutation_without_postcondition_is_partial_verification():
    from klonet_agent.ops.privileged.checkers import ensure_postconditions

    checks, level = ensure_postconditions("custom-admin-tool mutate foo", [])

    assert checks == [{"checker": "exit_code_zero", "args": {}}]
    assert level == "partial"
