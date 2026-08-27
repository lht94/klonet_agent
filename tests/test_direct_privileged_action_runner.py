from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path


def _write_runtime_entries(root: Path) -> None:
    for name in (
        "gun.py", "master_main.py", "celery_worker.py",
        "web_terminal_main.py", "worker_gun.py", "worker_main.py",
    ):
        (root / name).write_text("# entry\n", encoding="utf-8")


def test_docker_uses_direct_group_socket_access_without_sudo(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module

    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        module.os,
        "access",
        lambda path, mode: path == "/var/run/docker.sock"
        and mode == (module.os.R_OK | module.os.W_OK),
    )

    assert module._sudo_if_needed(["/usr/bin/docker", "ps"]) == [
        "/usr/bin/docker",
        "ps",
    ]
    assert module._sudo_if_needed(
        ["/usr/bin/systemctl", "restart", "nginx"]
    )[0] == "sudo"


def test_runtime_python_env_aliases_hardcoded_package_to_isolated_clone(
    tmp_path, monkeypatch
):
    from klonet_agent.ops.privileged import action_runner as module

    instance = tmp_path / "renamed_instance"
    mains = instance / "mains"
    config = instance / "vemu_config"
    mains.mkdir(parents=True)
    config.mkdir()
    (instance / "__init__.py").write_text("", encoding="utf-8")
    (mains / "gun.py").write_text(
        "from original_package.vemu_config.config import PROJ_CONFIG\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))

    env = module._runtime_python_env(mains)

    alias_root = Path(env["PYTHONPATH"].split(module.os.pathsep)[0])
    alias = alias_root / "original_package"
    assert alias.is_symlink()
    assert alias.resolve() == instance.resolve()
    assert alias_root.stat().st_mode & 0o777 == 0o755


def test_screen_component_waits_for_its_application_port(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    python = tmp_path / "python3.8"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    waited = []
    monkeypatch.setattr(module, "_python_executable", lambda _args: str(python))
    monkeypatch.setattr(module, "_runtime_python_env", lambda _root: {})
    monkeypatch.setattr(
        module,
        "_wait_tcp_listening",
        lambda host, port, timeout: waited.append((host, port, timeout)) or True,
    )
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, "", ""
        )
    )

    result = runner(
        _step(
            "start_screen_component",
            {
                "platform": "v4e2e",
                "component": "master",
                "screen_session": "v4e2e_m",
                "project_root": str(tmp_path),
                "master_port": "47001",
            },
            risk="medium",
        )
    )

    assert result.status == "completed", result.output
    assert waited == [("127.0.0.1", 47001, 20.0)]


def test_screen_component_uses_frozen_runtime_uid_and_python(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    python = tmp_path / "python3.8"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(module, "_runtime_python_env", lambda _root: {"PYTHONPATH": "/runtime/alias"})
    monkeypatch.setattr(module, "_wait_tcp_listening", lambda *_args, **_kwargs: True)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )

    result = runner(_step(
        "start_screen_component",
        {
            "platform": "102",
            "component": "master",
            "screen_session": "102_m",
            "project_root": str(tmp_path),
            "master_port": 27694,
            "run_as_uid": 997,
            "python_executable": str(python),
        },
        risk="medium",
    ))

    assert result.status == "completed", result.output
    runtime_calls = [call for call in calls if call[:4] == ["sudo", "-n", "-u", "#997"]]
    assert len(runtime_calls) >= 3
    assert any(str(python) in call for call in runtime_calls)
    assert any("PYTHONPATH=/runtime/alias" in call for call in runtime_calls)


def test_screen_component_keeps_interactive_shell_after_foreground_service_stops(
    tmp_path, monkeypatch
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    python = tmp_path / "python3.8"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(module, "_runtime_python_env", lambda _root: {
        "PYTHONPATH": "/tmp/runtime-alias",
    })
    monkeypatch.setattr(module, "_wait_tcp_listening", lambda *_args, **_kwargs: True)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )

    result = runner(_step(
        "start_screen_component",
        {
            "platform": "v4e2e",
            "component": "worker",
            "screen_session": "v4e2e_w",
            "project_root": str(tmp_path),
            "worker_port": 47002,
            "python_executable": str(python),
        },
        risk="medium",
    ))

    assert result.status == "completed", result.output
    screen_call = next(call for call in calls if "screen" in call and "-dmS" in call)
    assert "--rcfile" in screen_call
    assert "-i" in screen_call
    assert "-lc" not in screen_call
    assert not any("exec " in str(item) for item in screen_call)
    rc_write = next(call for call in calls if "p.write_text" in " ".join(call))
    rc_body = rc_write[-1]
    assert "klonet_start()" in rc_body
    assert "Ctrl-C" in rc_body
    assert "exec " not in rc_body
    assert "python3.8 -m gunicorn -c worker_gun.py worker_main:flask_app" in rc_body
    assert "session_mode=interactive_foreground" in result.output


def test_start_component_requires_entries_in_instance_root(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    mains = tmp_path / "mains"
    mains.mkdir()
    _write_runtime_entries(mains)
    runner = DirectPrivilegedActionRunner()

    result = runner(_step(
        "start_screen_component",
        {
            "platform": "v4e2e",
            "component": "master",
            "screen_session": "v4e2e_m",
            "project_root": str(tmp_path),
        },
        risk="medium",
    ))

    assert result.status == "blocked"
    assert "runtime_entries_not_prepared" in result.output


def test_restart_preserves_existing_interactive_screen_shell(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    python = tmp_path / "python3.8"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(module, "_wait_tcp_released", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "_wait_tcp_listening", lambda *_args, **_kwargs: True)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(
        runner,
        "_screen_session_targets",
        lambda session, run_as_uid="": ["123.v4e2e_w"],
    )
    monkeypatch.setattr(
        module, "_screen_owner_targets_for_pids",
        lambda _pids: (["123.v4e2e_w"], True),
    )
    listener_snapshots = iter([[101], [202]])
    monkeypatch.setattr(
        module,
        "_listener_pids_for_port",
        lambda _port: next(listener_snapshots),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "v4e2e",
            "component": "worker",
            "screen_session": "v4e2e_w",
            "project_root": str(tmp_path),
            "worker_port": 47002,
            "python_executable": str(python),
        },
        risk="medium",
    ))

    assert result.status == "completed", result.output
    stuff = [call for call in calls if "stuff" in call]
    assert stuff[0][-1] == "\x03"
    assert stuff[1][-1] == "klonet_start\n"
    assert not any("quit" in call for call in calls)
    assert "session_preserved=true" in result.output


def test_restart_preserves_target_screen_but_stops_proven_orphan_same_role(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["123.test_c"],
    )

    def ownership(pids):
        return (["123.test_c"], True) if 100 in pids else ([], True)

    monkeypatch.setattr(module, "_screen_owner_targets_for_pids", ownership)
    monkeypatch.setattr(module, "_component_pids", lambda *_args: [100, 101])
    monkeypatch.setattr(
        module, "_wait_component_new_pids", lambda *_args, **_kwargs: [202],
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))
    stopped = []
    monkeypatch.setattr(
        runner, "_stop_frozen_component_groups",
        lambda root, component, pids, step: stopped.append(
            (root, component, list(pids))
        ) or DirectActionResult(
            "completed", "orphan component groups stopped",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "test", "component": "celery",
            "screen_session": "test_c", "project_root": str(tmp_path),
        },
        risk="high",
    ))

    assert result.status == "completed", result.output
    assert stopped == [(tmp_path.resolve(), "celery", [101])]
    assert not any("quit" in call for call in calls)
    stuff = [call for call in calls if "stuff" in call]
    assert [call[-1] for call in stuff] == ["\x03", "klonet_start\n"]
    assert "cleaned_orphan_pids=101" in result.output


def test_screen_adoption_cleans_frozen_orphan_without_restarting_managed_screen(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["123.test_c"],
    )
    monkeypatch.setattr(module, "_component_pids", lambda *_args: [100, 101])
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))

    def ownership(pids):
        return (["123.test_c"], True) if 100 in pids else ([], True)

    monkeypatch.setattr(module, "_screen_owner_targets_for_pids", ownership)
    stopped = []
    monkeypatch.setattr(
        runner, "_stop_frozen_component_groups",
        lambda root, component, pids, step: stopped.append(list(pids))
        or DirectActionResult("completed", "orphan component groups stopped"),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "test", "component": "celery",
            "screen_session": "test_c", "project_root": str(tmp_path),
            "lifecycle_mode": "screen_adoption", "orphan_pids": [101],
        },
        risk="high",
    ))

    assert result.status == "completed", result.output
    assert stopped == [[101]]
    assert "adoption_only=true" in result.output
    assert not any("stuff" in call or "quit" in call for call in calls)


