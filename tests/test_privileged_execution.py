from __future__ import annotations

from pathlib import Path

def _step(command, **overrides):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.shell_artifact import create_shell_artifact

    values = {
        "step_id": "step-1",
        "title": "run command",
        "command": command,
        "objective": "execute reviewed test script",
        "reason": "exercise the frozen shell boundary",
        "success_criteria": ["exit code is zero"],
        "risk": "low",
        "timeout": 5,
    }
    values.update(overrides)
    artifact = create_shell_artifact(
        artifact_id="shell-test",
        script=command,
        cwd="/tmp",
        run_as="",
        timeout=values["timeout"],
        environment_fingerprint="",
        declared_changes=["test fixture"],
        rollback="remove test fixture",
        nonce="nonce",
    )
    artifact.status = "approved"
    artifact.approved_contract_hash = artifact.contract_hash
    values["execution_binding"] = ExecutionBinding(
        kind="shell_artifact",
        risk=max(values["risk"], "high", key=("readonly", "low", "medium", "high", "destructive").index),
        approval_scope="step",
        shell_artifact=artifact,
        postconditions=[{"checker": "exit_code_zero", "args": {}}],
    )
    return PrivilegedStep(**values)


def test_executor_captures_bounded_stdout_stderr_and_return_code():
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    command = "printf 'hello\\n'; printf 'problem\\n' >&2"
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
    command = "sleep 2"

    evidence = executor.execute(_step(command, timeout=1))

    assert evidence.timed_out is True
    assert evidence.return_code is None
    assert len(calls) == 1
    assert "实际受控脚本" in calls[0]
    assert "cwd=/tmp" in calls[0]
    assert command in calls[0]


def test_shell_artifact_executes_fixed_argv_without_shell_interpretation(monkeypatch):
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

    class AllowPolicy:
        @staticmethod
        def validate(artifact):
            del artifact
            return ""

    monkeypatch.setattr("klonet_agent.ops.privileged.executor.subprocess.Popen", fake_popen)

    evidence = PrivilegedCommandExecutor(shell_policy=AllowPolicy()).execute(
        _step("sudo whoami")
    )

    assert evidence.return_code == 0
    assert "stdin" not in seen
    assert seen["shell"] is False


def test_executor_turns_process_launch_error_into_failed_evidence(monkeypatch):
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    def fail_popen(*args, **kwargs):
        raise OSError("working directory missing")

    class AllowPolicy:
        @staticmethod
        def validate(artifact):
            del artifact
            return ""

    monkeypatch.setattr("klonet_agent.ops.privileged.executor.subprocess.Popen", fail_popen)

    evidence = PrivilegedCommandExecutor(shell_policy=AllowPolicy()).execute(
        _step("echo ok")
    )

    assert evidence.return_code == 127
    assert "working directory missing" in evidence.stderr
    assert evidence.timed_out is False


