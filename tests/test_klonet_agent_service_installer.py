import os
from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVICE_TEMPLATE = PROJECT_ROOT / "scripts" / "klonet-agent.service.in"
INSTALLER = PROJECT_ROOT / "scripts" / "install-klonet-agent-service.sh"


def test_service_template_runs_as_dedicated_account():
    text = SERVICE_TEMPLATE.read_text(encoding="utf-8")

    assert "User=klonet-agent" in text
    assert "Group=klonet-agent" in text
    assert "WorkingDirectory=@PACKAGE_PARENT@" in text
    assert "EnvironmentFile=-@ENV_FILE@" in text
    assert "ExecStart=@PYTHON@ -m klonet_agent.agent --mode @MODE@" in text
    assert "--user-id @USER_ID@ --project-id @PROJECT_ID@" in text
    assert "StandardInput=null" in text
    assert "Restart=on-failure" in text


def _write_fake_command(bin_dir: Path, name: str, body: str = "exit 0") -> None:
    path = bin_dir / name
    path.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$(basename \"$0\") $*\" >> \"$KLONET_TEST_CALLS\"\n"
        f"{body}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_installer(tmp_path: Path, *extra_args: str):
    install_root = tmp_path / "root"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")

    _write_fake_command(
        bin_dir,
        "id",
        'if [[ "${1:-}" == "-u" ]]; then echo 0; exit 0; fi; exit 1',
    )
    _write_fake_command(bin_dir, "getent", "exit 2")
    for name in (
        "groupadd", "useradd", "usermod", "visudo", "systemctl", "sudo",
        "passwd", "chgrp", "chmod", "find", "chown",
    ):
        _write_fake_command(bin_dir, name)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "KLONET_INSTALL_ROOT": str(install_root),
            "KLONET_TEST_CALLS": str(calls),
        }
    )
    command = [
        "bash",
        str(INSTALLER),
        "--project-root",
        str(PROJECT_ROOT),
        "--python",
        str(Path(os.sys.executable).resolve()),
        *extra_args,
    ]
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    return result, calls.read_text(encoding="utf-8"), install_root


def test_installer_keeps_real_execution_disabled():
    text = INSTALLER.read_text(encoding="utf-8")

    assert "KLONET_AGENT_OPS_REAL_EXECUTION=1" not in text
    assert "--execute" not in text
    assert "reload-nginx --dry-run" in text


def test_installer_requires_explicit_start(tmp_path):
    result, calls, _ = _run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "systemctl enable klonet-agent.service" in calls
    assert "systemctl restart klonet-agent.service" not in calls

    result, calls, _ = _run_installer(tmp_path, "--start")
    assert result.returncode == 0, result.stderr
    assert "systemctl restart klonet-agent.service" in calls


def test_reinstall_preserves_environment_file(tmp_path):
    result, _, install_root = _run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    env_file = install_root / "etc/klonet-agent/klonet-agent.env"
    env_file.write_text("OPENAI_API_KEY=server-secret\n", encoding="utf-8")

    result, _, _ = _run_installer(tmp_path)

    assert result.returncode == 0, result.stderr
    env_text = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=server-secret\n" in env_text
    assert "TMPDIR=/home/klonet-agent/.cache/tmp\n" in env_text


def test_installer_uses_home_tmpdir_by_default(tmp_path):
    result, calls, install_root = _run_installer(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "--home-dir /home/klonet-agent" in calls
    assert "chown klonet-agent:klonet-agent" in calls
    env_file = install_root / "etc/klonet-agent/klonet-agent.env"
    assert "TMPDIR=/home/klonet-agent/.cache/tmp\n" in env_file.read_text(
        encoding="utf-8"
    )


def test_set_password_requires_ssh_login(tmp_path):
    result, calls, _ = _run_installer(tmp_path, "--set-password")

    assert result.returncode != 0
    assert "--set-password requires --enable-ssh-login" in result.stderr
    assert "passwd klonet-agent" not in calls


def test_ssh_login_installs_profile_and_sets_password_interactively(tmp_path):
    result, calls, install_root = _run_installer(
        tmp_path, "--enable-ssh-login", "--set-password"
    )

    assert result.returncode == 0, result.stderr
    assert "--shell /bin/bash klonet-agent" in calls
    assert "passwd klonet-agent" in calls
    profile = (install_root / "etc/profile.d/klonet-agent.sh").read_text(
        encoding="utf-8"
    )
    assert str(Path(os.sys.executable).resolve().parent) in profile
    assert "/etc/klonet-agent/klonet-agent.env" in profile
    assert str(PROJECT_ROOT.parent) in profile


def test_installer_contains_no_password_input_option():
    text = INSTALLER.read_text(encoding="utf-8")

    assert "--password " not in text
    assert "chpasswd" not in text
    assert 'passwd "$agent_user"' in text
