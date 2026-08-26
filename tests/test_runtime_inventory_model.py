from types import SimpleNamespace
import base64
import json


def _bundle(output):
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    bundle = EvidenceBundle(goal="runtime")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"), output,
    ))
    return bundle


def _inventory_output():
    return "\n".join([
        "inspect_running_platforms",
        "runtime_candidate_count=3",
        "healthy_count=2",
        "abnormal_count=1",
        "code_only_count=1",
        "platform=102 project_root=/home/klonet-agent/102 roles=master,worker "
        "pids=10,11 backend_status=healthy missing_roles=none "
        "configured_ports=master_port:27694,worker_port:27695 "
        "master_port=27694 master_endpoint=healthy worker_port=27695 worker_endpoint=healthy",
        "platform=v4e2e project_root=/home/lzl/klonet_v4_e2e roles=celery,master,worker "
        "pids=20,21 backend_status=healthy missing_roles=none "
        "configured_ports=master_port:47001,worker_port:47002,web_terminal_port:47003 "
        "master_port=47001 master_endpoint=healthy worker_port=47002 worker_endpoint=healthy",
        "platform=test project_root=/home/lzl/test/vemu_uestc roles=master "
        "pids=30 backend_status=abnormal missing_roles=worker "
        "configured_ports=master_port:45554,worker_port:45555 "
        "master_port=45554 master_endpoint=healthy worker_port=45555 worker_endpoint=not_checked",
        "code_only_root=/srv/code-only",
        "environment unchanged",
    ])


def test_runtime_inventory_parses_all_rows_without_synthesis_truncation():
    from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory

    inventory = RuntimeInventory.from_bundle(_bundle(_inventory_output()))

    assert inventory.complete
    assert len(inventory.instances) == 3
    assert [item.project_root for item in inventory.healthy] == [
        "/home/klonet-agent/102", "/home/lzl/klonet_v4_e2e",
    ]
    assert inventory.instances[1].configured_ports["master_port"] == 47001
    assert inventory.code_only_roots == ("/srv/code-only",)


def test_runtime_inventory_parses_authoritative_role_listener_binding():
    from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory

    encoded = base64.urlsafe_b64encode(json.dumps({
        "worker": {
            "role": "worker",
            "configured_port": 45552,
            "status": "runtime_conflict",
            "listener_pid": 220,
            "listener_pgid": 220,
            "listener_pids": [220, 221],
            "observed_role": "worker",
            "runtime_root": "/srv/simulation/worker101",
            "code_root": "/home/lzl/vemu_uestc",
        }
    }).encode("utf-8")).decode("ascii")
    output = "\n".join([
        "runtime_candidate_count=1", "healthy_count=0", "abnormal_count=1",
        "code_only_count=0",
        "platform=vemu project_root=/home/lzl/vemu_uestc roles=worker "
        "backend_status=abnormal configured_ports=worker_port:45552 "
        "role_bindings_b64=" + encoded,
    ])

    instance = RuntimeInventory.from_bundle(_bundle(output)).instances[0]
    binding = instance.role_binding("worker")

    assert binding is not None
    assert binding.status == "runtime_conflict"
    assert binding.listener_pgid == 220
    assert binding.listener_pids == (220, 221)
    assert binding.runtime_root == "/srv/simulation/worker101"


def test_runtime_inventory_overlays_later_port_owner_evidence():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory

    encoded = base64.urlsafe_b64encode(json.dumps({
        "worker": {
            "role": "worker", "configured_port": 45552,
            "status": "owner_unavailable",
        }
    }).encode("utf-8")).decode("ascii")
    bundle = EvidenceBundle(goal="runtime")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        "\n".join([
            "runtime_candidate_count=1", "healthy_count=0", "abnormal_count=1",
            "code_only_count=0",
            "platform=vemu project_root=/home/lzl/vemu_uestc roles=worker "
            "backend_status=abnormal configured_ports=worker_port:45552 "
            "role_bindings_b64=" + encoded,
        ]),
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("port_owner", {"ports": [45552]}, "owner"),
        "- port_owner: detected - port=45552 pid=220 tree_root_pid=220 "
        "pgid=220 cmd=/opt/env/bin/python3.8 -m gunicorn --pid /srv/docker_sim/worker101.pid "
        "-c /home/lzl/vemu_uestc/mains/worker_gun.py "
        "worker_main:flask_app cwd=/srv/simulation/worker101",
    ))

    binding = RuntimeInventory.from_bundle(bundle).instances[0].role_binding("worker")

    assert binding is not None
    assert binding.status == "runtime_conflict"
    assert binding.listener_pid == 220
    assert binding.runtime_root == "/srv/simulation/worker101"
    assert binding.code_root == "/home/lzl/vemu_uestc"


