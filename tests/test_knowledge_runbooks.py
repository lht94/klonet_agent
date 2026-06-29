"""Contract tests for the operator-facing Klonet runbooks."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_RUNBOOK = ROOT / "knowledge" / "klonet" / "ops" / "environment_setup.md"
STARTUP_RUNBOOK = ROOT / "knowledge" / "klonet" / "ops" / "startup_shutdown.md"
CURRENT_SERVER_STARTUP_PATH = (
    ROOT / "knowledge" / "klonet" / "ops" / "current_server_startup_path.md"
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
    assert "不是标准" in text or "不要使用" in text


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


def test_startup_runbook_uses_current_server_python_path_for_backend():
    text = _read(CURRENT_SERVER_STARTUP_PATH)

    for expected in (
        "screen -S 103_m",
        "screen -S 103_c",
        "screen -S 103_web",
        "screen -S 103_w",
        "sudo /usr/local/python3/bin/gunicorn -c gun.py master_main:flask_app",
        "sudo /usr/local/python3/bin/celery -A celery_worker.celery worker --loglevel=info",
        "sudo /usr/local/python3/bin/python3.8 web_terminal_main.py",
        "sudo /usr/local/python3/bin/gunicorn -c worker_gun.py worker_main:flask_app",
    ):
        assert expected in text

    for expected in (
        "不要把历史文档里的 `/usr/local/bin/gunicorn`",
        "以本条目为准",
        "`/usr/local/bin/redis-server` 只是 Redis 独立服务",
    ):
        assert expected in text


def test_startup_runbook_requires_current_machine_path_verification():
    text = _read(STARTUP_RUNBOOK)

    for expected in (
        "command -v gunicorn",
        "command -v celery",
        "command -v python3.8",
        "只执行当前机器实际存在",
        "不要混用 `/usr/local/bin/` 和 `/usr/local/python3/bin/`",
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
    assert "按需" in context or "仅当" in context


def test_runbooks_do_not_contain_environment_specific_ipv4_addresses():
    text = _read(ENVIRONMENT_RUNBOOK) + "\n" + _read(STARTUP_RUNBOOK)
    addresses = set(
        re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text)
    )

    assert addresses <= {"127.0.0.1"}