def test_executor_runs_approved_readonly_shell_without_claiming_environment_change(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding, PrivilegedStep,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
    from klonet_agent.ops.privileged.shell_artifact import create_shell_artifact

    artifact = create_shell_artifact(
        artifact_id="shell-readonly-test",
        script="pwd",
        cwd=str(tmp_path),
        run_as="",
        timeout=5,
        environment_fingerprint="",
        declared_changes=[],
        rollback="",
        nonce="readonly-nonce",
    )
    artifact.status = "approved"
    artifact.approved_contract_hash = artifact.contract_hash
    step = PrivilegedStep(
        step_id="readonly-shell",
        title="Read current directory",
        objective="Observe the current directory without mutation",
        risk="readonly",
        execution_binding=ExecutionBinding(
            kind="shell_artifact",
            risk="readonly",
            approval_scope="plan",
            shell_artifact=artifact,
            postconditions=[{"checker": "exit_code_zero", "args": {}}],
        ),
    )

    evidence = PrivilegedCommandExecutor().execute(step)

    assert evidence.return_code == 0
    assert evidence.stdout.strip() == str(tmp_path)
    assert evidence.environment_changed is False
    assert evidence.commands[0]["changes_state"] is False

def test_readonly_executor_uses_validated_argv_without_shell(monkeypatch):
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

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("klonet_agent.ops.privileged.executor.subprocess.Popen", fake_popen)
    step = _step("find /tmp/scope -maxdepth 1", risk="readonly")

    evidence = PrivilegedCommandExecutor().execute_readonly(
        step,
        ["find", "/tmp/scope", "-maxdepth", "1"],
    )

    assert evidence.return_code == 0
    assert seen["command"] == ["find", "/tmp/scope", "-maxdepth", "1"]
    assert seen["shell"] is False


def test_registered_action_persists_and_prints_only_redacted_mutating_commands():
    from types import SimpleNamespace

    from klonet_agent.ops.privileged.contracts import ExecutionBinding, PrivilegedStep
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    class Runner:
        on_command = None

        def __call__(self, step):
            self.on_command({
                "action": step.action,
                "argv": ["ss", "-ltn"],
                "cwd": "/srv/app",
                "execution": "subprocess",
                "changes_state": False,
            })
            self.on_command({
                "action": step.action,
                "argv": ["service-cli", "--token", "top-secret", "restart"],
                "cwd": "/srv/app",
                "execution": "subprocess",
                "changes_state": True,
            })
            return SimpleNamespace(
                status="completed", output="environment_changed=true", metadata={},
            )

    progress = []
    executor = PrivilegedCommandExecutor(
        direct_action_runner=Runner(), on_start=progress.append,
    )
    step = PrivilegedStep(
        step_id="restart-extra", title="重启 extra", risk="medium",
        execution_binding=ExecutionBinding(
            kind="registered_action", risk="medium",
            action="restart_screen_component", args={},
        ),
    )

    evidence = executor.execute(step)

    assert len(evidence.commands) == 2
    assert "top-secret" not in str(evidence.commands)
    assert "[REDACTED]" in str(evidence.commands)
    assert any("实际命令" in item for item in progress)
    assert not any("ss -ltn" in item for item in progress)


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


def test_restart_identity_accepts_recovery_from_stale_screen_without_old_pid(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import checkers as module
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence

    monkeypatch.setattr(module, "_pid_cwd", lambda pid: str(tmp_path))
    evidence = ExecutionEvidence(
        return_code=0,
        mutation={
            "kind": "component_restart",
            "component": "master",
            "old_pids": "",
            "new_pids": "202,203",
        },
    )

    result = DefaultCheckerRegistry().run(
        {
            "checker": "component_restart_identity",
            "args": {"component": "master", "project_root": str(tmp_path)},
        },
        evidence=evidence,
    )

    assert result.status == "passed"
    assert "old=none" in result.observed


def test_restart_identity_still_rejects_reused_live_process_identity(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import checkers as module
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence

    monkeypatch.setattr(module, "_pid_cwd", lambda pid: str(tmp_path))
    evidence = ExecutionEvidence(
        return_code=0,
        mutation={
            "kind": "component_restart",
            "component": "worker",
            "old_pids": "301",
            "new_pids": "301",
        },
    )

    result = DefaultCheckerRegistry().run(
        {
            "checker": "component_restart_identity",
            "args": {"component": "worker", "project_root": str(tmp_path)},
        },
        evidence=evidence,
    )

    assert result.status == "failed"


def test_nginx_checker_retries_permission_failure_with_unprivileged_config_copy(
    tmp_path,
    monkeypatch,
):
    import subprocess

    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    config = tmp_path / "nginx.conf"
    config.write_text(
        "user root;\n"
        "pid /run/nginx.pid;\n"
        "error_log /var/log/nginx/error.log;\n"
        "events {}\n"
        "http { access_log /var/log/nginx/access.log; }\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(list(command))
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr='open() "/run/nginx.pid" failed (13: Permission denied)',
            )
        candidate = Path(command[command.index("-c") + 1]).read_text(
            encoding="utf-8"
        )
        assert "/run/nginx.pid" not in candidate
        assert "/var/log/nginx/error.log" not in candidate
        assert "/var/log/nginx/access.log" not in candidate
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="syntax is ok\ntest is successful",
        )

    monkeypatch.setattr(
        "klonet_agent.ops.privileged.checkers.subprocess.run",
        fake_run,
    )

    result = DefaultCheckerRegistry().run(
        {
            "checker": "nginx_config_valid",
            "args": {"binary": "nginx", "config": str(config)},
        }
    )

    assert result.status == "passed"
    assert len(calls) == 2
    assert "-c" in calls[1]


def test_nginx_checker_accepts_completed_parse_with_only_runtime_permission_error(
    monkeypatch,
):
    import subprocess

    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    calls = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok\n"
                "[emerg] open() \"/run/nginx.pid\" failed "
                "(13: Permission denied)\n"
                "nginx: configuration file /etc/nginx/nginx.conf test failed"
            ),
        )

    monkeypatch.setattr(
        "klonet_agent.ops.privileged.checkers.subprocess.run",
        fake_run,
    )

    result = DefaultCheckerRegistry().run(
        {"checker": "nginx_config_valid", "args": {}}
    )

    assert result.status == "passed"
    assert "runtime access deferred" in result.observed
    assert calls == [["nginx", "-t"]]


def test_nginx_checker_does_not_mask_real_syntax_failure(tmp_path, monkeypatch):
    import subprocess

    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    config = tmp_path / "nginx.conf"
    config.write_text("events {\n", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr='unexpected end of file, expecting "}"',
        )

    monkeypatch.setattr(
        "klonet_agent.ops.privileged.checkers.subprocess.run",
        fake_run,
    )

    result = DefaultCheckerRegistry().run(
        {
            "checker": "nginx_config_valid",
            "args": {"binary": "nginx", "config": str(config)},
        }
    )

    assert result.status == "failed"
    assert len(calls) == 1


