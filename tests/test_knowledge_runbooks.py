"""Contract tests for the operator-facing Klonet runbooks."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_RUNBOOK = ROOT / "knowledge" / "klonet" / "ops" / "environment_setup.md"
STARTUP_RUNBOOK = ROOT / "knowledge" / "klonet" / "ops" / "startup_shutdown.md"
MULTI_PLATFORM_STARTUP_RUNBOOK = (
    ROOT / "knowledge" / "klonet" / "ops" / "multi_platform_startup.md"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_environment_runbook_uses_the_standard_install_scripts():
    text = _read(ENVIRONMENT_RUNBOOK)

    for expected in (
        "base_requ_setup.sh NORMAL",
        "docker_service.sh",
        "/etc/docker/daemon.json",
        "systemctl daemon-reload",
        "systemctl restart docker",
    ):
        assert expected in text

    assert "DPDK" in text


def test_startup_runbook_contains_concrete_runtime_commands():
    text = _read(STARTUP_RUNBOOK)

    for expected in (
        "/usr/local/python3/bin/gunicorn",
        "/usr/local/python3/bin/celery",
        "service_begin_both/begin_redis.sh",
        "/usr/local/bin/redis-server redis.conf &",
        "VEMU2/scripts/config.js",
        "screen -r",
        "Ctrl+C",
    ):
        assert expected in text

    assert "<python_env>" not in text


def test_multi_platform_startup_runbook_is_generic_and_conflict_aware():
    text = _read(MULTI_PLATFORM_STARTUP_RUNBOOK)

    for expected in (
        "screen -ls",
        "sudo ss -lntp",
        "sudo nginx -T",
        'grep -R "proxy_pass\\|listen\\|alias" /etc/nginx',
        "screen -S <instance>_m",
        "screen -S <instance>_c",
        "screen -S <instance>_web",
        "screen -S <instance>_w",
        "cp mains/gun.py mains/master_main.py mains/celery_worker.py mains/web_terminal_main.py mains/worker_gun.py mains/worker_main.py .",
        "cd <project_root>",
        "sudo /usr/local/python3/bin/gunicorn -c gun.py master_main:flask_app",
        "sudo /usr/local/python3/bin/celery -A celery_worker.celery worker --loglevel=info",
        "sudo /usr/local/python3/bin/python3.8 web_terminal_main.py",
        "sudo /usr/local/python3/bin/gunicorn -c worker_gun.py worker_main:flask_app",
        "sudo nginx -t",
        "sudo nginx -s reload",
    ):
        assert expected in text

    for placeholder in (
        "<instance>",
        "<project_root>",
        "<master_port>",
        "<worker_port>",
        "<terminal_port>",
        "<public_port>",
        "<frontend_alias>",
    ):
        assert placeholder in text

    assert "`/usr/local/bin/redis-server`" in text
    for stale in (
        "103_project",
        "screen -S 103_m",
        "adminis Klonet",
        "cd <project_root>/mains",
        "所有后端命令都从 `<project_root>/mains` 执行",
    ):
        assert stale not in text


def test_startup_runbook_requires_current_machine_path_verification():
    text = _read(STARTUP_RUNBOOK)

    for expected in (
        "command -v gunicorn",
        "command -v celery",
        "command -v python3.8",
        "/usr/local/bin/",
        "/usr/local/python3/bin/",
    ):
        assert expected in text


def test_startup_runbook_contains_complete_nginx_template():
    text = _read(STARTUP_RUNBOOK)

    for location in (
        "location /file/dload/",
        "location /file/uload/",
        "location /reallyload/",
        "location /download/",
        "location / {",
        "location /VEMU2/",
    ):
        assert location in text

    assert "sudo vim /etc/nginx/sites-available/default" in text
    assert "sudo nginx -t" in text
    assert "sudo nginx -s reload" in text


def test_libvirt_initialization_is_conditional():
    text = _read(STARTUP_RUNBOOK)
    index = text.index("libvirt_config.sh")
    context = text[max(0, index - 300) : index + 300]

    assert "KVM" in context


def test_runbooks_do_not_contain_environment_specific_ipv4_addresses():
    text = "\n".join(
        (
            _read(ENVIRONMENT_RUNBOOK),
            _read(STARTUP_RUNBOOK),
            _read(MULTI_PLATFORM_STARTUP_RUNBOOK),
        )
    )
    addresses = set(
        re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text)
    )

    assert addresses <= {"127.0.0.1"}
