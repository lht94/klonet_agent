from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile


ENTRY_FILES = (
    "gun.py",
    "master_main.py",
    "celery_worker.py",
    "web_terminal_main.py",
    "worker_gun.py",
    "worker_main.py",
)


def _layout(tmp_path):
    root = tmp_path / "demo_project"
    backend = root / "vemu_uestc"
    mains = backend / "mains"
    config = backend / "vemu_config" / "config.py"
    mains.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    (backend / "__init__.py").write_text("", encoding="utf-8")
    config.write_text(
        (
            "class Demo:\n"
            "    master_port = 5000\n"
            "    worker_port = 5001\n"
            "    web_terminal_port = 5002\n"
            "PROJ_CONFIG = Demo()\n"
        ),
        encoding="utf-8",
    )
    for name in ENTRY_FILES:
        (mains / name).write_text("# entry\n", encoding="utf-8")
    return root, backend, mains


def _step(action, args, *, risk="medium"):
    from klonet_agent.ops.privileged.contracts import PrivilegedStep

    return PrivilegedStep(
        step_id=action,
        title=action,
        action=action,
        args=args,
        risk=risk,
    )


def test_direct_runner_prepares_nested_entries_without_ops_helper(tmp_path):
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    root, _backend, mains = _layout(tmp_path)
    result = DirectPrivilegedActionRunner()(
        _step(
            "prepare_project_files",
            {
                "project_root": str(root),
                "source_root": str(mains),
            },
        )
    )

    assert result.status == "completed"
    assert (root / "gun.py").is_file()
    assert "klonet-agent-op" not in result.output


def test_direct_runner_starts_platform_with_fixed_argv_not_helper(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    root, _backend, mains = _layout(tmp_path)
    for name in ENTRY_FILES:
        (root / name).write_text((mains / name).read_text(), encoding="utf-8")
    python = tmp_path / "python3.8"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    calls = []
    monkeypatch.setattr(
        module,
        "_wait_platform_runtime_ready",
        lambda *_args, **_kwargs: "",
    )

    def command_runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        stdout = "No Sockets found.\n" if argv == ["screen", "-ls"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "start_platform_screens",
            {
                "platform": "demo",
                "project_root": str(root),
                "python_executable": str(python),
            },
        )
    )

    assert result.status == "completed"
    assert len([argv for argv, _ in calls if argv[:2] == ["screen", "-dmS"]]) == 4
    assert all("klonet-agent-op" not in " ".join(argv) for argv, _ in calls)
    assert any(
        argv[:4] == [str(python.resolve()), "-m", "gunicorn", "--check-config"]
        for argv, _ in calls
    )


def test_direct_runner_blocks_configured_port_conflict(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    root, _backend, mains = _layout(tmp_path)
    for name in ENTRY_FILES:
        (root / name).write_text((mains / name).read_text(), encoding="utf-8")
    python = tmp_path / "python3.8"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda program: "/usr/bin/ss" if program == "ss" else None,
    )

    def command_runner(argv, **kwargs):
        if argv == ["screen", "-ls"]:
            return subprocess.CompletedProcess(argv, 1, "", "No Sockets")
        if argv == ["/usr/bin/ss", "-ltn"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                "LISTEN 0 128 0.0.0.0:5001 0.0.0.0:*\n",
                "",
            )
        raise AssertionError("startup must stop before preflight")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "start_platform_screens",
            {
                "platform": "demo",
                "project_root": str(root),
                "python_executable": str(python),
            },
        )
    )

    assert result.status == "blocked"
    assert "runtime_port_already_listening=5001" in result.output







