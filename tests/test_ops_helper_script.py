"""Server-side Ops helper contract tests."""

import importlib.machinery
import importlib.util
import shlex
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER = PROJECT_ROOT / "scripts" / "klonet-agent-op"


def test_docker_container_helper_dry_run_contracts():
    inspect_result = subprocess.run(
        [sys.executable, str(HELPER), "inspect-docker-containers", "--dry-run", "--name", "mysql-vemu"],
        capture_output=True,
        text=True,
    )
    start_result = subprocess.run(
        [sys.executable, str(HELPER), "start-docker-container", "--dry-run", "--name", "mysql-vemu"],
        capture_output=True,
        text=True,
    )

    assert inspect_result.returncode == 0
    assert "container_filter=mysql-vemu" in inspect_result.stdout
    assert start_result.returncode == 0
    assert "container=mysql-vemu" in start_result.stdout
    assert "environment_changed=false" in start_result.stdout


def test_read_file_helper_execute_reads_regular_file(tmp_path):
    target = tmp_path / "secret.env"
    target.write_text("TOKEN=visible-to-root-read\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "read-file",
            "--execute",
            "--path",
            str(target),
            "--max-chars",
            "200",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "action=read-file" in result.stdout
    assert "environment_changed=false" in result.stdout
    assert "content:" in result.stdout
    assert "TOKEN=visible-to-root-read" in result.stdout


def test_inspect_install_scripts_helper_execute_reads_scripts(tmp_path):
    install_dir = tmp_path / "vemu_install_new_gen"
    install_dir.mkdir()
    (install_dir / "base_requ_setup.sh").write_text(
        "#!/usr/bin/env bash\napt-get update\n",
        encoding="utf-8",
    )
    (install_dir / "docker_service.sh").write_text(
        "#!/bin/bash\ndocker ps\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "inspect-install-scripts",
            "--execute",
            "--script-dir",
            str(install_dir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "action=inspect-install-scripts" in result.stdout
    assert "script=base_requ_setup.sh status=detected" in result.stdout
    assert "risk_markers=apt-get" in result.stdout
    assert "script=docker_service.sh status=detected" in result.stdout
    assert "risk_markers=docker" in result.stdout
    assert "environment_changed=false" in result.stdout


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


def test_start_platform_screens_helper_dry_run_outputs_command_contract():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "start-platform-screens",
            "--dry-run",
            "--platform",
            "103",
            "--project-root",
            "/home/adminis/lht/103_project/vemu_uestc",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "klonet_agent_op" in result.stdout
    assert "action=start-platform-screens" in result.stdout
    assert "dry_run=true" in result.stdout
    assert "platform=103" in result.stdout
    assert "project_root=/home/adminis/lht/103_project/vemu_uestc" in result.stdout
    assert "screen_sessions=103_m,103_c,103_web,103_w" in result.stdout
    assert "environment_changed=false" in result.stdout


def test_stop_screen_component_helper_dry_run_outputs_command_contract():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "stop-screen-component",
            "--dry-run",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "klonet_agent_op" in result.stdout
    assert "action=stop-screen-component" in result.stdout
    assert "dry_run=true" in result.stdout
    assert "platform=102" in result.stdout
    assert "component=master" in result.stdout
    assert "screen_session=102_m" in result.stdout
    assert "environment_changed=false" in result.stdout


def test_stop_platform_screens_helper_dry_run_outputs_command_contract():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "stop-platform-screens",
            "--dry-run",
            "--platform",
            "102",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "klonet_agent_op" in result.stdout
    assert "action=stop-platform-screens" in result.stdout
    assert "dry_run=true" in result.stdout
    assert "platform=102" in result.stdout
    assert "screen_sessions=102_m,102_c,102_web,102_w" in result.stdout
    assert "environment_changed=false" in result.stdout


def test_reload_nginx_helper_dry_run_outputs_command_contract():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "reload-nginx",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "klonet_agent_op" in result.stdout
    assert "action=reload-nginx" in result.stdout
    assert "dry_run=true" in result.stdout
    assert "test_command=nginx -t" in result.stdout
    assert "reload_command=nginx -s reload" in result.stdout
    assert "environment_changed=false" in result.stdout


def test_install_nginx_config_helper_dry_run_outputs_destination():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "install-nginx-config",
            "--dry-run",
            "--source-path",
            "/tmp/103.conf",
            "--config-name",
            "103.conf",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "klonet_agent_op" in result.stdout
    assert "action=install-nginx-config" in result.stdout
    assert "dry_run=true" in result.stdout
    assert "source_path=/tmp/103.conf" in result.stdout
    assert "destination_path=/etc/nginx/conf.d/103.conf" in result.stdout
    assert "environment_changed=false" in result.stdout


def test_install_nginx_config_helper_execute_copies_valid_conf(monkeypatch, tmp_path, capsys):
    helper = _load_helper_module()
    source = tmp_path / "103.conf"
    source.write_text("server {}", encoding="utf-8")
    copied = []

    monkeypatch.setattr(
        helper,
        "_validate_nginx_config_install_args",
        lambda source_path, config_name: "",
    )
    monkeypatch.setattr(
        helper.shutil,
        "copy2",
        lambda source_path, destination_path: copied.append((source_path, destination_path)),
    )

    code = helper.main(
        [
            "install-nginx-config",
            "--execute",
            "--source-path",
            str(source),
            "--config-name",
            "103.conf",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert copied == [(source, helper.Path("/etc/nginx/conf.d/103.conf"))]
    assert "action=install-nginx-config" in captured.out
    assert "dry_run=false" in captured.out
    assert "environment_changed=true" in captured.out


def test_install_nginx_config_helper_rejects_source_outside_staging_area():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "install-nginx-config",
            "--dry-run",
            "--source-path",
            "/etc/passwd.conf",
            "--config-name",
            "103.conf",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 2
    assert "source_path_not_allowlisted" in result.stderr
    assert "environment_changed=false" in result.stderr


def test_reload_nginx_helper_execute_tests_config_before_reload(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))

    code = helper.main(["reload-nginx", "--execute"])
    captured = capsys.readouterr()

    assert code == 0
    assert commands == [["nginx", "-t"], ["nginx", "-s", "reload"]]
    assert "action=reload-nginx" in captured.out
    assert "dry_run=false" in captured.out
    assert "nginx_test=ok" in captured.out
    assert "nginx_reload=ok" in captured.out
    assert "environment_changed=true" in captured.out


def test_reload_nginx_helper_execute_stops_when_config_test_fails(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    def fail_test(command):
        commands.append(command)
        raise subprocess.CalledProcessError(
            1,
            command,
            output="",
            stderr="nginx: configuration file /etc/nginx/nginx.conf test failed",
        )

    monkeypatch.setattr(helper, "run_checked", fail_test)

    code = helper.main(["reload-nginx", "--execute"])
    captured = capsys.readouterr()

    assert code == 1
    assert commands == [["nginx", "-t"]]
    assert "nginx_test_failed returncode=1" in captured.err
    assert "test failed" in captured.err
    assert "environment_changed=false" in captured.err


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


def test_stop_platform_screens_helper_execute_uses_fixed_screen_quits(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(
        helper,
        "existing_screen_sessions",
        lambda sessions: ["102_m", "102_c", "102_web", "102_w"],
    )

    code = helper.main(
        [
            "stop-platform-screens",
            "--execute",
            "--platform",
            "102",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert commands == [
        ["screen", "-S", "102_m", "-X", "quit"],
        ["screen", "-S", "102_c", "-X", "quit"],
        ["screen", "-S", "102_web", "-X", "quit"],
        ["screen", "-S", "102_w", "-X", "quit"],
    ]
    assert "dry_run=false" in captured.out
    assert "environment_changed=true" in captured.out


def test_stop_screen_component_helper_execute_uses_fixed_screen_quit(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: ["102_m"])

    code = helper.main(
        [
            "stop-screen-component",
            "--execute",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert commands == [["screen", "-S", "102_m", "-X", "quit"]]
    assert "dry_run=false" in captured.out
    assert "environment_changed=true" in captured.out


def test_stop_screen_component_helper_execute_rejects_missing_screen(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: [])

    code = helper.main(
        [
            "stop-screen-component",
            "--execute",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert commands == []
    assert "screen_session_not_found=102_m" in captured.err
    assert "environment_changed=false" in captured.err


def test_stop_platform_screens_helper_execute_rejects_when_no_platform_screens_exist(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: [])

    code = helper.main(
        [
            "stop-platform-screens",
            "--execute",
            "--platform",
            "102",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert commands == []
    assert "screen_session_not_found=102_m,102_c,102_web,102_w" in captured.err
    assert "environment_changed=false" in captured.err


def test_stop_platform_screens_helper_execute_stops_only_existing_platform_screens(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: ["102_m", "102_w"])

    code = helper.main(
        [
            "stop-platform-screens",
            "--execute",
            "--platform",
            "102",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert commands == [
        ["screen", "-S", "102_m", "-X", "quit"],
        ["screen", "-S", "102_w", "-X", "quit"],
    ]
    assert "screen_sessions=102_m,102_c,102_web,102_w" in captured.out
    assert "environment_changed=true" in captured.out


def test_start_platform_screens_helper_execute_uses_fixed_screen_templates(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: [])
    monkeypatch.setattr(helper, "project_entry_files_missing", lambda project_root: [])
    monkeypatch.setattr(helper, "configured_ports", lambda project_root: [5000, 5001])
    monkeypatch.setattr(helper, "listening_ports", lambda ports: [])

    code = helper.main(
        [
            "start-platform-screens",
            "--execute",
            "--platform",
            "103",
            "--project-root",
            "/home/adminis/lht/103_project/vemu_uestc",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert commands == [
        [
            "screen",
            "-dmS",
            "103_m",
            "bash",
            "-lc",
            "cd /home/adminis/lht/103_project/vemu_uestc && /usr/local/bin/gunicorn -c gun.py master_main:flask_app",
        ],
        [
            "screen",
            "-dmS",
            "103_c",
            "bash",
            "-lc",
            "cd /home/adminis/lht/103_project/vemu_uestc && /usr/local/bin/celery -A celery_worker.celery worker --loglevel=info",
        ],
        [
            "screen",
            "-dmS",
            "103_web",
            "bash",
            "-lc",
            "cd /home/adminis/lht/103_project/vemu_uestc && /usr/local/python3/bin/python3.8 web_terminal_main.py",
        ],
        [
            "screen",
            "-dmS",
            "103_w",
            "bash",
            "-lc",
            "cd /home/adminis/lht/103_project/vemu_uestc && /usr/local/bin/gunicorn -c worker_gun.py worker_main:flask_app",
        ],
    ]
    assert "dry_run=false" in captured.out
    assert "environment_changed=true" in captured.out


def test_start_platform_screens_helper_execute_rejects_existing_screen_session(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(
        helper,
        "existing_screen_sessions",
        lambda sessions: ["103_m"],
        raising=False,
    )
    monkeypatch.setattr(helper, "project_entry_files_missing", lambda project_root: [])
    monkeypatch.setattr(helper, "configured_ports", lambda project_root: [5000, 5001])
    monkeypatch.setattr(helper, "listening_ports", lambda ports: [])

    code = helper.main(
        [
            "start-platform-screens",
            "--execute",
            "--platform",
            "103",
            "--project-root",
            "/home/adminis/lht/103_project/vemu_uestc",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert commands == []
    assert "screen_session_already_exists=103_m" in captured.err
    assert "environment_changed=false" in captured.err


def test_start_platform_screens_helper_execute_rejects_missing_project_entry_files(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: [])
    monkeypatch.setattr(
        helper,
        "project_entry_files_missing",
        lambda project_root: ["master_main.py", "worker_main.py"],
    )
    monkeypatch.setattr(helper, "configured_ports", lambda project_root: [5000, 5001])
    monkeypatch.setattr(helper, "listening_ports", lambda ports: [])

    code = helper.main(
        [
            "start-platform-screens",
            "--execute",
            "--platform",
            "103",
            "--project-root",
            "/home/adminis/lht/103_project/vemu_uestc",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert commands == []
    assert "missing_project_entry_files=master_main.py,worker_main.py" in captured.err
    assert "environment_changed=false" in captured.err


def test_start_platform_screens_helper_execute_rejects_listening_config_port(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: [])
    monkeypatch.setattr(helper, "project_entry_files_missing", lambda project_root: [])
    monkeypatch.setattr(helper, "configured_ports", lambda project_root: [5000, 5045])
    monkeypatch.setattr(helper, "listening_ports", lambda ports: [5045])

    code = helper.main(
        [
            "start-platform-screens",
            "--execute",
            "--platform",
            "103",
            "--project-root",
            "/home/adminis/lht/103_project/vemu_uestc",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert commands == []
    assert "port_already_listening=5045" in captured.err
    assert "environment_changed=false" in captured.err


def test_start_platform_screens_helper_execute_rejects_missing_config_ports(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: [])
    monkeypatch.setattr(helper, "project_entry_files_missing", lambda project_root: [])
    monkeypatch.setattr(helper, "configured_ports", lambda project_root: [])
    monkeypatch.setattr(helper, "listening_ports", lambda ports: [])

    code = helper.main(
        [
            "start-platform-screens",
            "--execute",
            "--platform",
            "103",
            "--project-root",
            "/home/adminis/lht/103_project/vemu_uestc",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert commands == []
    assert "missing_config_ports=vemu_config/config.py" in captured.err
    assert "environment_changed=false" in captured.err


def test_configured_ports_reads_vemu_config_ports(tmp_path):
    helper = _load_helper_module()
    config_dir = tmp_path / "vemu_config"
    config_dir.mkdir()
    (config_dir / "config.py").write_text(
        "\n".join(
            [
                "master_port = 5000",
                "worker_port='5001'",
                "public_port = 8080",
                "web_terminal_port = 5045",
                "unused_port = 6000",
            ]
        ),
        encoding="utf-8",
    )

    assert helper.configured_ports(str(tmp_path)) == [5000, 5001, 8080, 5045]


def test_extract_archive_helper_execute_extracts_safe_tar_members(tmp_path, capsys):
    import tarfile

    helper = _load_helper_module()
    payload = tmp_path / "base_requ_setup.sh"
    payload.write_text("# setup\n", encoding="utf-8")
    archive = tmp_path / "vemu_install_2024_12_5.tar"
    with tarfile.open(archive, "w") as handle:
        handle.add(payload, arcname="vemu_install_new_gen/base_requ_setup.sh")
    destination = tmp_path / "root"

    code = helper.main(
        [
            "extract-archive",
            "--execute",
            "--archive-path",
            str(archive),
            "--destination-dir",
            str(destination),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert (destination / "vemu_install_new_gen" / "base_requ_setup.sh").read_text(encoding="utf-8") == "# setup\n"
    assert "action=extract-archive" in captured.out
    assert "dry_run=false" in captured.out
    assert "environment_changed=true" in captured.out


def test_run_install_script_helper_execute_uses_allowlisted_command(monkeypatch, tmp_path, capsys):
    helper = _load_helper_module()
    commands = []
    script_dir = tmp_path / "vemu_install_new_gen"
    script_dir.mkdir()
    (script_dir / "base_requ_setup.sh").write_text("# setup\n", encoding="utf-8")

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))

    code = helper.main(
        [
            "run-install-script",
            "--execute",
            "--script-dir",
            str(script_dir),
            "--script-name",
            "base_requ_setup.sh",
            "--script-args",
            "NORMAL",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert commands == [
        [
            "bash",
            "-lc",
            f"cd {shlex.quote(str(script_dir))} && bash ./base_requ_setup.sh NORMAL",
        ]
    ]
    assert "action=run-install-script" in captured.out
    assert "script_name=base_requ_setup.sh" in captured.out
    assert "environment_changed=true" in captured.out


def test_restart_screen_component_helper_execute_uses_fixed_screen_templates(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: ["102_m"])
    monkeypatch.setattr(helper, "project_entry_files_missing", lambda project_root: [])

    code = helper.main(
        [
            "restart-screen-component",
            "--execute",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
            "--project-root",
            "/home/adminis/lht/102_project",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert commands == [
        ["screen", "-S", "102_m", "-X", "quit"],
        [
            "screen",
            "-dmS",
            "102_m",
            "bash",
            "-lc",
            "cd /home/adminis/lht/102_project && /usr/local/bin/gunicorn -c gun.py master_main:flask_app",
        ],
    ]
    assert "dry_run=false" in captured.out
    assert "environment_changed=true" in captured.out


def test_restart_screen_component_helper_execute_reports_command_failure(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    def fail_second_command(command):
        commands.append(command)
        if len(commands) == 2:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(helper, "run_checked", fail_second_command)
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: ["102_m"])
    monkeypatch.setattr(helper, "project_entry_files_missing", lambda project_root: [])

    code = helper.main(
        [
            "restart-screen-component",
            "--execute",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
            "--project-root",
            "/home/adminis/lht/102_project",
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert len(commands) == 2
    assert "error=command_failed" in captured.err
    assert "failed_command=screen -dmS 102_m" in captured.err
    assert "returncode=1" in captured.err
    assert "environment_changed=unknown" in captured.err
    assert "Traceback" not in captured.err


def test_restart_web_terminal_helper_uses_server_python_path(monkeypatch):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: ["102_web"])
    monkeypatch.setattr(helper, "project_entry_files_missing", lambda project_root: [])

    code = helper.main(
        [
            "restart-screen-component",
            "--execute",
            "--platform",
            "102",
            "--component",
            "web_terminal",
            "--screen",
            "102_web",
            "--project-root",
            "/home/adminis/lht/102_project",
        ]
    )

    assert code == 0
    assert commands[1] == [
        "screen",
        "-dmS",
        "102_web",
        "bash",
        "-lc",
        "cd /home/adminis/lht/102_project && /usr/local/python3/bin/python3.8 web_terminal_main.py",
    ]


def test_restart_screen_component_helper_execute_rejects_missing_project_entry_files(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: ["102_m"])
    monkeypatch.setattr(
        helper,
        "project_entry_files_missing",
        lambda project_root: ["master_main.py"],
    )

    code = helper.main(
        [
            "restart-screen-component",
            "--execute",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
            "--project-root",
            "/home/adminis/lht/102_project",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert commands == []
    assert "missing_project_entry_files=master_main.py" in captured.err
    assert "environment_changed=false" in captured.err


def test_restart_screen_component_helper_execute_rejects_missing_screen(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
    monkeypatch.setattr(helper, "existing_screen_sessions", lambda sessions: [])
    monkeypatch.setattr(helper, "project_entry_files_missing", lambda project_root: [])

    code = helper.main(
        [
            "restart-screen-component",
            "--execute",
            "--platform",
            "102",
            "--component",
            "master",
            "--screen",
            "102_m",
            "--project-root",
            "/home/adminis/lht/102_project",
        ]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert commands == []
    assert "screen_session_not_found=102_m" in captured.err
    assert "environment_changed=false" in captured.err


def test_restart_screen_component_helper_rejects_screen_platform_mismatch():
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
            "lht_m",
            "--project-root",
            "/home/adminis/lht/102_project",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 2
    assert "screen_session_does_not_match_platform" in result.stderr
    assert "environment_changed=false" in result.stderr


def test_restart_screen_component_helper_rejects_shell_metacharacters_in_project_root():
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
            "/home/adminis/lht/102_project;touch /tmp/pwned",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 2
    assert "invalid_project_root=" in result.stderr
    assert "environment_changed=false" in result.stderr


def _load_helper_module():
    loader = importlib.machinery.SourceFileLoader("klonet_agent_op_helper", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module
