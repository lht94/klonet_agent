"""Server-side Ops helper contract tests."""

import importlib.machinery
import importlib.util
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


def test_restart_screen_component_helper_execute_uses_fixed_screen_templates(monkeypatch, capsys):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
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


def test_restart_web_terminal_helper_uses_server_python_path(monkeypatch):
    helper = _load_helper_module()
    commands = []

    monkeypatch.setattr(helper, "run_checked", lambda command: commands.append(command))
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