def test_direct_runner_start_platform_is_idempotent_when_target_runtime_is_healthy(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    root, _backend, mains = _layout(tmp_path)
    for name in ENTRY_FILES:
        (root / name).write_text((mains / name).read_text(), encoding="utf-8")
    python = tmp_path / "python3.8"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(module, "_runtime_ports_with_wrong_cwd", lambda *_args, **_kwargs: [])
    calls = []

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["screen", "-ls"]:
            stdout = (
                "There are screens on:\n"
                "\t1.demo_m\t(Detached)\n"
                "\t2.demo_c\t(Detached)\n"
                "\t3.demo_web\t(Detached)\n"
                "\t4.demo_w\t(Detached)\n"
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "start_platform_screens",
            {
                "platform": "demo",
                "project_root": str(root),
                "python_executable": str(python),
            },
        )
    )

    assert result.status == "completed"
    assert "already_running=true" in result.output
    assert not any(argv[:2] == ["screen", "-dmS"] for argv in calls)
def test_runtime_ports_use_web_terminal_entry_binding_over_config(tmp_path):
    from klonet_agent.ops.privileged.action_runner import _configured_runtime_ports

    root, _backend, mains = _layout(tmp_path)
    config = root / "vemu_uestc" / "vemu_config" / "config.py"
    config.write_text(
        (
            "class Demo:\n"
            "    master_port = 45551\n"
            "    worker_port = 45552\n"
            "    web_terminal_port = 5114\n"
            "PROJ_CONFIG = Demo()\n"
        ),
        encoding="utf-8",
    )
    (mains / "web_terminal_main.py").write_text(
        "server = pywsgi.WSGIServer((\n"
        "    '0.0.0.0', 5005), app, handler_class=WebSocketHandler)\n",
        encoding="utf-8",
    )

    assert _configured_runtime_ports(mains) == [45551, 45552, 5005]
def test_direct_runner_cleans_stale_runtime_before_port_check_when_authorized(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult,
        DirectPrivilegedActionRunner,
    )

    root, _backend, mains = _layout(tmp_path)
    for name in ENTRY_FILES:
        (root / name).write_text((mains / name).read_text(), encoding="utf-8")
    python = tmp_path / "python3.8"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    cleanup_called = []
    cleanup_done = {"value": False}
    monkeypatch.setattr(
        module,
        "_wait_platform_runtime_ready",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        module,
        "_cleanup_stale_runtime_owners",
        lambda *_args, **_kwargs: (
            cleanup_called.append(True)
            or cleanup_done.__setitem__("value", True)
            or DirectActionResult("completed", "cleaned_stale=/old/vemu_uestc")
        ),
    )
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda program: "/usr/bin/ss" if program == "ss" else None,
    )
    monkeypatch.setattr(
        module,
        "_listening_ports",
        lambda *_args, **_kwargs: [] if cleanup_done["value"] else [5000],
    )

    def command_runner(argv, **kwargs):
        stdout = "No Sockets found.\n" if argv == ["screen", "-ls"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "start_platform_screens",
            {
                "platform": "demo",
                "project_root": str(root),
                "python_executable": str(python),
                "allow_stale_runtime_cleanup": True,
            },
        )
    )

    assert result.status == "completed"
    assert cleanup_called
    assert "cleaned_stale=/old/vemu_uestc" in result.output

def test_direct_runner_controlled_argv_executes_program_directly(tmp_path):
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    calls = []

    def command_runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="clean\n", stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "run_ops_command",
            {
                "program": "git",
                "argv": ["status"],
                "cwd": str(tmp_path),
            },
            risk="readonly",
        )
    )

    assert result.status == "completed"
    assert calls[0][0] == ["git", "status"]
    assert "klonet-agent-op" not in result.output


def test_direct_runner_privileged_argv_uses_sudo_not_helper(
    tmp_path,
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    calls = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(module, "command_exists", lambda program: True)

    def command_runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "run_ops_command",
            {
                "program": "apt",
                "argv": ["update"],
                "cwd": "",
            },
            risk="high",
        )
    )

    assert result.status == "completed"
    assert calls[0][0] == ["sudo", "apt", "update"]
    assert "klonet-agent-op" not in " ".join(calls[0][0])