def test_restart_replaces_both_screens_when_same_role_has_multiple_owners(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["123.test_c"],
    )
    monkeypatch.setattr(
        runner, "_existing_screen_sessions", lambda run_as_uid="": set(),
    )
    monkeypatch.setattr(
        module, "_screen_owner_targets_for_pids",
        lambda _pids: (["123.test_c", "456.legacy_c"], True),
    )
    component_states = iter([[100, 101], [202]])
    monkeypatch.setattr(
        module, "_component_pids", lambda *_args: next(component_states),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(
        runner, "_stop_frozen_component_groups",
        lambda *_args: DirectActionResult(
            "completed", "frozen role stopped",
        ),
    )
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: DirectActionResult(
            "completed", "action=start_screen_component environment_changed=true",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "test", "component": "celery",
            "screen_session": "test_c", "project_root": str(tmp_path),
        },
        risk="high",
    ))

    assert result.status == "completed", result.output
    assert ["screen", "-S", "123.test_c", "-X", "quit"] in calls
    assert ["screen", "-S", "456.legacy_c", "-X", "quit"] in calls
    assert not any("stuff" in call for call in calls)
    assert "restart_state=runtime_screen_migrated" in result.output


def test_restart_cleans_frozen_runtime_after_legacy_screen_quit(
    tmp_path, monkeypatch,
):
    """A non-interactive legacy Screen may leave its foreground role alive."""

    from klonet_agent.ops.privileged import action_runner as module
    import sys
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    calls = []

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            1 if argv[:2] == ["test", "-f"] else 0,
            "",
            "",
        )

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["123.vemu_uestc_m"],
    )
    monkeypatch.setattr(
        runner, "_existing_screen_sessions", lambda run_as_uid="": set(),
    )
    monkeypatch.setattr(
        module, "_screen_owner_targets_for_pids",
        lambda _pids: (["123.vemu_uestc_m"], True),
    )
    listeners = iter([[100], [200]])
    monkeypatch.setattr(
        module, "_listener_pids_for_port", lambda _port: next(listeners),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(module, "_wait_tcp_released", lambda *_args, **_kwargs: True)
    cleaned = []
    monkeypatch.setattr(
        runner, "_stop_frozen_component_groups",
        lambda root, component, pids, step: cleaned.append(
            (root, component, list(pids))
        ) or DirectActionResult(
            "completed", "frozen role stopped",
        ),
    )
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: DirectActionResult(
            "completed", "action=start_screen_component environment_changed=true",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "vemu_uestc", "component": "master",
            "screen_session": "vemu_uestc_m", "project_root": str(tmp_path),
            "master_port": 45551, "python_executable": sys.executable,
        },
        risk="high",
    ))

    assert result.status == "completed", result.output
    assert ["screen", "-S", "123.vemu_uestc_m", "-X", "quit"] in calls
    assert cleaned == [(tmp_path.resolve(), "master", [100])]
    assert "old_pids=100" in result.output
    assert "new_pids=200" in result.output


def test_restart_orphan_cleanup_stops_only_frozen_same_role_group(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    processes = [{
        "pid": 101, "pgid": 101,
        "cmdline": "python -m celery -A celery_worker.celery worker",
        "cwd": str(tmp_path),
    }]
    monkeypatch.setattr(
        module, "_klonet_runtime_processes",
        lambda root, **kwargs: list(processes),
    )
    monkeypatch.setattr(
        module, "_component_pids",
        lambda root, component: [101] if processes else [],
    )
    monkeypatch.setattr(module, "_proc_uid", lambda _pid: 1000)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)
    calls = []

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        processes.clear()
        return subprocess.CompletedProcess(argv, 0, "", "")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    result = runner._stop_frozen_component_groups(
        tmp_path, "celery", [101],
        _step("restart_screen_component", {}, risk="high"),
    )

    assert result.status == "completed", result.output
    assert calls == [["kill", "-TERM", "-101"]]
    assert "component=celery" in result.output
    assert "pids=101" in result.output


def test_orphan_cleanup_never_scans_unrelated_runtime_groups(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    processes = [{
        "pid": 121, "pgid": 121,
        "cmdline": "python web_terminal_main.py",
        "cwd": str(tmp_path),
    }]

    def scoped_processes(root, **kwargs):
        calls.append(kwargs.get("expected_pid"))
        assert kwargs.get("expected_pid") == 121
        return list(processes)

    monkeypatch.setattr(module, "_klonet_runtime_processes", scoped_processes)
    monkeypatch.setattr(module, "_component_pids", lambda *_args: [])
    monkeypatch.setattr(module, "_proc_uid", lambda _pid: 1000)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)

    def command_runner(argv, **kwargs):
        assert argv[:1] != ["sudo"]
        processes.clear()
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = DirectPrivilegedActionRunner(
        command_runner=command_runner,
    )._stop_frozen_component_groups(
        tmp_path, "web_terminal", [121],
        _step("restart_screen_component", {}, risk="high"),
    )

    assert result.status == "completed", result.output
    assert calls and set(calls) == {121}


def test_runtime_role_recognizes_web_terminal_script_and_module_forms():
    from klonet_agent.ops.privileged.action_runner import (
        _is_klonet_runtime_command, _klonet_component_for_command,
    )

    commands = [
        "python web_terminal_main.py",
        "python /srv/klonet/web_terminal_main.py --port 43444",
        "python -m vemu_uestc.mains.web_terminal_main",
    ]
    assert all(
        _klonet_component_for_command(command) == "web_terminal"
        for command in commands
    )
    assert all(_is_klonet_runtime_command(command) for command in commands)
    assert _klonet_component_for_command(
        "python -m tools.web_terminal_main_backup"
    ) == ""
    assert not _is_klonet_runtime_command("python worker_helper.py")


def test_restart_orphan_cleanup_rejects_mixed_role_process_group(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    processes = [
        {
            "pid": 101, "pgid": 101,
            "cmdline": "python -m celery -A celery_worker.celery worker",
            "cwd": str(tmp_path),
        },
        {
            "pid": 102, "pgid": 101,
            "cmdline": "gunicorn master_main:flask_app",
            "cwd": str(tmp_path),
        },
    ]
    monkeypatch.setattr(
        module, "_klonet_runtime_processes",
        lambda root, **kwargs: list(processes),
    )
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(list(argv))
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )

    result = runner._stop_frozen_component_groups(
        tmp_path, "celery", [101],
        _step("restart_screen_component", {}, risk="high"),
    )

    assert result.status == "blocked"
    assert "process_group_mixed_roles" in result.output
    assert calls == []


def test_restart_replaces_stale_screen_when_component_listener_is_missing(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["123.v4e2e_m"],
    )
    monkeypatch.setattr(
        runner, "_existing_screen_sessions",
        lambda run_as_uid="": set(),
    )
    listener_snapshots = iter([[], [202]])
    monkeypatch.setattr(
        module, "_listener_pids_for_port",
        lambda _port: next(listener_snapshots),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: DirectActionResult(
            "completed", "action=start_screen_component environment_changed=true",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "v4e2e", "component": "master",
            "screen_session": "v4e2e_m", "project_root": str(tmp_path),
            "master_port": 47001,
        },
        risk="medium",
    ))

    assert result.status == "completed", result.output
    assert ["screen", "-S", "123.v4e2e_m", "-X", "quit"] in calls
    assert "restart_state=stale_screen_replaced" in result.output


