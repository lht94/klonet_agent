"""Ops-specific routing and slot extraction tests."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_ops_route_extracts_port_conflict_slots():
    from klonet_agent.ops.routing import route_ops_request

    route = route_ops_request(
        "我在 `/home/adminis/lht/102_project` 里再次启动 `web_terminal_main.py`，"
        "报 address already in use。请精确确认占用 5045 的 PID、命令和 cwd；"
        "不要仅凭 screen 存在下结论，也不要修改环境。"
    )

    assert route.goal == "端口占用诊断"
    assert route.mode == "只读诊断"
    assert route.ports == [5045]
    assert route.paths == ["/home/adminis/lht/102_project"]
    assert route.components == ["web_terminal_main.py"]
    assert "inspect_klonet_runtime" in route.recommended_tools
    assert "code_lookup" not in route.summary()


def test_ops_route_detects_write_request_but_keeps_plan_boundary():
    from klonet_agent.ops.routing import route_ops_request

    route = route_ops_request("请直接 kill 掉 102_web 或占用 5045 的进程")

    assert route.goal == "受控操作请求"
    assert route.mode == "需要 OperationPlan"
    assert route.risk == "high"
    assert route.action == "stop"
    assert "create_ops_operation_plan" in route.recommended_tools
