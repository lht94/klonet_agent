from __future__ import annotations

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
    assert calls == [command]


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


def test_http_status_checker_accepts_one_of_multiple_expected_codes(monkeypatch):
    from klonet_agent.ops.privileged.checkers import DefaultCheckerRegistry

    class Response:
        status = 302

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "klonet_agent.ops.privileged.checkers.urllib.request.urlopen",
        lambda *args, **kwargs: Response(),
    )

    result = DefaultCheckerRegistry().run(
        {
            "checker": "http_status",
            "args": {"url": "http://127.0.0.1/lht/", "statuses": [200, 302]},
        }
    )

    assert result.status == "passed"


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