def test_restart_migrates_runtime_owned_by_different_screen_and_removes_empty_shell(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["101.legacy_m"],
    )
    monkeypatch.setattr(
        runner, "_existing_screen_sessions",
        lambda run_as_uid="": set(),
    )
    monkeypatch.setattr(
        module, "_screen_owner_targets_for_pids",
        lambda _pids: (["355.live_m"], True),
    )
    listener_snapshots = iter([[500], [600]])
    monkeypatch.setattr(
        module, "_listener_pids_for_port",
        lambda _port: next(listener_snapshots),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(module, "_wait_tcp_released", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner, "_stop_frozen_component_groups",
        lambda *_args: DirectActionResult(
            "completed", "frozen role stopped",
        ),
    )
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: DirectActionResult(
            "completed", "action=start_screen_component environment_changed=true",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "legacy", "component": "master",
            "screen_session": "legacy_m", "project_root": str(tmp_path),
            "master_port": 47001,
        },
        risk="high",
    ))

    assert result.status == "completed", result.output
    assert ["screen", "-S", "101.legacy_m", "-X", "quit"] in calls
    assert ["screen", "-S", "355.live_m", "-X", "quit"] in calls
    assert not any("stuff" in call for call in calls)
    assert "restart_state=runtime_screen_migrated" in result.output


def test_restart_creates_missing_screen_when_component_is_not_running(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets", lambda *_args, **_kwargs: [],
    )
    listener_snapshots = iter([[], [303]])
    monkeypatch.setattr(
        module, "_listener_pids_for_port",
        lambda _port: next(listener_snapshots),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: DirectActionResult(
            "completed", "action=start_screen_component environment_changed=true",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "test", "component": "worker",
            "screen_session": "test_w", "project_root": str(tmp_path),
            "worker_port": 45555,
        },
        risk="medium",
    ))

    assert result.status == "completed", result.output
    assert not any("quit" in call for call in calls)
    assert "restart_state=missing_screen_created" in result.output


def test_restart_rehomes_exact_root_listener_when_screen_ancestry_is_hidden(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, "", "",
        )
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets", lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        module, "_screen_owner_targets_for_pids", lambda _pids: ([], False),
    )
    listener_snapshots = iter([[501], [601]])
    monkeypatch.setattr(
        module, "_listener_pids_for_port", lambda _port: next(listener_snapshots),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(module, "_wait_tcp_released", lambda *_a, **_k: True)
    stopped = []
    monkeypatch.setattr(
        runner, "_stop_frozen_component_groups",
        lambda root, component, pids, step: stopped.append(list(pids))
        or DirectActionResult("completed", "stopped exact frozen group"),
    )
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: DirectActionResult(
            "completed", "action=start_screen_component environment_changed=true",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "klonet", "component": "web_terminal",
            "screen_session": "klonet_web", "project_root": str(tmp_path),
            "web_terminal_port": 43444,
        },
        risk="medium",
    ))

    assert result.status == "completed", result.output
    assert stopped == [[501]]
    assert "old_pids=501" in result.output


def test_restart_custom_component_rejects_missing_preflight_before_cleanup(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    runner = DirectPrivilegedActionRunner()
    cleanup_attempts = []
    monkeypatch.setattr(
        runner,
        "_clean_dead_screen_session",
        lambda *_args: cleanup_attempts.append(True) or ([], ""),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "test",
            "component": "data_server",
            "screen_suffix": "data_server",
            "screen_session": "test_data_server",
            "project_root": str(tmp_path),
            "command_argv": [sys.executable, "-m", "data_server"],
        },
        risk="medium",
    ))

    assert result.status == "blocked"
    assert "component_preflight_missing" in result.output
    assert "environment_changed=false" in result.output
    assert cleanup_attempts == []


def test_restart_reports_changed_when_start_fails_after_orphan_cleanup(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    events = []

    def command_runner(argv, **kwargs):
        events.append(("command", list(argv)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    monkeypatch.setattr(
        runner, "_clean_dead_screen_session", lambda *_args: ([], ""),
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets", lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        module, "_component_pids", lambda *_args: [501],
    )
    monkeypatch.setattr(
        module, "_screen_owner_targets_for_pids", lambda _pids: ([], False),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))

    def stop_groups(*_args):
        events.append(("stop", [501]))
        return DirectActionResult(
            "completed", "stopped environment_changed=true",
        )

    monkeypatch.setattr(runner, "_stop_frozen_component_groups", stop_groups)
    monkeypatch.setattr(
        runner,
        "_start_one_component",
        lambda *_args: DirectActionResult(
            "failed",
            "screen_start_failed environment_changed=false",
            "inspect_runtime",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "test",
            "component": "data_server",
            "screen_suffix": "data_server",
            "screen_session": "test_data_server",
            "project_root": str(tmp_path),
            "python": sys.executable,
            "command_argv": [sys.executable, "-m", "data_server"],
            "preflight_argv": [sys.executable, "-c", "import sys"],
        },
        risk="medium",
    ))

    assert result.status == "failed"
    assert result.output.count("environment_changed=") == 1
    assert "environment_changed=true" in result.output
    assert [kind for kind, _value in events].index("command") < [
        kind for kind, _value in events
    ].index("stop")


def test_restart_does_not_create_parallel_screen_when_stale_cleanup_fails(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1 if "quit" in argv else 0, "", "cannot quit" if "quit" in argv else "",
        )
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["321.test_w"],
    )
    monkeypatch.setattr(module, "_listener_pids_for_port", lambda _port: [])
    started = []
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: started.append(True),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "test", "component": "worker",
            "screen_session": "test_w", "project_root": str(tmp_path),
            "worker_port": 45555,
        },
        risk="medium",
    ))

    assert result.status == "failed"
    assert "screen_restart_cleanup_failed" in result.output
    assert started == []


def test_restart_wipes_dead_screen_then_starts_same_session(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    _write_runtime_entries(tmp_path)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    dead_states = iter([["2031135.v4e2e_c"], []])
    monkeypatch.setattr(
        runner, "_dead_screen_session_targets",
        lambda *_args, **_kwargs: next(dead_states, []),
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets", lambda *_args, **_kwargs: [],
    )
    component_states = iter([[], [202]])
    monkeypatch.setattr(
        module, "_component_pids",
        lambda *_args, **_kwargs: next(component_states),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: DirectActionResult(
            "completed", "action=start_screen_component environment_changed=true",
        ),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "v4e2e", "component": "celery",
            "screen_session": "v4e2e_c", "project_root": str(tmp_path),
        },
        risk="medium",
    ))

    assert result.status == "completed", result.output
    assert ["screen", "-wipe", "2031135.v4e2e_c"] in calls
    assert not any("quit" in call for call in calls)
    assert "restart_state=dead_screen_replaced" in result.output


def test_restart_refuses_parallel_session_when_dead_socket_cannot_be_cleaned(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            1 if "-wipe" in argv else 0,
            "",
            "socket is still owned" if "-wipe" in argv else "",
        )
    )
    monkeypatch.setattr(
        runner, "_dead_screen_session_targets",
        lambda *_args, **_kwargs: ["2031135.v4e2e_c"],
    )
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    started = []
    monkeypatch.setattr(
        runner, "_start_one_component",
        lambda *_args: started.append(True),
    )

    result = runner(_step(
        "restart_screen_component",
        {
            "platform": "v4e2e", "component": "celery",
            "screen_session": "v4e2e_c", "project_root": str(tmp_path),
        },
        risk="medium",
    ))

    assert result.status == "failed"
    assert "screen_dead_cleanup_incomplete" in result.output
    assert "2031135.v4e2e_c" in result.output
    assert started == []


def test_screen_component_waits_for_generic_role_port_and_fails_if_not_ready(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    python = tmp_path / "python3.8"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    waited = []
    monkeypatch.setattr(module, "_python_executable", lambda _args: str(python))
    monkeypatch.setattr(module, "_runtime_python_env", lambda _root: {})
    monkeypatch.setattr(
        module,
        "_wait_tcp_listening",
        lambda host, port, timeout: waited.append((host, port, timeout)) or False,
    )
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
    )

    result = runner(_step(
        "start_screen_component",
        {
            "platform": "test",
            "component": "worker",
            "screen_session": "test_w",
            "project_root": str(tmp_path),
            "worker_port": "45555",
        },
        risk="medium",
    ))

    assert result.status == "failed"
    assert "component_port_not_ready" in result.output
    assert waited == [("127.0.0.1", 45555, 20.0)]


