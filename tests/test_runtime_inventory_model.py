from types import SimpleNamespace


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

    assert "正常运行实例（2）" in rendered
    assert "/home/klonet-agent/102" in rendered
    assert "/home/lzl/klonet_v4_e2e" in rendered
    assert "master_port=47001" in rendered
    assert "后端异常的运行候选（1）" in rendered
    assert "只有代码、没有后端运行证据的目录（1）" in rendered


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


def test_runtime_inventory_overrides_misclassified_causal_goal_kind():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceConclusion

    outcome = PrivilegedVerifierAgent(None).verify_goal(
        "102 没有 Screen，是不是说明它没启动？",
        _bundle(_inventory_output()), EvidenceConclusion(),
        goal_kind="causal_diagnosis",
    )

    assert outcome.status == "achieved"