def test_restart_planner_reports_external_listener_instead_of_guessing_pid():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    encoded = base64.urlsafe_b64encode(json.dumps({
        "worker": {
            "role": "worker", "configured_port": 45552,
            "status": "runtime_conflict", "listener_pid": 220,
            "listener_pgid": 220, "listener_pids": [220, 221],
            "observed_role": "worker",
            "runtime_root": "/srv/simulation/worker101",
            "code_root": "/home/lzl/vemu_uestc",
        }
    }).encode("utf-8")).decode("ascii")
    bundle = _bundle("\n".join([
        "runtime_candidate_count=1", "healthy_count=0", "abnormal_count=1",
        "code_only_count=0",
        "platform=vemu project_root=/home/lzl/vemu_uestc roles=worker "
        "backend_status=abnormal configured_ports=worker_port:45552 "
        "worker_port=45552 role_bindings_b64=" + encoded,
    ]))

    result = ChangePlannerAgent._deterministic_runtime_restart(
        "重启 vemu worker",
        bundle,
        intent_context={
            "operation": "restart", "scope": "component",
            "components": ["worker"],
            "resolved_project_root": "/home/lzl/vemu_uestc",
        },
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert "different runtime" in result["reason"]
    assert "修改 /home/lzl/vemu_uestc 的 worker" in result["missing_decisions"][0]
    assert "本次跳过该角色" in result["missing_decisions"][0]
    assert "不会停止当前占用者" in result["missing_decisions"][0]


def test_restart_planner_conflict_reassigns_only_after_checked_free_port():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceRecord, ProbeRequest,
    )

    encoded = base64.urlsafe_b64encode(json.dumps({
        "worker": {
            "role": "worker", "configured_port": 45552,
            "status": "runtime_conflict", "listener_pid": 220,
            "listener_pgid": 220, "listener_pids": [220],
            "observed_role": "worker", "runtime_root": "/srv/simulation/worker101",
            "code_root": "/home/lzl/vemu_uestc",
        }
    }).encode("utf-8")).decode("ascii")
    bundle = _bundle("\n".join([
        "runtime_candidate_count=1", "healthy_count=0", "abnormal_count=1",
        "code_only_count=0",
        "platform=vemu project_root=/home/lzl/vemu_uestc roles=worker "
        "worker_identities=220:0:/opt/python3.8 backend_status=abnormal "
        "configured_ports=worker_port:45552 worker_port=45552 "
        "role_bindings_b64=" + encoded,
    ]))
    context = {
        "operation": "restart", "scope": "component", "components": ["worker"],
        "resolved_project_root": "/home/lzl/vemu_uestc",
        "decision_history": [
            "保留当前占用者，修改 /home/lzl/vemu_uestc 的 worker 端口后继续"
        ],
    }

    missing = ChangePlannerAgent._deterministic_runtime_restart(
        "重启 vemu worker", bundle, intent_context=context,
    )

    assert missing is not None and missing["status"] == "need_evidence"
    request = missing["probe_requests"][0]
    assert request["probe"] == "ports"
    replacement = request["args"]["ports"][0]
    assert replacement != 45552

    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("ports", request["args"], "candidates"),
        "inspect_ports\nno matching listeners",
    ))
    ready = ChangePlannerAgent._deterministic_runtime_restart(
        "重启 vemu worker", bundle, intent_context=context,
    )

    assert ready is not None and ready["status"] == "ready"
    change = ready["changes"][0]
    assert "worker:45552→%s" % replacement in change["objective"]
    assert "保留冲突端口的当前占用者" in change["objective"]
    assert any(
        item["name"] == "worker_port" and item["value"] == replacement
        for item in ready["resources"]
    )
    assert any(
        item["name"] == "old_worker_port" and item["value"] == 45552
        for item in ready["resources"]
    )