def test_dead_screen_target_is_identified_for_exact_session(monkeypatch):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runner = DirectPrivilegedActionRunner()
    monkeypatch.setattr(
        runner,
        "_screen_ls_text",
        lambda: (
            "2561348.vemu_uestc_w (Dead ???)\n"
            "2561338.vemu_uestc_m (Detached)\n"
        ),
    )

    assert runner._dead_screen_session_targets("vemu_uestc_w") == [
        "2561348.vemu_uestc_w"
    ]
    assert runner._dead_screen_session_targets("vemu_uestc_m") == []


def test_start_component_waits_for_exact_dead_screen_cleanup(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    python = tmp_path / "python3.8"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(module, "_python_executable", lambda _args: str(python))
    monkeypatch.setattr(module, "_runtime_python_env", lambda _root: {})
    monkeypatch.setattr(module, "_wait_tcp_listening", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
    )
    states = iter([
        "123.vemu_uestc_w (Dead ???)",
        "123.vemu_uestc_w (Dead ???)",
        "No Sockets found",
        "No Sockets found",
    ])
    monkeypatch.setattr(runner, "_screen_ls_text", lambda: next(states, "No Sockets found"))

    result = runner(_step(
        "start_screen_component",
        {
            "platform": "vemu_uestc", "component": "worker",
            "screen_session": "vemu_uestc_w", "project_root": str(tmp_path),
            "worker_port": "45552",
        },
        risk="medium",
    ))

    assert result.status == "completed"
    assert ["screen", "-wipe", "123.vemu_uestc_w"] in calls


def test_start_component_recovers_live_screen_without_runtime(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    python = tmp_path / "python3.8"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    calls = []
    runner = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(
        runner, "_clean_dead_screen_session", lambda *_args: ([], ""),
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["123.v4e2e_web"],
    )
    monkeypatch.setattr(runner, "_existing_screen_sessions", lambda *_args: set())
    listener_snapshots = iter([[], [], [202]])
    monkeypatch.setattr(
        module, "_listener_pids_for_port",
        lambda _port: next(listener_snapshots),
    )
    monkeypatch.setattr(module, "_runtime_python_env", lambda _root: {})
    monkeypatch.setattr(module, "_wait_tcp_listening", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))

    result = runner(_step(
        "start_screen_component",
        {
            "platform": "v4e2e", "component": "web_terminal",
            "screen_session": "v4e2e_web", "project_root": str(tmp_path),
            "web_terminal_port": 47003,
            "python_executable": str(python),
        },
        risk="medium",
    ))

    assert result.status == "completed", result.output
    assert "recovered_stale_screen=true" in result.output
    assert ["screen", "-S", "123.v4e2e_web", "-X", "quit"] in calls
    assert any("-dmS" in call and "v4e2e_web" in call for call in calls)


def test_start_component_is_idempotent_when_target_screen_owns_runtime(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    runner = DirectPrivilegedActionRunner()
    monkeypatch.setattr(
        runner, "_clean_dead_screen_session", lambda *_args: ([], ""),
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["123.v4e2e_web"],
    )
    monkeypatch.setattr(module, "_listener_pids_for_port", lambda _port: [101])
    monkeypatch.setattr(
        module, "_screen_owner_targets_for_pids",
        lambda _pids: (["123.v4e2e_web"], True),
    )
    monkeypatch.setattr(module, "_proc_cwd", lambda _pid: str(tmp_path))

    result = runner(_step(
        "start_screen_component",
        {
            "platform": "v4e2e", "component": "web_terminal",
            "screen_session": "v4e2e_web", "project_root": str(tmp_path),
            "web_terminal_port": 47003,
        },
        risk="medium",
    ))

    assert result.status == "completed"
    assert "already_running=true" in result.output
    assert "environment_changed=false" in result.output


def test_start_component_rejects_existing_screen_with_foreign_runtime(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    _write_runtime_entries(tmp_path)
    runner = DirectPrivilegedActionRunner()
    monkeypatch.setattr(
        runner, "_clean_dead_screen_session", lambda *_args: ([], ""),
    )
    monkeypatch.setattr(
        runner, "_screen_session_targets",
        lambda session, run_as_uid="": ["123.v4e2e_web"],
    )
    monkeypatch.setattr(module, "_listener_pids_for_port", lambda _port: [101])
    monkeypatch.setattr(
        module, "_screen_owner_targets_for_pids",
        lambda _pids: (["999.other_web"], True),
    )

    result = runner(_step(
        "start_screen_component",
        {
            "platform": "v4e2e", "component": "web_terminal",
            "screen_session": "v4e2e_web", "project_root": str(tmp_path),
            "web_terminal_port": 47003,
        },
        risk="medium",
    ))

    assert result.status == "blocked"
    assert "screen_session_runtime_ownership_unresolved" in result.output


def test_web_terminal_command_uses_frozen_active_config_port():
    from klonet_agent.ops.privileged.action_runner import _component_commands

    command = _component_commands("/usr/bin/python3.8")["web_terminal"]

    assert command[:2] == ["/usr/bin/python3.8", "-c"]
    assert "PROJ_CONFIG.web_terminal_port" in command[2]
    assert "web_terminal_main.py" not in command


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


def test_install_nginx_config_also_enables_site(tmp_path):
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    source = tmp_path / "lht.conf"
    source.write_text("server { listen 45563; }\n", encoding="utf-8")
    calls = []

    def command_runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "install_nginx_config",
            {"source_path": str(source), "config_name": "lht"},
        )
    )

    assert result.status == "completed"
    commands = [argv[1:] if argv and argv[0] == "sudo" else argv for argv, _ in calls]
    assert [
        "install",
        "-o",
        "root",
        "-g",
        "root",
        "-m",
        "0644",
        str(source.resolve()),
        "/etc/nginx/sites-available/lht",
    ] in commands
    assert [
        "ln",
        "-sfn",
        "/etc/nginx/sites-available/lht",
        "/etc/nginx/sites-enabled/lht",
    ] in commands
    assert "enabled=/etc/nginx/sites-enabled/lht" in result.output


def test_install_nginx_config_accepts_inline_content():
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    calls = []
    installed_content = []

    def command_runner(argv, **kwargs):
        command = list(argv[1:] if argv and argv[0] == "sudo" else argv)
        calls.append(command)
        if command and command[0] == "install":
            installed_content.append(
                Path(command[-2]).read_text(encoding="utf-8")
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    content = "server { listen 45563; }\n"
    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "install_nginx_config",
            {"content": content, "config_name": "lht"},
        )
    )

    assert result.status == "completed"
    assert installed_content == [content]
    assert any(command[0] == "ln" for command in calls)


def test_nginx_install_failure_before_copy_reports_no_environment_change():
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    result = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="sudo: a password is required"
        )
    )(
        _step(
            "install_nginx_config",
            {
                "content": "server { listen 47007; }\n",
                "config_name": "klonet-install-failure-test",
            },
        )
    )

    assert result.status == "failed"
    assert "environment_changed=false" in result.output


def test_reload_nginx_starts_inactive_service(monkeypatch):
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    calls = []

    def fake_which(command):
        return {
            "nginx": "/usr/sbin/nginx",
            "systemctl": "/usr/bin/systemctl",
            "pgrep": "/usr/bin/pgrep",
        }.get(command)

    def command_runner(argv, **kwargs):
        del kwargs
        calls.append(list(argv[1:] if argv and argv[0] == "sudo" else argv))
        return subprocess.CompletedProcess(
            argv,
            1 if "pgrep" in str(argv[0]) else 0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(
        "klonet_agent.ops.privileged.action_runner.shutil.which",
        fake_which,
    )

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step("reload_nginx", {})
    )

    assert result.status == "completed"
    assert calls == [
        ["/usr/sbin/nginx", "-t"],
        ["/usr/bin/pgrep", "-f", "^nginx: master process"],
        ["/usr/bin/systemctl", "start", "nginx"],
    ]
    assert "activation=start" in result.output


