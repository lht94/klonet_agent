"""Ops helper installation and sudoers contract tests."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUDOERS_TEMPLATE = PROJECT_ROOT / "scripts" / "klonet-agent-op.sudoers"
INSTALL_DOC = PROJECT_ROOT / "docs" / "ops" / "klonet-agent-op-install.md"


def test_sudoers_template_only_allows_fixed_helper_entrypoint():
    text = SUDOERS_TEMPLATE.read_text(encoding="utf-8")

    assert "/usr/local/bin/klonet-agent-op restart-screen-component --execute" in text
    assert "/usr/local/bin/klonet-agent-op stop-screen-component --execute" in text
    assert "/usr/local/bin/klonet-agent-op stop-platform-screens --execute" in text
    assert "/usr/local/bin/klonet-agent-op start-platform-screens --execute" in text
    assert "/usr/local/bin/klonet-agent-op reload-nginx --execute" in text
    assert "/usr/local/bin/klonet-agent-op extract-archive --execute" in text
    assert "/usr/local/bin/klonet-agent-op run-install-script --execute" in text
    assert "NOPASSWD:" in text
    assert "/bin/bash" not in text
    assert "/usr/bin/screen" not in text
    assert "/usr/bin/kill" not in text
    assert "/usr/bin/python" not in text
    assert "/usr/local/python3/bin/python3.8" not in text
    assert "/usr/sbin/nginx" not in text


def test_install_doc_requires_root_owned_helper_and_visudo_validation():
    text = INSTALL_DOC.read_text(encoding="utf-8")

    assert "chown root:root /usr/local/bin/klonet-agent-op" in text
    assert "chmod 0755 /usr/local/bin/klonet-agent-op" in text
    assert "visudo -cf /etc/sudoers.d/klonet-agent-op" in text
    assert "Agent 侧默认仍然 dry-run" in text
    assert "KLONET_AGENT_OPS_REAL_EXECUTION=1" in text
    assert "不要直接放行 screen、kill、bash、python、nginx" in text
    assert "sudo -n /usr/local/bin/klonet-agent-op" in text
    assert "只有运行 Agent 的专用 `klonet-agent` 账户应加入 `klonet-ops`" in text
    assert "reload-nginx --dry-run" in text
    assert "extract-archive --dry-run" in text
    assert "run-install-script --dry-run" in text


def test_install_doc_covers_dedicated_service_deployment():
    text = INSTALL_DOC.read_text(encoding="utf-8")

    assert "install-klonet-agent-service.sh" in text
    assert "sudo -u klonet-agent" in text
    assert "systemctl start klonet-agent" in text
    assert "journalctl -u klonet-agent" in text
    assert "/etc/klonet-agent/klonet-agent.env" in text
    assert "--start" in text


def test_install_doc_covers_ssh_login_account():
    text = INSTALL_DOC.read_text(encoding="utf-8")

    assert "--enable-ssh-login" in text
    assert "--set-password" in text
    assert "ssh klonet-agent@" in text
    assert "python -m klonet_agent.agent --mode mentor" in text
    assert "python -m klonet_agent.agent --mode coding" in text
    assert "python -m klonet_agent.agent --mode ops" in text
    assert "sshd -T" in text