def test_restart_planner_conflict_can_skip_role_without_stopping_owner():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    encoded = base64.urlsafe_b64encode(json.dumps({
        "master": {
            "role": "master", "configured_port": 45551,
            "status": "confirmed", "listener_pid": 110,
            "listener_pgid": 110, "listener_pids": [110],
            "observed_role": "master", "runtime_root": "/home/lzl/vemu_uestc",
            "code_root": "/home/lzl/vemu_uestc",
        },
        "worker": {
            "role": "worker", "configured_port": 45552,
            "status": "runtime_conflict", "listener_pid": 220,
            "listener_pgid": 220, "listener_pids": [220],
            "observed_role": "worker", "runtime_root": "/srv/simulation/worker101",
            "code_root": "/home/lzl/vemu_uestc",
        }
    }).encode("utf-8")).decode("ascii")
    bundle = _bundle("\n".join([
        "runtime_candidate_count=1", "healthy_count=0", "abnormal_count=1",
        "code_only_count=0",
        "platform=vemu project_root=/home/lzl/vemu_uestc roles=master,worker "
        "master_identities=110:1000:/opt/python3.8 "
        "worker_identities=220:0:/opt/python3.8 backend_status=abnormal "
        "configured_ports=master_port:45551,worker_port:45552 "
        "master_port=45551 worker_port=45552 role_bindings_b64=" + encoded,
    ]))

    ready = ChangePlannerAgent._deterministic_runtime_restart(
        "重启 vemu master 和 worker",
        bundle,
        intent_context={
            "operation": "restart", "scope": "platform",
            "resolved_project_root": "/home/lzl/vemu_uestc",
            "decision_history": [
                "保留 45552 当前占用者，本次跳过 /home/lzl/vemu_uestc worker"
            ],
        },
    )

    assert ready is not None and ready["status"] == "ready"
    assert "master" in ready["changes"][0]["objective"]
    assert "worker" not in ready["changes"][0]["objective"]


def test_runtime_inventory_matches_alias_numeric_id_and_exact_roots():
    from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory

    inventory = RuntimeInventory.from_bundle(_bundle(_inventory_output()))

    assert inventory.matching("v4_e2e 平台正常吗")[0].project_root == "/home/lzl/klonet_v4_e2e"
    assert inventory.matching("102 没有 Screen 是不是没启动")[0].platform == "102"
    assert {item.project_root for item in inventory.matching(
        "/home/lzl/klonet_v4_e2e 和 /home/lzl/test/vemu_uestc 是不是一个实例"
    )} == {"/home/lzl/klonet_v4_e2e", "/home/lzl/test/vemu_uestc"}


def test_runtime_response_uses_complete_bundle_not_truncated_conclusion():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion
    from klonet_agent.ops.privileged.workflow.response import ResponseAgent

    rendered = ResponseAgent(None).render_readonly(
        "有多少个正常运行的平台？",
        EvidenceConclusion(),
        evidence_bundle=_bundle(_inventory_output()),
    )

    assert "2 个正常，1 个异常" in rendered
    assert "/home/klonet-agent/102" in rendered
    assert "/home/lzl/klonet_v4_e2e" in rendered
    assert "Master：健康检查通过，端口 47001" in rendered
    assert "运行异常" in rendered
    assert "仅发现代码、没有后端运行证据" in rendered


def test_runtime_inventory_scope_does_not_depend_on_enumeration_keywords():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion
    from klonet_agent.ops.privileged.workflow.response import ResponseAgent

    rendered = ResponseAgent(None).render_readonly(
        "盘点当前 Klonet 平台运行情况",
        EvidenceConclusion(),
        evidence_bundle=_bundle(_inventory_output()),
    )

    assert "当前发现 3 个有后端运行证据" in rendered
    assert "/home/klonet-agent/102" in rendered
    assert "/home/lzl/klonet_v4_e2e" in rendered


def test_explicit_instance_runtime_goal_stays_single_target():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion
    from klonet_agent.ops.privileged.workflow.response import ResponseAgent

    rendered = ResponseAgent(None).render_readonly(
        "查看 v4e2e 是否在运行",
        EvidenceConclusion(),
        evidence_bundle=_bundle(_inventory_output()),
    )

    assert "实例 `v4e2e`" in rendered
    assert "当前发现 3 个" not in rendered
    assert "/home/klonet-agent/102" not in rendered