def test_reload_nginx_signals_existing_master(monkeypatch):
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    calls = []

    monkeypatch.setattr(
        "klonet_agent.ops.privileged.action_runner.shutil.which",
        lambda command: {
            "nginx": "/usr/sbin/nginx",
            "systemctl": "/usr/bin/systemctl",
            "pgrep": "/usr/bin/pgrep",
        }.get(command),
    )

    def command_runner(argv, **kwargs):
        del kwargs
        calls.append(list(argv[1:] if argv and argv[0] == "sudo" else argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step("reload_nginx", {})
    )

    assert result.status == "completed"
    assert calls == [
        ["/usr/sbin/nginx", "-t"],
        ["/usr/bin/pgrep", "-f", "^nginx: master process"],
        ["/usr/sbin/nginx", "-s", "reload"],
    ]
    assert "activation=signal-reload" in result.output


def test_sync_directory_refuses_nonempty_destination(tmp_path):
    from klonet_agent.ops.privileged.action_runner import (
        DirectPrivilegedActionRunner,
    )

    source = tmp_path / "source"
    destination = tmp_path / "existing"
    source.mkdir()
    destination.mkdir()
    (source / "new.txt").write_text("new", encoding="utf-8")
    legacy = destination / "legacy.txt"
    legacy.write_text("keep", encoding="utf-8")

    result = DirectPrivilegedActionRunner()(
        _step(
            "sync_directory",
            {"source": str(source), "destination": str(destination)},
        )
    )

    assert result.status == "blocked"
    assert "destination_not_empty" in result.output
    assert legacy.read_text(encoding="utf-8") == "keep"
    assert not (destination / "new.txt").exists()


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


def test_prepare_project_files_rejects_source_changed_after_approval(tmp_path):
    import hashlib

    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    root, _backend, mains = _layout(tmp_path)
    hashes = {
        name: hashlib.sha256((mains / name).read_bytes()).hexdigest()
        for name in ENTRY_FILES
    }
    (mains / "master_main.py").write_text("changed\n", encoding="utf-8")

    result = DirectPrivilegedActionRunner()(_step(
        "prepare_project_files",
        {
            "project_root": str(root),
            "source_root": str(mains),
            "entry_sha256s": hashes,
        },
    ))

    assert result.status == "blocked"
    assert "entry_sources_changed_after_approval=master_main.py" in result.output
    assert not (root / "gun.py").exists()


def test_prepare_project_files_freezes_targets_and_records_recoverable_backups(tmp_path):
    import hashlib
    import json
    from types import SimpleNamespace

    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    root, _backend, mains = _layout(tmp_path)
    target = root / "master_main.py"
    target.write_text("user version\n", encoding="utf-8")
    target_hashes = {
        name: (
            hashlib.sha256((root / name).read_bytes()).hexdigest()
            if (root / name).is_file() else "missing"
        )
        for name in ENTRY_FILES
    }
    runner = DirectPrivilegedActionRunner()
    result = runner(_step("prepare_project_files", {
        "project_root": str(root),
        "source_root": str(mains),
        "target_sha256s": target_hashes,
    }))

    assert result.status == "completed"
    backups = json.loads(result.metadata["backups"])
    assert str(target) in backups
    assert target.read_text(encoding="utf-8") != "user version\n"

    rolled_back = runner.rollback(SimpleNamespace(mutation=result.metadata))

    assert rolled_back.status == "completed"
    assert target.read_text(encoding="utf-8") == "user version\n"


def test_prepare_project_files_uses_existing_sudo_commit_and_rollback_for_root_owned_dir(
    monkeypatch, tmp_path,
):
    import json
    import shutil
    import subprocess
    from types import SimpleNamespace

    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    root, _backend, mains = _layout(tmp_path)
    original = root / "master_main.py"
    original.write_text("user version\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(module.os, "access", lambda path, mode: False)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        command = list(argv[1:] if argv[:1] == ["sudo"] else argv)
        if command[0] == "cp":
            shutil.copy2(command[-2], command[-1])
        elif command[0] == "install":
            shutil.copy2(command[-2], command[-1])
        elif command[0] == "rm":
            module.Path(command[-1]).unlink(missing_ok=True)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    result = runner(_step("prepare_project_files", {
        "project_root": str(root),
        "source_root": str(mains),
    }))

    assert result.status == "completed"
    assert original.read_text(encoding="utf-8") != "user version\n"
    assert any(call[:2] == ["sudo", "install"] for call in calls)
    assert any(call[:2] == ["sudo", "cp"] for call in calls)
    backups = json.loads(result.metadata["backups"])
    assert str(original) in backups

    rolled_back = runner.rollback(SimpleNamespace(mutation=result.metadata))

    assert rolled_back.status == "completed"
    assert original.read_text(encoding="utf-8") == "user version\n"
    assert not (root / "gun.py").exists()
    assert any(call[:2] == ["sudo", "rm"] for call in calls)


def test_prepare_project_files_rolls_back_earlier_entries_when_later_commit_fails(
    monkeypatch, tmp_path,
):
    from klonet_agent.ops.privileged.action_runner import (
        DirectActionResult, DirectPrivilegedActionRunner,
    )

    root, _backend, mains = _layout(tmp_path)
    runner = DirectPrivilegedActionRunner()
    original_commit = runner._commit_text_candidate
    calls = []

    def commit(path, original, updated, timeout):
        calls.append(path.name)
        if len(calls) == 2:
            return (
                DirectActionResult(
                    "failed",
                    "synthetic_commit_failure environment_changed=false",
                ),
                None,
                True,
            )
        return original_commit(path, original, updated, timeout)

    monkeypatch.setattr(runner, "_commit_text_candidate", commit)

    result = runner(_step("prepare_project_files", {
        "project_root": str(root),
        "source_root": str(mains),
    }))

    assert result.status == "failed"
    assert "partial_changes_rolled_back=true" in result.output
    assert "environment_changed=false" in result.output
    assert not any((root / name).exists() for name in ENTRY_FILES)


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


def test_start_screen_component_accepts_grounded_future_component(tmp_path):
    import subprocess
    import sys

    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES

    for name in REQUIRED_ENTRY_FILES:
        (tmp_path / name).write_text("# fixture\n", encoding="utf-8")
    calls = []

    def command_runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        stdout = "No Sockets found\n" if argv[-2:] == ["screen", "-ls"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    events = []
    result = DirectPrivilegedActionRunner(
        command_runner=command_runner,
        on_command=events.append,
    )(_step("start_screen_component", {
        "platform": "v4e2e",
        "component": "metrics",
        "screen_suffix": "metrics",
        "screen_session": "v4e2e_metrics",
        "project_root": str(tmp_path),
        "python": sys.executable,
        "command_argv": [sys.executable, "-m", "metrics_service"],
        "preflight_argv": [sys.executable, "-c", "import sys"],
    }))

    assert result.status == "completed"
    assert any(
        event["execution"] == "screen_foreground"
        and event["argv"][-2:] == ["-m", "metrics_service"]
        for event in events
    )
    assert any("v4e2e_metrics" in call[0] for call in calls)


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


def test_direct_runner_creates_new_container_with_bounded_docker_argv(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda program: "/usr/bin/docker")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, "", "No such object")
        return subprocess.CompletedProcess(argv, 0, "container-id\n", "")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "create_docker_container",
            {
                "name": "v4e2e-mysql",
                "image": "mysql:latest",
                "port_bindings": ["127.0.0.1:47005:3306"],
                "environment": ["MYSQL_ROOT_PASSWORD=private-value"],
                "restart_policy": "unless-stopped",
            },
            risk="high",
        )
    )

    assert result.status == "completed"
    assert calls == [
        ["/usr/bin/docker", "container", "inspect", "v4e2e-mysql"],
        ["/usr/bin/docker", "image", "inspect", "mysql:latest"],
        [
            "/usr/bin/docker", "run", "-d", "--name", "v4e2e-mysql",
            "--restart", "unless-stopped",
            "-p", "127.0.0.1:47005:3306",
            "-e", "MYSQL_ROOT_PASSWORD=private-value",
            "mysql:latest",
        ],
    ]
    assert "private-value" not in result.output


def test_direct_runner_allows_only_bounded_redis_password_command(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda program: "/usr/bin/docker")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, "", "No such object")
        return subprocess.CompletedProcess(argv, 0, "container-id\n", "")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    completed = runner(
        _step(
            "create_docker_container",
            {
                "name": "v4e2e-redis",
                "image": "redis:7",
                "port_bindings": ["127.0.0.1:47005:6379"],
                "command": ["redis-server", "--requirepass", "local-secret"],
            },
            risk="high",
        )
    )
    blocked = runner(
        _step(
            "create_docker_container",
            {
                "name": "v4e2e-redis-unsafe",
                "image": "redis:7",
                "port_bindings": ["127.0.0.1:47015:6379"],
                "command": ["sh", "-c", "touch /tmp/unsafe"],
            },
            risk="high",
        )
    )

    assert completed.status == "completed"
    assert calls[2][-4:] == ["redis:7", "redis-server", "--requirepass", "local-secret"]
    assert "local-secret" not in completed.output
    assert blocked.status == "blocked"
    assert "invalid_container_command" in blocked.output


