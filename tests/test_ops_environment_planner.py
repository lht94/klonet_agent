"""Deterministic Ops environment decision planner tests."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_platform_start_plan_skips_running_services_and_blocks_conflicts():
    from klonet_agent.ops.planner import build_ops_environment_plan

    tool_events = [
        {
            "name": "inspect_klonet_runtime",
            "args": {"checks": ["redis", "docker_containers", "ports", "screen"]},
            "result": "\n".join(
                [
                    "inspect_klonet_runtime",
                    "- redis: detected - active",
                    "- docker_containers: detected - redis_102 Up 6 days",
                    "- ports: detected - LISTEN 0 4096 0.0.0.0:12000 users:(('gunicorn',pid=11,fd=5))",
                    "- screen: detected - 103_m 102_m lht_m",
                ]
            ),
        },
        {
            "name": "read_ops_file",
            "args": {"path": "/home/adminis/lht/103_project/vemu_uestc/vemu_config/config.py"},
            "result": "\n".join(
                [
                    "read_ops_file",
                    "- /home/adminis/lht/103_project/vemu_uestc/vemu_config/config.py: detected - resolved_path=/home/adminis/lht/103_project/vemu_uestc/vemu_config/config.py",
                    "master_port = 12000",
                    "worker_port = 12001",
                ]
            ),
        },
    ]

    plan = build_ops_environment_plan(
        user_input="我怎么启动 Klonet",
        operation="platform_start",
        tool_events=tool_events,
    )

    assert "step=redis action=skip" in plan
    assert "step=docker action=skip" in plan
    assert "step=ports action=block" in plan
    assert "12000" in plan
    assert "step=gunicorn action=verify" in plan
    assert "command -v gunicorn" in plan
    assert "step=screen action=block" in plan
    assert "103_m" in plan


def test_platform_start_plan_proceeds_when_no_conflict_and_paths_verified():
    from klonet_agent.ops.planner import build_ops_environment_plan

    tool_events = [
        {
            "name": "inspect_klonet_runtime",
            "args": {},
            "result": "\n".join(
                [
                    "inspect_klonet_runtime",
                    "- redis: detected - active",
                    "- ports: detected - LISTEN 0 4096 0.0.0.0:12000",
                    "- screen: detected - 102_m lht_m",
                ]
            ),
        },
        {
            "name": "read_ops_file",
            "args": {"path": "/tmp/config.py"},
            "result": "read_ops_file\nmaster_port = 13000\nworker_port = 13001\nweb_terminal_port = 13002",
        },
        {
            "name": "inspect_ops_context",
            "args": {"sections": ["baseline"]},
            "result": "inspect_ops_context\n## baseline\n- python: detected - /usr/local/bin/gunicorn /usr/local/bin/celery Python 3.8.0",
        },
    ]

    plan = build_ops_environment_plan(
        user_input="我怎么启动 Klonet",
        operation="platform_start",
        tool_events=tool_events,
    )

    assert "step=ports action=proceed" in plan
    assert "step=gunicorn action=proceed" in plan
    assert "step=screen action=verify" in plan
