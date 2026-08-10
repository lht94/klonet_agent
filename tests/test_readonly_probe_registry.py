from __future__ import annotations

import subprocess
import sys
import tarfile


def test_readonly_probe_registry_covers_required_environment_evidence():
    from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES

    expected = {
        "privilege_capabilities",
        "system_environment",
        "project_layout",
        "python_runtime",
        "ports",
        "port_owner",
        "process",
        "process_tree",
        "process_logs",
        "service",
        "screen",
        "docker",
        "nginx",
        "redis",
        "mysql",
        "rabbitmq",
        "network",
        "firewall",
        "disk",
        "memory",
        "git_repository",
        "logs",
        "tcp_connection",
        "http_endpoint",
        "python_import",
        "path_permissions",
        "virtualization",
        "libvirt",
        "ovs",
        "docker_networks",
        "docker_images",
        "network_links",
        "file_integrity",
        "json_file",
        "archive_inventory",
        "klonet_config_consistency",
    }

    assert expected <= {spec.name for spec in DEFAULT_READONLY_PROBES.describe()}
    assert all(spec.description for spec in DEFAULT_READONLY_PROBES.describe())


def test_process_logs_requires_pid_cwd_and_log_inside_same_project_root(
    tmp_path, monkeypatch
):
    from klonet_agent.ops.privileged import probes

    runtime = tmp_path / "102"
    logs = runtime / "logs"
    logs.mkdir(parents=True)
    log = logs / "master.any-name.log"
    log.write_text("RuntimeError: boot failed\n", encoding="utf-8")
    monkeypatch.setattr(
        probes,
        "_safe_readlink",
        lambda path: str(runtime) if str(path) == "/proc/123/cwd" else "",
    )
    monkeypatch.setattr(probes, "_process_log_paths", lambda pid: [log])
    monkeypatch.setattr(
        probes,
        "_resolve_process_log_path",
        lambda path, root: path,
    )

    output = probes.DEFAULT_READONLY_PROBES.run(
        "process_logs",
        {"pids": [123], "project_root": str(runtime)},
    )

    assert "fd_log=%s" % log in output
    assert "RuntimeError: boot failed" in output

    monkeypatch.setattr(
        probes,
        "_safe_readlink",
        lambda path: "/srv/unrelated" if str(path) == "/proc/123/cwd" else "",
    )
    refused = probes.DEFAULT_READONLY_PROBES.run(
        "process_logs",
        {"pids": [123], "project_root": str(runtime)},
    )
    assert "refused_cwd_outside_project_root" in refused
    assert "RuntimeError" not in refused