def test_direct_runner_resolves_klonet_container_credentials_locally(
    tmp_path, monkeypatch
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text(
        "class MysqlConfig:\n"
        "    mysql_password = 'mysql-local-secret'\n"
        "    mysql_database = 'mydb'\n"
        "class RedisConfig:\n"
        "    redis_password = 'redis-local-secret'\n"
        "class WtxConfig(MysqlConfig, RedisConfig):\n"
        "    redis_password = 'redis-local-secret'\n"
        "PROJ_CONFIG = WtxConfig()\n",
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda _program: "/usr/bin/docker")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **_kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 1, "", "No such object")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    runner = DirectPrivilegedActionRunner(command_runner=command_runner)
    mysql = runner(
        _step(
            "create_docker_container",
            {
                "name": "v4e2e-mysql",
                "image": "mysql:8",
                "port_bindings": ["127.0.0.1:47006:3306"],
                "credential_source": {
                    "path": str(config),
                    "service": "mysql",
                },
            },
            risk="high",
        )
    )
    redis = runner(
        _step(
            "create_docker_container",
            {
                "name": "v4e2e-redis",
                "image": "redis:7",
                "port_bindings": ["127.0.0.1:47005:6379"],
                "credential_source": {
                    "path": str(config),
                    "service": "redis",
                },
            },
            risk="high",
        )
    )

    assert mysql.status == redis.status == "completed"
    mysql_run, redis_run = calls[2], calls[5]
    assert "MYSQL_ROOT_PASSWORD=mysql-local-secret" in mysql_run
    assert "MYSQL_DATABASE=mydb" in mysql_run
    assert redis_run[-4:] == [
        "redis:7", "redis-server", "--requirepass", "redis-local-secret"
    ]
    assert "mysql-local-secret" not in mysql.output
    assert "redis-local-secret" not in redis.output


def test_direct_runner_refuses_to_replace_existing_container(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda program: "/usr/bin/docker")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "existing-id\n", "")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "create_docker_container",
            {
                "name": "v4e2e-mysql",
                "image": "mysql:latest",
                "port_bindings": ["127.0.0.1:47005:3306"],
            },
            risk="high",
        )
    )

    assert result.status == "blocked"
    assert "container_already_exists" in result.output
    assert calls == [
        ["/usr/bin/docker", "container", "inspect", "v4e2e-mysql"]
    ]


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


def test_direct_runner_clone_at_revision_uses_frozen_branch_then_detached_revision(
    tmp_path,
):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    repository = tmp_path / "v4e2e"
    calls = []

    def command_runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "git_operation",
            {
                "repository": str(repository),
                "operation": "clone_at_revision",
                "url": "gitee:example/platform.git",
                "ref": "develop",
                "revision": "a" * 40,
            },
            risk="high",
        )
    )

    assert result.status == "completed"
    assert calls == [
        (
            [
                "git",
                "clone",
                "--branch",
                "develop",
                "--single-branch",
                "gitee:example/platform.git",
                "v4e2e",
            ],
                {"cwd": str(repository.parent), "env": None, "timeout": 120},
        ),
        (
            ["git", "checkout", "--detach", "a" * 40],
                {"cwd": str(repository), "env": None, "timeout": 120},
        ),
    ]


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

    def fake_processes(path, **kwargs):
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


def test_direct_runner_stops_only_exact_root_bound_component(monkeypatch, tmp_path):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runtime = tmp_path / "vemu_uestc"
    (runtime / "vemu_config").mkdir(parents=True)
    processes = [
        {"pid": 1001, "pgid": 1001, "cmdline": "gunicorn worker_main:flask_app", "cwd": str(runtime)},
        {"pid": 1002, "pgid": 1001, "cmdline": "gunicorn worker_main:flask_app", "cwd": str(runtime)},
        {"pid": 2001, "pgid": 2001, "cmdline": "python web_terminal_main.py", "cwd": str(runtime)},
    ]
    calls = []
    discovery_calls = []

    def exact_process_group(root, **kwargs):
        discovery_calls.append((root, dict(kwargs)))
        return processes

    monkeypatch.setattr(module, "_klonet_runtime_processes", exact_process_group)
    monkeypatch.setattr(
        module, "_listener_pids_for_port", lambda port: [1002] if processes else []
    )
    monkeypatch.setattr(module.Path, "exists", lambda path: False if str(path) == "/proc/1001" else True)
    monkeypatch.setattr(module, "_proc_uid", lambda pid: 1000)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        processes.clear()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "stop_klonet_component",
            {"runtime_cwd": str(runtime), "component": "worker", "pid": 1001, "port": 45552},
            risk="high",
        )
    )

    assert result.status == "completed"
    assert calls == [["kill", "-TERM", "-1001"]]
    assert discovery_calls[0][1]["expected_pid"] == 1001
    assert "component=worker" in result.output


def test_component_stop_accepts_frozen_role_with_rooted_command_and_foreign_cwd(
    monkeypatch, tmp_path,
):
    """A manual launch may identify its instance by absolute config path."""

    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runtime = tmp_path / "vemu_uestc"
    (runtime / "vemu_config").mkdir(parents=True)
    processes = [{
        "pid": 1001,
        "pgid": 1001,
        "cmdline": (
            "python -m gunicorn -c %s worker_main:flask_app"
            % (runtime / "mains" / "worker_gun.py")
        ),
        "cwd": str(tmp_path),
    }]
    discovery_calls = []

    def exact_process_group(root, **kwargs):
        discovery_calls.append(dict(kwargs))
        return list(processes) if kwargs.get("allow_command_root_identity") else []

    monkeypatch.setattr(module, "_klonet_runtime_processes", exact_process_group)
    monkeypatch.setattr(
        module, "_listener_pids_for_port", lambda _port: [1001] if processes else [],
    )
    monkeypatch.setattr(
        module.Path, "exists",
        lambda path: False if str(path) == "/proc/1001" else True,
    )

    def command_runner(argv, **kwargs):
        processes.clear()
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "stop_klonet_component",
            {
                "runtime_cwd": str(runtime), "component": "worker",
                "pid": 1001, "port": 45552,
            },
            risk="high",
        )
    )

    assert result.status == "completed", result.output
    assert discovery_calls[0]["expected_pid"] == 1001
    assert discovery_calls[0]["allow_command_root_identity"] is True


def test_process_project_identity_uses_paths_not_substrings(tmp_path):
    from klonet_agent.ops.privileged.environment_facts import (
        process_belongs_to_project_root,
    )

    root = tmp_path / "vemu_uestc"
    rooted_config = root / "mains" / "worker_gun.py"

    assert process_belongs_to_project_root(
        cwd=str(root), cmdline="gunicorn worker_main:flask_app", project_root=root,
    )
    assert process_belongs_to_project_root(
        cwd=str(tmp_path),
        cmdline="gunicorn --config=%s worker_main:flask_app" % rooted_config,
        project_root=root,
    )
    assert not process_belongs_to_project_root(
        cwd=str(tmp_path),
        cmdline="gunicorn --label=%s-backup worker_main:flask_app" % root,
        project_root=root,
    )
    assert not process_belongs_to_project_root(
        cwd=str(tmp_path),
        cmdline="gunicorn -c /srv/other/worker_gun.py worker_main:flask_app",
        project_root=root,
    )