def test_http_status_checker_accepts_one_of_multiple_expected_codes(monkeypatch):
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    class Response:
        status = 302

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "klonet_agent.ops.privileged.checkers.open_http_request",
        lambda *args, **kwargs: Response(),
    )

    result = DefaultCheckerRegistry().run(
        {
            "checker": "http_status",
            "args": {"url": "http://127.0.0.1/lht/", "statuses": [200, 302]},
        }
    )

    assert result.status == "passed"


def test_backend_health_checker_requires_http_200_and_code_one(monkeypatch):
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"code": 1}'

    monkeypatch.setattr(
        "klonet_agent.ops.privileged.checkers.open_http_request",
        lambda *args, **kwargs: Response(),
    )

    result = DefaultCheckerRegistry().run({
        "checker": "backend_health",
        "args": {"url": "http://127.0.0.1:45551/server_health/"},
    })

    assert result.status == "passed"
    assert result.observed == "http=200 code=1 transport=direct_loopback"


def test_python_import_checker_uses_requested_interpreter_and_cwd(tmp_path):
    import sys

    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    package = tmp_path / "demo_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "config.py").write_text(
        "class DemoConfig:\n    pass\n",
        encoding="utf-8",
    )
    registry = DefaultCheckerRegistry()

    found = registry.run(
        {
            "checker": "python_import_succeeds",
            "args": {
                "module": "demo_pkg.config.DemoConfig",
                "python_executable": sys.executable,
                "cwd": str(tmp_path),
            },
        }
    )
    missing = registry.run(
        {
            "checker": "python_import_succeeds",
            "args": {
                "module": "missing_pkg.config.DemoConfig",
                "python_executable": sys.executable,
                "cwd": str(tmp_path),
            },
        }
    )

    assert found.status == "passed"
    assert missing.status == "failed"


def test_python_file_syntax_checker_does_not_import_application_dependencies(tmp_path):
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    source = tmp_path / "entry.py"
    source.write_text(
        "import dependency_not_installed_in_agent\nVALUE = 1\n",
        encoding="utf-8",
    )

    result = DefaultCheckerRegistry().run({
        "checker": "python_file_syntax_valid",
        "args": {"path": str(source)},
    })

    assert result.status == "passed"

    source.write_text("def broken(:\n", encoding="utf-8")
    result = DefaultCheckerRegistry().run({
        "checker": "python_file_syntax_valid",
        "args": {"path": str(source)},
    })
    assert result.status == "failed"


def test_python_attribute_checker_compares_grounded_class_value(tmp_path):
    import sys

    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    package = tmp_path / "demo_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "config.py").write_text(
        "class DemoConfig:\n    master_port = 46001\n",
        encoding="utf-8",
    )
    registry = DefaultCheckerRegistry()
    contract = {
        "checker": "python_attribute_equals",
        "args": {
            "module": "demo_pkg.config",
            "attribute": "DemoConfig.master_port",
            "expected": 46001,
            "python_executable": sys.executable,
            "cwd": str(tmp_path),
        },
    }

    assert registry.run(contract).status == "passed"
    contract["args"]["expected"] = 46002
    assert registry.run(contract).status == "failed"


def test_python_attribute_checker_falls_back_to_static_literal_when_import_fails(
    tmp_path,
):
    import sys

    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    package = tmp_path / "demo_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "config.py").write_text(
        "import dependency_that_is_not_installed\n"
        "class DemoConfig:\n"
        "    master_ip = '127.0.0.1'\n",
        encoding="utf-8",
    )
    registry = DefaultCheckerRegistry()
    contract = {
        "checker": "python_attribute_equals",
        "args": {
            "module": "demo_pkg.config",
            "attribute": "DemoConfig.master_ip",
            "expected": "127.0.0.1",
            "python_executable": sys.executable,
            "cwd": str(tmp_path),
        },
    }

    result = registry.run(contract)

    assert result.status == "passed"
    assert result.observed == '"127.0.0.1" (static literal)'


def test_python_attribute_static_fallback_resolves_active_proj_config(tmp_path):
    import sys

    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    package = tmp_path / "demo_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "config.py").write_text(
        "import dependency_that_is_not_installed\n"
        "class WtxConfig:\n"
        "    master_port = 45554\n"
        "PROJ_CONFIG = WtxConfig()\n",
        encoding="utf-8",
    )
    result = DefaultCheckerRegistry().run({
        "checker": "python_attribute_equals",
        "args": {
            "module": "demo_pkg.config",
            "attribute": "PROJ_CONFIG.master_port",
            "expected": 45554,
            "python_executable": sys.executable,
            "cwd": str(tmp_path),
        },
    })

    assert result.status == "passed"
    assert result.observed == "45554 (static literal)"


def test_checker_bug_is_reported_unavailable_instead_of_escaping():
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    registry = DefaultCheckerRegistry()

    def crash(args, evidence):
        del args, evidence
        raise RuntimeError("api_key=secret-value")

    registry._checkers["crash"] = crash
    result = registry.run({"checker": "crash", "args": {}})

    assert result.status == "unavailable"
    assert "RuntimeError" in result.observed
    assert "secret-value" not in result.observed
    assert "[REDACTED]" in result.observed


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
