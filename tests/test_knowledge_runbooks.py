"""Contract tests for the operator-facing Klonet runbooks."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_RUNBOOK = ROOT / "knowledge" / "klonet" / "ops" / "environment_setup.md"
STARTUP_RUNBOOK = ROOT / "knowledge" / "klonet" / "ops" / "startup_shutdown.md"
MULTI_PLATFORM_STARTUP_RUNBOOK = (
    ROOT / "knowledge" / "klonet" / "ops" / "multi_platform_startup.md"
)
SOURCE_ACQUISITION_RUNBOOK = (
    ROOT / "knowledge" / "klonet" / "ops" / "source_acquisition_git.md"
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


def test_startup_runbook_documents_source_acquisition_before_runtime_start():
    text = _read(STARTUP_RUNBOOK)

    for expected in (
        "## 第零步：获取平台源码并确认项目根目录",
        "git clone <repo_url> <project_root>",
        "git -C <project_root> remote -v",
        "knowledge/klonet/ops/source_acquisition_git.md",
        "rsync",
        "scp",
        "Klonet 平台源码不从 `vemu_install_new_gen` 环境安装包中推断",
        "同时包含 `mains/` 与 `vemu_uestc/`",
    ):
        assert expected in text


def test_source_acquisition_runbook_contains_git_ssh_details_and_safety_boundaries():
    text = _read(SOURCE_ACQUISITION_RUNBOOK)

    for expected in (
        "git clone gitee:uestc-minenet/vemu_uestc.git",
        "git clone git@github.com:lht94/vemu-web.git",
        "Host gitee",
        "IdentityFile /home/<target_user>/.ssh/<gitee_private_key>",
        "chmod 700 ~/.ssh",
        "chmod 600 ~/.ssh/<gitee_private_key>",
        "ssh -T gitee",
        "git config --local user.name",
        "git remote -v",
        "git branch -vv",
        "git push --force-with-lease origin <branch>",
        "Ops Agent 在没有明确用户授权和受控计划时，不应执行",
    ):
        assert expected in text

    for sensitive in (
        "123@qq.com",
        "/home/adminis/.ssh/",
        "vemu6@192.168.1.60",
        "lzl@192.168.1.33",
        "wudx_gitee",
        "chmod 700 ~/.sshscp",
    ):
        assert sensitive not in text


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
        "优先从 Git 仓库克隆",
        "不要从 `vemu_install_new_gen` 环境安装包推断或抽取平台源码",
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


def test_startup_runbooks_do_not_mix_legacy_python_entrypoints():
    text = "\n".join((_read(STARTUP_RUNBOOK), _read(MULTI_PLATFORM_STARTUP_RUNBOOK)))

    stale_commands = (
        "cd <project_root>/mains",
        "python3 mains/master_main.py",
        "python3 mains/worker_main.py",
        "python3 mains/web_terminal_main.py",
        "python3.8 mains/master_main.py",
        "python3.8 mains/worker_main.py",
        "python3.8 mains/web_terminal_main.py",
        "sudo /usr/local/bin/gunicorn",
        "sudo /usr/local/bin/celery",
        "sudo /usr/local/bin/python3.8",
    )
    for stale in stale_commands:
        assert stale not in text


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