def test_component_stop_escalates_same_frozen_group_after_term_timeout(
    monkeypatch, tmp_path,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runtime = tmp_path / "vemu_uestc"
    (runtime / "vemu_config").mkdir(parents=True)
    processes = [
        {
            "pid": 1001, "pgid": 1001,
            "cmdline": "gunicorn master_main:flask_app", "cwd": str(runtime),
        },
        {
            "pid": 1002, "pgid": 1001,
            "cmdline": "gunicorn master_main:flask_app", "cwd": str(runtime),
        },
    ]
    monkeypatch.setattr(
        module, "_klonet_runtime_processes",
        lambda root, **kwargs: list(processes),
    )
    monkeypatch.setattr(
        module, "_listener_pids_for_port",
        lambda _port: [1001, 1002] if processes else [],
    )
    monkeypatch.setattr(
        module.Path, "exists",
        lambda path: bool(processes) if str(path) == "/proc/1001" else True,
    )
    clock = iter([0.0, 9.0, 10.0, 10.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    calls = []

    def command_runner(argv, **kwargs):
        calls.append(list(argv))
        if "-KILL" in argv:
            processes.clear()
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "stop_klonet_component",
            {
                "runtime_cwd": str(runtime), "component": "master",
                "pid": 1001, "port": 45554,
            },
            risk="high",
        )
    )

    assert result.status == "completed", result.output
    assert [call[-3:] for call in calls] == [
        ["kill", "-TERM", "-1001"], ["kill", "-KILL", "-1001"],
    ]
    assert "signal=TERM,KILL" in result.output


def test_default_runner_authenticates_sudo_once_and_forces_noninteractive_commands(
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["sudo", "-n", "-v"]:
            return subprocess.CompletedProcess(argv, 1, "", "password required")
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)
    runner = DirectPrivilegedActionRunner()

    first = runner._run_command(
        ["sudo", "readlink", "-f", "/proc/123/cwd"],
        cwd=None,
        env=None,
        timeout=5,
    )
    second = runner._run_command(
        ["sudo", "kill", "-TERM", "-123"],
        cwd=None,
        env=None,
        timeout=5,
    )

    assert first.returncode == second.returncode == 0
    assert calls == [
        ["sudo", "-n", "-v"],
        ["sudo", "-v"],
        ["sudo", "-n", "readlink", "-f", "/proc/123/cwd"],
        ["sudo", "-n", "kill", "-TERM", "-123"],
    ]


def test_default_runner_failed_sudo_authentication_never_reprompts_within_action(
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "authentication failed")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)
    runner = DirectPrivilegedActionRunner()

    first = runner._run_command(
        ["sudo", "readlink", "-f", "/proc/123/cwd"],
        cwd=None,
        env=None,
        timeout=5,
    )
    second = runner._run_command(
        ["sudo", "readlink", "-f", "/proc/124/cwd"],
        cwd=None,
        env=None,
        timeout=5,
    )

    assert first.returncode == second.returncode == 1
    assert first.stderr == second.stderr == "sudo_authentication_failed"
    assert calls == [["sudo", "-n", "-v"], ["sudo", "-v"]]


def test_component_stop_accepts_instance_root_with_complete_mains(monkeypatch, tmp_path):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runtime = tmp_path / "102"
    mains = runtime / "mains"
    mains.mkdir(parents=True)
    _write_runtime_entries(mains)
    processes = [{
        "pid": 1001,
        "pgid": 1001,
        "cmdline": "gunicorn master_main:flask_app",
        "cwd": str(runtime),
    }]
    monkeypatch.setattr(
        module, "_klonet_runtime_processes", lambda root, **kwargs: processes,
    )
    monkeypatch.setattr(
        module,
        "_listener_pids_for_port",
        lambda port: [1001] if processes else [],
    )
    monkeypatch.setattr(
        module.Path,
        "exists",
        lambda path: False if str(path) == "/proc/1001" else True,
    )
    monkeypatch.setattr(module, "_proc_uid", lambda pid: 1000)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)

    def command_runner(argv, **kwargs):
        processes.clear()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = DirectPrivilegedActionRunner(command_runner=command_runner)(
        _step(
            "stop_klonet_component",
            {
                "runtime_cwd": str(runtime),
                "component": "master",
                "pid": 1001,
                "port": 27694,
            },
            risk="high",
        )
    )

    assert result.status == "completed"
    assert "runtime_cwd=%s" % runtime in result.output


def test_runtime_cwd_normalizes_complete_mains_to_instance_root(tmp_path):
    from klonet_agent.ops.privileged.action_runner import (
        _normalized_klonet_runtime_cwd,
    )

    root = tmp_path / "102"
    mains = root / "mains"
    mains.mkdir(parents=True)
    _write_runtime_entries(mains)

    assert _normalized_klonet_runtime_cwd(mains) == root.resolve()


def test_proc_cwd_uses_bounded_sudo_readlink_for_other_user(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module

    original_resolve = module.Path.resolve

    def resolve(path, *args, **kwargs):
        if str(path) == "/proc/1234/cwd":
            raise PermissionError("cross-user proc cwd")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(module.Path, "resolve", resolve)
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="/srv/instance\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module._proc_cwd(1234) == "/srv/instance"
    assert calls == [["sudo", "-n", "readlink", "-f", "/proc/1234/cwd"]]


def test_proc_cwd_uses_action_runner_interactive_sudo_for_approved_action(
    monkeypatch,
):
    from klonet_agent.ops.privileged import action_runner as module

    original_resolve = module.Path.resolve

    def resolve(path, *args, **kwargs):
        if str(path) == "/proc/1234/cwd":
            raise PermissionError("cross-user proc cwd")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(module.Path, "resolve", resolve)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    calls = []

    def command_runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(
            argv, 0, stdout="/srv/instance\n", stderr="",
        )

    assert module._proc_cwd(1234, command_runner=command_runner) == "/srv/instance"
    assert calls == [
        (["sudo", "readlink", "-f", "/proc/1234/cwd"], {"timeout": 5}),
    ]


def test_listener_pid_query_retries_with_bounded_sudo(monkeypatch):
    from klonet_agent.ops.privileged import action_runner as module

    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/sbin/ss")
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        stdout = (
            "LISTEN 0 128 127.0.0.1:27694 0.0.0.0:* "
            'users:(("gunicorn",pid=4321,fd=5))\n'
            if argv[0] == "sudo"
            else "LISTEN 0 128 127.0.0.1:27694 0.0.0.0:*\n"
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module._listener_pids_for_port(27694) == [4321]
    assert calls == [
        ["/usr/sbin/ss", "-ltnp"],
        ["sudo", "-n", "/usr/sbin/ss", "-ltnp"],
    ]


def test_component_stop_blocks_role_or_port_mismatch(monkeypatch, tmp_path):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runtime = tmp_path / "vemu_uestc"
    (runtime / "vemu_config").mkdir(parents=True)
    monkeypatch.setattr(module, "_klonet_runtime_processes", lambda root, **kwargs: [{
        "pid": 1001, "pgid": 1001,
        "cmdline": "gunicorn master_main:flask_app", "cwd": str(runtime),
    }])
    monkeypatch.setattr(module, "_listener_pids_for_port", lambda port: [1001])

    result = DirectPrivilegedActionRunner()(
        _step(
            "stop_klonet_component",
            {"runtime_cwd": str(runtime), "component": "worker", "pid": 1001, "port": 45552},
            risk="high",
        )
    )

    assert result.status == "blocked"
    assert "component_role_mismatch" in result.output


def test_stop_runtime_does_not_require_shared_ports_owned_by_other_root_to_close(
    monkeypatch, tmp_path,
):
    from klonet_agent.ops.privileged import action_runner as module
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    runtime = tmp_path / "vemu_uestc"
    (runtime / "vemu_config").mkdir(parents=True)
    snapshots = [[{
        "pid": 1001,
        "pgid": 1001,
        "cmdline": "python -m gunicorn -c worker_gun.py worker_main:flask_app",
        "cwd": str(runtime),
    }], []]
    monkeypatch.setattr(
        module,
        "_klonet_runtime_processes",
        lambda path, **kwargs: snapshots.pop(0) if snapshots else [],
    )
    monkeypatch.setattr(
        module,
        "_runtime_ports_owned_by_allowed_cwd",
        lambda ports, root: [],
    )
    monkeypatch.setattr(
        module,
        "_ports_currently_listening",
        lambda runner, ports: [45551],
    )
    monkeypatch.setattr(module, "_proc_uid", lambda pid: 1000)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000, raising=False)

    result = DirectPrivilegedActionRunner(
        command_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="", stderr=""
        )
    )(
        _step(
            "stop_klonet_runtime_instance",
            {"runtime_cwd": str(runtime), "ports": [45551, 45552]},
            risk="high",
        )
    )

    assert result.status == "completed"


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