def test_process_log_privileged_fallback_stays_inside_bound_root(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import probes

    root = tmp_path / "102"
    log = root / "logs" / "master.log"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[2:4] == ["readlink", "-f"]:
            return str(log)
        if argv[2:4] == ["tail", "-c"]:
            return "RuntimeError: injected boot failure"
        raise AssertionError(argv)

    monkeypatch.setattr(probes, "_run", fake_run)
    assert probes._resolve_process_log_path(log, root) == log
    output = probes._read_process_log(log)
    assert "RuntimeError: injected boot failure" in output
    assert calls == [
        (["sudo", "-n", "readlink", "-f", str(log)], {"timeout": 4}),
        (
            ["sudo", "-n", "tail", "-c", "20000", "--", str(log)],
            {"timeout": 8},
        ),
    ]

    calls.clear()
    outside = tmp_path / "elsewhere" / "master.log"
    assert probes._resolve_process_log_path(outside, root) is None
    assert not calls


def test_process_metadata_readlink_uses_bounded_sudo_only_for_allowlisted_proc_links(
    monkeypatch,
):
    from klonet_agent.ops.privileged import probes

    calls = []
    monkeypatch.setattr(probes, "_safe_readlink", lambda _path: "")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return "/home/klonet-agent/102"

    monkeypatch.setattr(probes, "_run", fake_run)

    assert probes._process_metadata_readlink(
        probes.Path("/proc/123/cwd")
    ) == "/home/klonet-agent/102"
    assert calls == [
        (
            ["sudo", "-n", "readlink", "-f", "/proc/123/cwd"],
            {"timeout": 4},
        )
    ]

    calls.clear()
    assert probes._process_metadata_readlink(probes.Path("/proc/123/environ")) == ""
    assert probes._process_metadata_readlink(probes.Path("/proc/self/cwd")) == ""
    assert not calls


def test_process_metadata_readlink_falls_back_when_resolve_returns_proc_path(
    monkeypatch,
):
    from klonet_agent.ops.privileged import probes

    monkeypatch.setattr(probes, "_safe_readlink", lambda path: str(path))
    monkeypatch.setattr(
        probes,
        "_run",
        lambda argv, **kwargs: "/home/klonet-agent/102/logs/master.log",
    )

    assert probes._process_metadata_readlink(
        probes.Path("/proc/123/fd/1")
    ) == "/home/klonet-agent/102/logs/master.log"


def test_process_metadata_readlink_rejects_ambiguous_privileged_output(monkeypatch):
    from klonet_agent.ops.privileged import probes

    monkeypatch.setattr(probes, "_safe_readlink", lambda _path: "")
    monkeypatch.setattr(
        probes,
        "_run",
        lambda _argv, **_kwargs: "/home/klonet-agent/102\nsudo: warning",
    )

    assert probes._process_metadata_readlink(probes.Path("/proc/123/fd/2")) == ""


def test_project_layout_probe_discovers_nested_klonet_package(tmp_path):
    from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES

    package = tmp_path / "vemu_uestc"
    mains = package / "mains"
    mains.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (mains / "master_main.py").write_text("# entry\n", encoding="utf-8")

    output = DEFAULT_READONLY_PROBES.run(
        "project_layout",
        {"project_roots": [str(tmp_path)]},
    )

    assert str(tmp_path) in output
    assert "vemu_uestc" in output


def test_screen_probe_maps_session_to_descendant_runtime_cwd(monkeypatch):
    from klonet_agent.ops.privileged import probes

    def fake_run(argv, **_kwargs):
        if argv[:2] == ["screen", "-ls"]:
            return "100.vemu_uestc_m (Detached)"
        raise AssertionError(argv)

    def fake_readlink(path):
        if str(path) == "/proc/101/cwd":
            return "/home/lzl/vemu_uestc/mains"
        return ""

    monkeypatch.setattr(probes, "_run", fake_run)
    monkeypatch.setattr(probes, "_safe_readlink", fake_readlink)
    monkeypatch.setattr(
        probes,
        "_nearest_git_root",
        lambda cwd: "/home/lzl/vemu_uestc" if "vemu_uestc" in cwd else "",
        raising=False,
    )
    monkeypatch.setattr(
        probes,
        "_git_repository",
        lambda args: "inspect_git_repository\npath=%s inside_work_tree=true branch=develop remotes=origin=gitee:example/vemu.git"
        % args["repository"],
    )
    monkeypatch.setattr(
        probes,
        "_proc_children",
        lambda pid: [101] if pid == 100 else [],
        raising=False,
    )
    monkeypatch.setattr(
        probes,
        "inspect_screen_session",
        lambda args: "inspect_screen_session\nsession=%s scrollback" % args["session"],
    )

    output = probes.DEFAULT_READONLY_PROBES.run("screen", {})

    assert "session=vemu_uestc_m" in output
    assert "runtime_cwds=/home/lzl/vemu_uestc/mains" in output
    assert "git_roots=/home/lzl/vemu_uestc" in output
    assert "inside_work_tree=true" in output
    assert "origin=gitee:example/vemu.git" in output
    filtered = probes.DEFAULT_READONLY_PROBES.run(
        "screen",
        {"session": "vemu_uestc"},
    )
    assert "runtime_cwds=/home/lzl/vemu_uestc/mains" in filtered
    assert "origin=gitee:example/vemu.git" in filtered
    assert "session=vemu_uestc scrollback" in filtered


def test_file_integrity_distinguishes_existing_directory_from_missing(tmp_path):
    from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES

    (tmp_path / "entry.py").write_text("# entry\n", encoding="utf-8")

    output = DEFAULT_READONLY_PROBES.run(
        "file_integrity",
        {"paths": [str(tmp_path)]},
    )

    assert "type=directory" in output
    assert "exists=true" in output
    assert "readable=true" in output
    assert "missing_or_invalid" not in output


def test_git_repository_probe_reports_branch_and_revision(tmp_path):
    from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    (tmp_path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "initial"],
        check=True,
    )

    output = DEFAULT_READONLY_PROBES.run(
        "git_repository",
        {"path": str(tmp_path)},
    )

    assert "inside_work_tree=true" in output
    assert "revision=" in output
    assert "status=" in output


def test_python_import_probe_uses_explicit_runtime_and_working_directory(tmp_path):
    from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES

    (tmp_path / "demo_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = DEFAULT_READONLY_PROBES.run(
        "python_import",
        {
            "python_executable": sys.executable,
            "module": "demo_module",
            "cwd": str(tmp_path),
        },
    )

    assert "import_ok=true" in output
    assert "demo_module.py" in output


def test_probe_registry_refuses_unknown_probe_and_redacts_secrets():
    from klonet_agent.ops.privileged.probes import (
        ReadOnlyProbeRegistry,
        ReadOnlyProbeSpec,
    )

    registry = ReadOnlyProbeRegistry(
        (
            ReadOnlyProbeSpec(
                "secret_demo",
                "test",
                lambda _args: (
                    "password=super-secret "
                    "docker-entrypoint.sh --requirepass redis-secret"
                ),
            ),
        )
    )

    assert "probe_not_registered" in registry.run("missing", {})
    output = registry.run("secret_demo", {})
    assert "super-secret" not in output
    assert "redis-secret" not in output


def test_archive_and_config_probes_report_structure_without_secret_values(
    tmp_path,
):
    from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES

    backend = tmp_path / "vemu_uestc" / "vemu_config"
    backend.mkdir(parents=True)
    config = backend / "config.py"
    config.write_text(
        (
            "class Demo:\n"
            "    master_port = 5000\n"
            "    redis_port = 8368\n"
            "    redis_password = 'do-not-render'\n"
            "PROJ_CONFIG = Demo()\n"
        ),
        encoding="utf-8",
    )
    archive_path = tmp_path / "bundle.tar"
    readme = tmp_path / "README.md"
    readme.write_text("bundle\n", encoding="utf-8")
    with tarfile.open(archive_path, "w") as archive:
        archive.add(readme, arcname="bundle/README.md")

    archive_output = DEFAULT_READONLY_PROBES.run(
        "archive_inventory",
        {"path": str(archive_path)},
    )
    config_output = DEFAULT_READONLY_PROBES.run(
        "klonet_config_consistency",
        {"project_root": str(tmp_path)},
    )

    assert "bundle/README.md type=file" in archive_output
    assert "active_config=Demo" in config_output
    assert "redis_port=8368" in config_output
    assert "do-not-render" not in config_output
