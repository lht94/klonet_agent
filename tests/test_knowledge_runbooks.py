"""Contract tests for the operator-facing Klonet runbooks."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_RUNBOOK = ROOT / "knowledge" / "klonet" / "ops" / "environment_setup.md"
STARTUP_RUNBOOK = ROOT / "knowledge" / "klonet" / "ops" / "startup_shutdown.md"


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
    assert "不是标准" in text or "不要使用" in text


def test_startup_runbook_contains_concrete_runtime_commands():
    text = _read(STARTUP_RUNBOOK)

    for expected in (
        "/usr/local/bin/gunicorn",
        "/usr/local/python3/bin/gunicorn",
        "/usr/local/bin/celery",
        "/usr/local/python3/bin/celery",
        "service_begin_both/begin_redis.sh",
        "/usr/local/bin/redis-server redis.conf &",
        "VEMU2/scripts/config.js",
        "screen -r",
        "Ctrl+C",
    ):
        assert expected in text

    assert "<python_env>" not in text


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
    assert "按需" in context or "仅当" in context


def test_runbooks_do_not_contain_environment_specific_ipv4_addresses():
    text = _read(ENVIRONMENT_RUNBOOK) + "\n" + _read(STARTUP_RUNBOOK)
    addresses = set(
        re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text)
    )

    assert addresses <= {"127.0.0.1"}