def test_text_replacement_preserves_existing_mode_and_owner(tmp_path):
    import os
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    target = tmp_path / "master_main.py"
    target.write_text("flask_app = create_app()\n", encoding="utf-8")
    target.chmod(0o775)
    before = target.stat()
    runner = DirectPrivilegedActionRunner()

    result = runner(_step(
        "replace_text_in_file",
        {
            "path": str(target),
            "old_text": "flask_app = create_app()",
            "new_text": "flask_app = create_master_app()",
        },
        risk="medium",
    ))

    after = target.stat()
    assert result.status == "completed"
    assert (after.st_uid, after.st_gid, after.st_mode & 0o777) == (
        before.st_uid, before.st_gid, 0o775,
    )


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


def test_direct_runner_generic_text_edit_supports_anchored_python_change(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text(
        "class WtxConfig:\n    master_port = 45551\n\n"
        "PROJ_CONFIG = WtxConfig()\n",
        encoding="utf-8",
    )
    inserted = "class LhtConfig(WtxConfig):\n    master_port = 5200\n"

    result = DirectPrivilegedActionRunner()(
        _step(
            "edit_text_file",
            {
                "path": str(config),
                "operation": "insert_before",
                "anchor": "PROJ_CONFIG = WtxConfig()",
                "content": inserted,
            },
        )
    )

    updated = config.read_text(encoding="utf-8")
    assert result.status == "completed"
    assert updated.index("class LhtConfig") < updated.index("PROJ_CONFIG")
    assert len(list(tmp_path.glob("config.py.klonet-agent.bak.*"))) == 1


def test_direct_runner_generic_text_edit_rejects_invalid_python_result(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    original = "PROJ_CONFIG = object()\n"
    config.write_text(original, encoding="utf-8")

    result = DirectPrivilegedActionRunner()(
        _step(
            "edit_text_file",
            {
                "path": str(config),
                "operation": "append",
                "anchor": "",
                "content": "class Broken(:",
            },
        )
    )

    assert result.status == "blocked"
    assert "text_edit_result_invalid_SyntaxError" in result.output
    assert config.read_text(encoding="utf-8") == original


def test_python_candidate_compiler_rejects_subclass_before_local_base(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    original = (
        "class CommonConfig:\n"
        "    pass\n\n"
        "class ExistingConfig(CommonConfig):\n"
        "    pass\n"
    )
    config.write_text(original, encoding="utf-8")

    result = DirectPrivilegedActionRunner()(
        _step(
            "insert_text_before_anchor",
            {
                "path": str(config),
                "anchor": "class CommonConfig:",
                "content": "class LhtConfig(CommonConfig):\n    master_port = 46001\n",
            },
        )
    )

    assert result.status == "blocked"
    assert "candidate_python_base_defined_after_subclass=LhtConfig:CommonConfig" in result.output
    assert config.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("config.py.klonet-agent.bak.*"))


def test_upsert_python_class_moves_existing_subclass_after_base(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text(
        "class LhtConfig(CommonConfig):\n"
        "    master_port = 1\n\n"
        "class CommonConfig:\n"
        "    inherited = True\n\n"
        "class ExistingConfig(CommonConfig):\n"
        "    pass\n",
        encoding="utf-8",
    )

    result = DirectPrivilegedActionRunner()(
        _step(
            "upsert_python_class",
            {
                "path": str(config),
                "class_name": "LhtConfig",
                "base_class": "CommonConfig",
                "body": (
                    "master_ip = '192.128.122.101'\n"
                    "master_port = 46001\n"
                    "worker_port = 46002\n"
                    "web_terminal_port = 46003"
                ),
            },
        )
    )

    updated = config.read_text(encoding="utf-8")
    assert result.status == "completed"
    assert updated.count("class LhtConfig(CommonConfig):") == 1
    assert updated.index("class CommonConfig:") < updated.index(
        "class LhtConfig(CommonConfig):"
    )
    compile(updated, str(config), "exec")
    assert result.metadata["state"] == "applied_unverified"


def test_upsert_python_class_does_not_write_when_local_base_is_missing(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    original = "class ExistingConfig:\n    pass\n"
    config.write_text(original, encoding="utf-8")

    result = DirectPrivilegedActionRunner()(
        _step(
            "upsert_python_class",
            {
                "path": str(config),
                "class_name": "LhtConfig",
                "base_class": "CommonConfig",
                "body": "master_port = 46001",
            },
        )
    )

    assert result.status == "blocked"
    assert "python_class_local_base_missing=CommonConfig" in result.output
    assert config.read_text(encoding="utf-8") == original


def test_upsert_python_config_class_infers_common_config_base(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text("class CommonConfig:\n    pass\n", encoding="utf-8")

    result = DirectPrivilegedActionRunner()(
        _step(
            "upsert_python_class",
            {
                "path": str(config),
                "class_name": "LhtConfig",
                "body": "master_port = 46001",
            },
        )
    )

    assert result.status == "completed"
    assert "class LhtConfig(CommonConfig):" in config.read_text(encoding="utf-8")


def test_set_python_config_assignment_uses_ast_and_validates_candidate(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text(
        "class CommonConfig:\n"
        "    pass\n\n"
        "class OldConfig(CommonConfig):\n"
        "    pass\n\n"
        "class LhtConfig(CommonConfig):\n"
        "    master_port = 46001\n\n"
        "PROJ_CONFIG = OldConfig()\n",
        encoding="utf-8",
    )

    result = DirectPrivilegedActionRunner()(
        _step(
            "set_python_config_assignment",
            {
                "path": str(config),
                "class_name": "LhtConfig",
            },
        )
    )

    updated = config.read_text(encoding="utf-8")
    assert result.status == "completed"
    assert "PROJ_CONFIG = LhtConfig()" in updated
    assert "PROJ_CONFIG = OldConfig()" not in updated
    compile(updated, str(config), "exec")
    assert result.metadata["state"] == "applied_unverified"


def test_set_python_config_assignment_rejects_missing_class_without_writing(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    original = "class OldConfig:\n    pass\n\nPROJ_CONFIG = OldConfig()\n"
    config.write_text(original, encoding="utf-8")

    result = DirectPrivilegedActionRunner()(
        _step(
            "set_python_config_assignment",
            {
                "path": str(config),
                "assignment_name": "PROJ_CONFIG",
                "class_name": "LhtConfig",
            },
        )
    )

    assert result.status == "blocked"
    assert "python_config_class_count=0 expected=1" in result.output
    assert config.read_text(encoding="utf-8") == original


def test_set_python_class_attribute_infers_active_config_and_preserves_type(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner

    config = tmp_path / "config.py"
    config.write_text(
        "class CommonConfig:\n"
        "    master_port = 10000\n\n"
        "class WtxConfig(CommonConfig):\n"
        "    worker_port = 10001\n\n"
        "PROJ_CONFIG = WtxConfig()\n",
        encoding="utf-8",
    )
    runner = DirectPrivilegedActionRunner()

    changed = runner(
        _step(
            "set_python_class_attribute",
            {
                "path": str(config),
                "attribute": "worker_port",
                "value": "45562",
            },
        )
    )
    appended = runner(
        _step(
            "set_python_class_attribute",
            {
                "path": str(config),
                "attribute": "master_ip",
                "value": "192.168.1.33",
            },
        )
    )

    updated = config.read_text(encoding="utf-8")
    assert changed.status == appended.status == "completed"
    assert "worker_port = 45562" in updated
    assert "master_ip = '192.168.1.33'" in updated
    compile(updated, str(config), "exec")


def test_text_mutation_records_backup_and_can_be_rolled_back(tmp_path):
    from klonet_agent.ops.privileged.action_runner import DirectPrivilegedActionRunner
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence

    config = tmp_path / "config.py"
    original = "class CommonConfig:\n    pass\n"
    config.write_text(original, encoding="utf-8")
    runner = DirectPrivilegedActionRunner()

    result = runner(
        _step(
            "edit_text_file",
            {
                "path": str(config),
                "operation": "append",
                "anchor": "",
                "content": "class LhtConfig(CommonConfig):\n    master_port = 46001\n",
            },
        )
    )

    assert result.status == "completed"
    assert result.metadata["state"] == "applied_unverified"
    assert Path(result.metadata["backup"]).is_file()
    rollback = runner.rollback(
        ExecutionEvidence(environment_changed=True, mutation=result.metadata)
    )
    assert rollback.status == "completed"
    assert config.read_text(encoding="utf-8") == original


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