def test_direct_runner_never_falls_back_to_helper_for_unsupported_action():
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    result = DirectPrivilegedActionRunner()(
        _step(
            "restart_platform",
            {},
            risk="high",
        )
    )

    assert result.status == "blocked"
    assert "action_not_registered" in result.output
    assert "klonet-agent-op" not in result.output


def test_privileged_executor_defaults_to_direct_backend():
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )
    from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor

    executor = PrivilegedCommandExecutor()

    assert isinstance(executor.action_runner, DirectPrivilegedActionRunner)
    assert executor._legacy_action_runner is None


def test_direct_runner_filesystem_lifecycle(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runner = DirectPrivilegedActionRunner()
    source = tmp_path / "source.txt"
    source.write_text("hello\n", encoding="utf-8")
    destination = tmp_path / "destination"

    created = runner(
        _step("create_directory", {"path": str(destination), "mode": "0750"})
    )
    copied = runner(
        _step(
            "copy_files",
            {"sources": [str(source)], "destination": str(destination)},
        )
    )
    moved_path = destination / "moved.txt"
    moved = runner(
        _step(
            "move_path",
            {
                "source": str(destination / source.name),
                "destination": str(moved_path),
            },
        )
    )
    removed = runner(
        _step("remove_path", {"path": str(moved_path)}, risk="high")
    )

    assert [created.status, copied.status, moved.status, removed.status] == [
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    assert destination.is_dir()
    assert not moved_path.exists()


def test_direct_runner_service_container_and_package_actions_use_bounded_argv(
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    paths = {
        "systemctl": "/usr/bin/systemctl",
        "docker": "/usr/bin/docker",
        "apt-get": "/usr/bin/apt-get",
    }
    monkeypatch.setattr(module.shutil, "which", lambda program: paths.get(program))
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="active\n", stderr="")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    service = runner(
        _step(
            "manage_service",
            {"service": "nginx", "operation": "restart"},
        )
    )
    container = runner(
        _step(
            "manage_container",
            {"engine": "docker", "name": "redis", "operation": "restart"},
        )
    )
    packages = runner(
        _step(
            "install_system_packages",
            {"packages": ["screen", "nginx"], "update": True},
            risk="high",
        )
    )

    assert service.status == container.status == packages.status == "completed"
    assert ["/usr/bin/systemctl", "restart", "nginx"] in calls
    assert ["/usr/bin/systemctl", "is-active", "nginx"] in calls
    assert ["/usr/bin/docker", "restart", "redis"] in calls
    assert [
        "/usr/bin/apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        "screen",
        "nginx",
    ] in calls


def test_direct_runner_python_packages_permissions_and_git_use_structured_argv(
    tmp_path,
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    python = tmp_path / "python3"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    target = tmp_path / "target.txt"
    target.write_text("x\n", encoding="utf-8")
    repository = tmp_path / "repo"
    repository.mkdir()
    calls = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    python_result = runner(
        _step(
            "install_python_packages",
            {
                "python_executable": str(python),
                "operation": "install",
                "packages": ["gunicorn==20.1.0"],
            },
            risk="high",
        )
    )
    permissions = runner(
        _step(
            "manage_file_permissions",
            {"path": str(target), "mode": "0640"},
            risk="high",
        )
    )
    git_result = runner(
        _step(
            "git_operation",
            {"repository": str(repository), "operation": "status"},
        )
    )

    assert python_result.status == permissions.status == git_result.status == "completed"
    assert [str(python.resolve()), "-m", "pip", "install", "gunicorn==20.1.0"] in [
        argv for argv, _kwargs in calls
    ]
    assert ["chmod", "0640", str(target.resolve())] in [
        argv for argv, _kwargs in calls
    ]
    assert any(argv == ["git", "status", "--short", "--branch"] for argv, _ in calls)


def test_direct_runner_process_action_verifies_pid_identity(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    monkeypatch.setattr(module, "_proc_cmdline", lambda pid: "python worker_main.py")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    blocked = runner(
        _step(
            "manage_process",
            {"pid": 12345, "signal": "TERM", "expected_command": "master_main"},
            risk="high",
        )
    )
    completed = runner(
        _step(
            "manage_process",
            {"pid": 12345, "signal": "TERM", "expected_command": "worker_main"},
            risk="high",
        )
    )

    assert blocked.status == "blocked"
    assert completed.status == "completed"
    assert calls == [["kill", "-TERM", "12345"]]


def test_direct_runner_process_action_accepts_posix_signal_names(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    monkeypatch.setattr(module, "_proc_cmdline", lambda pid: "python worker_main.py")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    completed = runner(
        _step(
            "manage_process",
            {"pid": "12345", "signal": "SIGTERM", "expected_command": "worker_main"},
            risk="high",
        )
    )

    assert completed.status == "completed"
    assert calls == [["kill", "-TERM", "12345"]]


def test_direct_runner_process_action_kills_same_user_without_sudo(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    monkeypatch.setattr(module, "_proc_cmdline", lambda pid: "python worker_main.py")
    monkeypatch.setattr(module, "_proc_uid", lambda pid: 1000)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    completed = runner(
        _step(
            "manage_process",
            {"pid": "12345", "signal": "TERM", "expected_command": "worker_main"},
            risk="high",
        )
    )

    assert completed.status == "completed"
    assert calls == [["kill", "-TERM", "12345"]]


def test_direct_runner_process_action_uses_sudo_for_other_user(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module

    monkeypatch.setattr(module, "_proc_uid", lambda pid: 2000)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)

    assert module._kill_argv_for_pid(12345, "TERM") == [
        "sudo",
        "kill",
        "-TERM",
        "12345",
    ]


def test_direct_runner_process_action_can_target_process_group(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    monkeypatch.setattr(module, "_proc_cmdline", lambda pid: "python gun.py master_main:flask_app")
    monkeypatch.setattr(module, "_proc_uid", lambda pid: 1000)
    monkeypatch.setattr(module, "_proc_pgid", lambda pid: 12345)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    completed = runner(
        _step(
            "manage_process",
            {
                "pid": "12345",
                "signal": "SIGKILL",
                "scope": "process_group",
                "expected_command": "master_main",
            },
            risk="high",
        )
    )

    assert completed.status == "completed"
    assert calls == [["kill", "-KILL", "-12345"]]


def test_direct_runner_stops_klonet_runtime_instance_by_cwd(monkeypatch, tmp_path):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runtime = tmp_path / "vemu_uestc"
    (runtime / "vemu_config").mkdir(parents=True)
    calls = []
    process_snapshots = [
        [
            {"pid": 1001, "pgid": 1001, "cmdline": "python -m gunicorn -c gun.py master_main:flask_app", "cwd": str(runtime)},
            {"pid": 1002, "pgid": 1001, "cmdline": "python -m gunicorn -c gun.py master_main:flask_app", "cwd": str(runtime)},
            {"pid": 1003, "pgid": 1003, "cmdline": "python web_terminal_main.py", "cwd": str(runtime)},
        ],
        [],
    ]

    def fake_processes(path):
        return process_snapshots.pop(0) if process_snapshots else []

    monkeypatch.setattr(module, "_klonet_runtime_processes", fake_processes)
    monkeypatch.setattr(module, "_ports_currently_listening", lambda runner, ports: [])
    monkeypatch.setattr(module, "_proc_uid", lambda pid: 1000)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "stop_klonet_runtime_instance",
            {"runtime_cwd": str(runtime), "ports": [45551, 45552, 5005]},
            risk="high",
        )
    )

    assert result.status == "completed"
    assert calls == [["kill", "-TERM", "-1001"], ["kill", "-TERM", "-1003"]]
    assert "runtime_cwd=" in result.output
    assert "ports=45551,45552,5005" in result.output


def test_direct_runner_exact_text_replacement_creates_backup(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text(
        "class RedisConfig:\n    redis_port = 8368\n",
        encoding="utf-8",
    )

    result = DirectPrivilegedActionRunner()(
        _step(
            "replace_text_in_file",
            {
                "path": str(config),
                "old_text": "redis_port = 8368",
                "new_text": "redis_port = 9368",
            },
        )
    )

    assert result.status == "completed"
    assert "redis_port = 9368" in config.read_text(encoding="utf-8")
    backups = list(tmp_path.glob("config.py.klonet-agent.bak.*"))
    assert len(backups) == 1
    assert "redis_port = 8368" in backups[0].read_text(encoding="utf-8")


def test_direct_runner_exact_text_replacement_requires_unique_match(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text("port = 1\nport = 1\n", encoding="utf-8")

    result = DirectPrivilegedActionRunner()(
        _step(
            "replace_text_in_file",
            {
                "path": str(config),
                "old_text": "port = 1",
                "new_text": "port = 2",
            },
        )
    )

    assert result.status == "blocked"
    assert "replacement_match_count=2" in result.output
    assert config.read_text(encoding="utf-8") == "port = 1\nport = 1\n"


def test_direct_runner_inserts_text_before_unique_anchor_with_backup(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text(
        "class WtxConfig:\n    master_port = 45551\n\n"
        "PROJ_CONFIG = WtxConfig()\n",
        encoding="utf-8",
    )
    inserted = "class LhtConfig(WtxConfig):\n    master_port = 5200\n\n"

    result = DirectPrivilegedActionRunner()(
        _step(
            "insert_text_before_anchor",
            {
                "path": str(config),
                "anchor": "PROJ_CONFIG = WtxConfig()",
                "content": inserted,
            },
        )
    )

    updated = config.read_text(encoding="utf-8")
    assert result.status == "completed"
    assert updated.index("class LhtConfig") < updated.index("PROJ_CONFIG")
    assert updated.count("PROJ_CONFIG = WtxConfig()") == 1
    backups = list(tmp_path.glob("config.py.klonet-agent.bak.*"))
    assert len(backups) == 1
    assert "class LhtConfig" not in backups[0].read_text(encoding="utf-8")


def test_direct_runner_text_insertion_requires_unique_anchor(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text("ANCHOR\nANCHOR\n", encoding="utf-8")

    result = DirectPrivilegedActionRunner()(
        _step(
            "insert_text_before_anchor",
            {
                "path": str(config),
                "anchor": "ANCHOR",
                "content": "class LhtConfig:\n    pass",
            },
        )
    )

    assert result.status == "blocked"
    assert "insertion_anchor_match_count=2" in result.output
    assert config.read_text(encoding="utf-8") == "ANCHOR\nANCHOR\n"


def test_direct_runner_extracts_safe_archive_and_refuses_traversal(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    safe = tmp_path / "safe.tar"
    source = tmp_path / "README.md"
    source.write_text("klonet\n", encoding="utf-8")
    with tarfile.open(safe, "w") as archive:
        archive.add(source, arcname="bundle/README.md")
    unsafe = tmp_path / "unsafe.tar"
    with tarfile.open(unsafe, "w") as archive:
        member = tarfile.TarInfo("../escape.txt")
        payload = b"escape\n"
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    runner = DirectPrivilegedActionRunner()
    destination = tmp_path / "unpacked"
    completed = runner(
        _step(
            "extract_archive",
            {
                "archive_path": str(safe),
                "destination_dir": str(destination),
            },
        )
    )
    blocked = runner(
        _step(
            "extract_archive",
            {
                "archive_path": str(unsafe),
                "destination_dir": str(tmp_path / "unsafe-output"),
            },
        )
    )

    assert completed.status == "completed"
    assert (destination / "bundle" / "README.md").read_text() == "klonet\n"
    assert blocked.status == "blocked"
    assert "archive_path_traversal" in blocked.output
    assert not (tmp_path.parent / "escape.txt").exists()


def test_direct_runner_merges_json_with_backup_and_validation(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "daemon.json"
    config.write_text(
        json.dumps({"log-driver": "json-file", "features": {"buildkit": False}}),
        encoding="utf-8",
    )
    result = DirectPrivilegedActionRunner()(
        _step(
            "merge_json_file",
            {
                "path": str(config),
                "patch": {"features": {"buildkit": True}},
            },
            risk="high",
        )
    )

    assert result.status == "completed"
    assert json.loads(config.read_text())["features"]["buildkit"] is True
    assert len(list(tmp_path.glob("daemon.json.klonet-agent.bak.*"))) == 1


def test_direct_runner_starts_configured_redis_and_checks_exact_port(
    tmp_path,
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    binary = tmp_path / "redis-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    config = tmp_path / "redis.conf"
    config.write_text("bind 127.0.0.1\nport 8368\n", encoding="utf-8")
    listening = iter((False, True))
    calls = []
    monkeypatch.setattr(module, "_tcp_listening", lambda *_args, **_kwargs: next(listening))
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "start_redis_instance",
            {
                "binary": str(binary),
                "config_path": str(config),
                "expected_port": 8368,
            },
        )
    )

    assert result.status == "completed"
    assert calls == [
        [str(binary.resolve()), str(config.resolve()), "--daemonize", "yes"]
    ]


def test_direct_runner_reviewed_script_requires_matching_sha256(
    tmp_path,
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    script = tmp_path / "repair.sh"
    script.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    calls = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    blocked = runner(
        _step(
            "run_reviewed_script",
            {
                "script_path": str(script),
                "cwd": str(tmp_path),
                "sha256": "0" * 64,
            },
            risk="high",
        )
    )
    completed = runner(
        _step(
            "run_reviewed_script",
            {
                "script_path": str(script),
                "cwd": str(tmp_path),
                "sha256": digest,
                "argv": ["NORMAL"],
            },
            risk="high",
        )
    )

    assert blocked.status == "blocked"
    assert "hash_mismatch" in blocked.output
    assert completed.status == "completed"
    assert calls == [["bash", str(script.resolve()), "NORMAL"]]


def test_direct_runner_container_network_virtualization_actions_are_bounded(
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    paths = {
        "docker": "/usr/bin/docker",
        "ip": "/usr/sbin/ip",
        "ovs-vsctl": "/usr/bin/ovs-vsctl",
        "virsh": "/usr/bin/virsh",
    }
    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda program: paths.get(program))
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    results = [
        runner(
            _step(
                "manage_container",
                {
                    "name": "redis-celery",
                    "operation": "set_restart_policy",
                    "restart_policy": "always",
                },
                risk="high",
            )
        ),
        runner(
            _step(
                "manage_docker_network",
                {
                    "network": "klonet-overlay",
                    "operation": "create",
                    "driver": "overlay",
                },
                risk="high",
            )
        ),
        runner(
            _step(
                "manage_network_link",
                {"name": "tap-demo", "operation": "down"},
                risk="high",
            )
        ),
        runner(
            _step(
                "manage_ovs_resource",
                {
                    "resource_type": "bridge",
                    "name": "br-demo",
                    "operation": "add",
                },
                risk="high",
            )
        ),
        runner(
            _step(
                "manage_libvirt_domain",
                {"domain": "vm-demo", "operation": "start"},
                risk="high",
            )
        ),
    ]

    assert all(result.status == "completed" for result in results)
    assert ["/usr/bin/docker", "update", "--restart", "always", "redis-celery"] in calls
    assert [
        "/usr/bin/docker",
        "network",
        "create",
        "--driver",
        "overlay",
        "--attachable",
        "klonet-overlay",
    ] in calls
    assert ["/usr/sbin/ip", "link", "set", "dev", "tap-demo", "down"] in calls
    assert ["/usr/bin/ovs-vsctl", "--may-exist", "add-br", "br-demo"] in calls
    assert ["/usr/bin/virsh", "start", "vm-demo"] in calls
