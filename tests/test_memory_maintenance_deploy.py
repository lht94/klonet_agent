"""部署工件测试（04 计划阶段 7）：systemd unit / env 样例 / compose 示例 / runbook。

全部是静态检查——部署样例的正确性（不泄密、优雅停止、最小权限）必须被
测试钉住，而不是靠 review 记性。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = PROJECT_ROOT / "deploy"
SYSTEMD = DEPLOY / "systemd"


def _read(path: Path) -> str:
    assert path.exists(), f"部署文件缺失：{path}"
    return path.read_text(encoding="utf-8")


def test_systemd_unit_exists_and_is_minimal() -> None:
    unit = _read(SYSTEMD / "klonet-memory-maintenance.service")
    # 生产入口就是 CLI 的 run 子命令。
    assert "python -m klonet_agent.memory.maintenance run" in unit
    # 机密只经 EnvironmentFile 注入，不进单元正文。
    assert "EnvironmentFile=" in unit
    assert not re.search(r"PASSWORD\s*=", unit)
    assert "postgres://" not in unit or "CHANGE_ME" not in unit
    # SIGTERM 优雅停止：宽限要显式给出。
    assert "TimeoutStopSec" in unit
    # 失败自动重启。
    assert "Restart=on-failure" in unit
    # 最小权限加固。
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit


def test_env_example_has_placeholders_not_secrets() -> None:
    env = _read(SYSTEMD / "klonet-memory-maintenance.env.example")
    assert "KLONET_AGENT_MEMORY_DSN=" in env
    assert "KLONET_AGENT_MAINTENANCE_ENABLED=true" in env
    # 占位值显式，且没有看起来像真实凭据的字符串。
    assert "CHANGE_ME" in env
    for line in env.splitlines():
        if line.startswith("KLONET_AGENT_MEMORY_DSN="):
            assert "CHANGE_ME" in line, f"DSN 行必须用占位密码：{line}"
    # 专用最小权限角色。
    assert "klonet_maint" in env


def test_docker_compose_example_exists() -> None:
    compose = _read(DEPLOY / "docker-compose.memory-maintenance.yml")
    assert "python -m klonet_agent.memory.maintenance run" in compose
    assert "env_file" in compose
    # 优雅停止的 compose 对应物。
    assert "stop_grace_period" in compose
    # 文件标注了"仅示例"。
    assert "仅作部署示例" in compose or "仅示例" in compose


def test_runbook_documents_pause_resume_rollback() -> None:
    runbook = _read(PROJECT_ROOT / "doc" / "18_memory_lifecycle_worker_runbook.md")
    for keyword in ("暂停", "恢复", "dry-run", "回滚", "ALTER COLUMN", "health"):
        assert keyword in runbook, f"runbook 缺少章节/关键词：{keyword}"


def test_no_secrets_in_any_deploy_artifact() -> None:
    """扫描全部部署工件：不允许出现形似真实凭据的字符串。"""

    artifacts = [
        SYSTEMD / "klonet-memory-maintenance.service",
        SYSTEMD / "klonet-memory-maintenance.env.example",
        DEPLOY / "docker-compose.memory-maintenance.yml",
    ]
    secretish = re.compile(
        r"postgresql://[A-Za-z0-9_]+:(?!CHANGE_ME)[A-Za-z0-9!@#$%^&*]{8,}@"
    )
    for path in artifacts:
        text = _read(path)
        assert not secretish.search(text), f"{path.name} 疑似包含真实凭据"