def test_runtime_response_settles_screen_and_root_404_without_extra_diagnosis():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion
    from klonet_agent.ops.privileged.workflow.response import ResponseAgent

    agent = ResponseAgent(None)
    screen = agent.render_readonly(
        "102 没有 Screen，是不是说明它没启动？",
        EvidenceConclusion(), evidence_bundle=_bundle(_inventory_output()),
    )
    endpoint = agent.render_readonly(
        "v4_e2e 访问 `/` 返回 404，是不是后端挂了？",
        EvidenceConclusion(), evidence_bundle=_bundle(_inventory_output()),
    )

    assert "Screen 只是启动方式证据" in screen
    assert "后端状态：healthy" in screen
    assert "`/server_health/`" in endpoint
    assert "后端状态：healthy" in endpoint


def test_goal_verifier_accepts_complete_inventory_without_calling_llm():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion

    class NeverLLM:
        def complete(self, **kwargs):
            raise AssertionError("complete runtime inventory must be deterministic")

    outcome = PrivilegedVerifierAgent(NeverLLM()).verify_goal(
        "v4_e2e 平台现在正常吗？",
        _bundle(_inventory_output()),
        EvidenceConclusion(),
        goal_kind="health_check",
    )

    assert outcome.status == "achieved"


def test_discovery_stops_after_inventory_when_it_answers_runtime_question():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    class NeverLLM:
        def complete(self, **kwargs):
            raise AssertionError("complete inventory must stop general discovery")

    agent = DiscoveryAgent(
        NeverLLM(),
        probe_runner=lambda requests: _inventory_output(),
    )

    bundle = agent.collect("v4_e2e 访问 `/` 返回 404，是不是后端挂了？")

    assert [item.request.probe for item in bundle.records] == ["running_platforms"]


def test_typed_runtime_probe_precedes_classifier_generic_screen_command():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    generic_calls = []
    agent = DiscoveryAgent(
        None,
        probe_runner=lambda requests: _inventory_output(),
        readonly_command_runner=lambda command: generic_calls.append(command) or "screen",
    )

    bundle = agent.collect(
        "102 没有 Screen，是不是说明它没启动？",
        command="screen -ls",
    )

    assert [item.request.probe for item in bundle.records] == ["running_platforms"]
    assert generic_calls == []


def test_process_role_filter_rejects_shell_that_only_mentions_celery():
    from klonet_agent.tools.environment import _is_runtime_process_executable

    assert not _is_runtime_process_executable(
        "/bin/bash", "bash -lc prompts=('Master Worker Celery')",
    )
    assert _is_runtime_process_executable(
        "/usr/bin/python3.8", "python3.8 -m celery -A worker worker",
    )


def test_complete_runtime_inventory_is_not_truncated_by_probe_transport():
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    large = _inventory_output() + "\n" + ("x" * 9000)
    builder = PrivilegedPlanContextBuilder()
    builder._probe_cache = {}
    original = __import__(
        "klonet_agent.ops.privileged.context", fromlist=["_RECOVERY_PROBES"]
    )._RECOVERY_PROBES["running_platforms"]
    module = __import__(
        "klonet_agent.ops.privileged.context", fromlist=["_RECOVERY_PROBES"]
    )
    module._RECOVERY_PROBES["running_platforms"] = lambda args: large
    try:
        rendered = builder.run_recovery_diagnostics([{
            "probe": "running_platforms", "args": {}, "purpose": "inventory",
        }])
    finally:
        module._RECOVERY_PROBES["running_platforms"] = original

    assert len(rendered) > 9000
    assert "grounded context truncated" not in rendered
    assert "code_only_root=/srv/code-only" in rendered


def test_runtime_inventory_cannot_complete_causal_goal_without_causal_evidence():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion

    outcome = PrivilegedVerifierAgent(None).verify_goal(
        "102 没有 Screen，是不是说明它没启动？",
        _bundle(_inventory_output()), EvidenceConclusion(),
        goal_kind="causal_diagnosis",
    )

    assert outcome.status == "blocked"


def test_runtime_inventory_resolves_project_root_by_exact_identity():
    from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory

    inventory = RuntimeInventory.from_bundle(_bundle(
        "\n".join([
            "platform=parent project_root=/home/lzl backend_status=abnormal",
            "platform=test project_root=/home/lzl/test/vemu_uestc "
            "backend_status=healthy",
        ])
    ))

    assert inventory.instance_for_root("/home/lzl/").platform == "parent"
    assert inventory.instance_for_root("/home/lzl/test/vemu_uestc").platform == "test"
    assert inventory.instance_for_root("/home/lzl/test") is None
