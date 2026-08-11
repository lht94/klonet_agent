from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output))]
        )


def test_user_visible_probe_purpose_is_chinese_even_when_model_purpose_is_english():
    from klonet_agent.ops.privileged.context import _localized_probe_purpose

    visible = _localized_probe_purpose(
        "screen_session",
        {"session": "v4e2e_w"},
        "Confirm the v4e2e screen session state",
    )

    assert "Screen" in visible
    assert "确认" not in visible or "核对" in visible
    assert "Confirm" not in visible


def test_discovery_runs_registered_probes_and_reuses_duplicate_request():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "platform_instances",
                            "args": {},
                            "purpose": "discover candidates",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "platform_instances",
                            "args": {},
                            "purpose": "repeat must use cache",
                        },
                        {
                            "probe": "screen",
                            "args": {},
                            "purpose": "corroborate runtime",
                        },
                    ],
                }
            ),
            json.dumps({"status": "ready"}),
        ]
    )
    probe_calls = []

    def run_probes(requests):
        probe_calls.append(requests)
        return "evidence for %s" % requests[0]["probe"]

    bundle = DiscoveryAgent(llm, probe_runner=run_probes).collect(
        "检查有哪些平台"
    )

    assert [item.request.probe for item in bundle.records] == [
        "platform_instances",
        "screen",
    ]
    assert [item[0]["probe"] for item in probe_calls] == [
        "platform_instances",
        "screen",
    ]
    assert len(llm.calls) == 3


def test_discovery_marks_budget_exhaustion_instead_of_replanning_forever():
    from klonet_agent.ops.privileged.workflow.contracts import DiscoveryBudget
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {"probe": "screen", "args": {}, "purpose": "screen"}
                    ],
                }
            ),
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {"probe": "ports", "args": {}, "purpose": "ports"}
                    ],
                }
            ),
        ]
    )
    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: "ok",
        budget_factory=lambda: DiscoveryBudget(max_rounds=1),
    ).collect("inspect")

    assert bundle.budget_exhausted is True
    assert [item.request.probe for item in bundle.records] == ["screen"]
    assert len(llm.calls) == 2


def test_discovery_normalizes_screen_evidence_section_aliases_to_registered_probe():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {"probe": "screen", "args": {}, "purpose": "sessions"},
                        {
                            "probe": "screen_git_repositories",
                            "args": {},
                            "purpose": "mistaken section alias",
                        },
                    ],
                }
            ),
            json.dumps({"status": "ready"}),
        ]
    )
    calls = []
    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: calls.append(requests) or "screen evidence",
    ).collect("inspect Screen source")

    assert [item.request.probe for item in bundle.records] == ["screen"]
    assert len(calls) == 1


def test_discovery_collect_requests_adds_only_fresh_registered_evidence():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    existing = ProbeRequest("screen", {}, "existing")
    bundle = EvidenceBundle(goal="deploy")
    bundle.add(EvidenceRecord.from_probe(existing, "screen evidence"))
    calls = []
    agent = DiscoveryAgent(
        FakeLLM([]),
        probe_runner=lambda requests: calls.append(requests) or "port evidence",
    )

    returned = agent.collect_requests(
        [existing, ProbeRequest("ports", {"ports": [47001]}, "freeze port")],
        bundle,
    )

    assert returned is bundle
    assert [item.request.probe for item in bundle.records] == ["screen", "ports"]
    assert len(calls) == 1
    assert calls[0][0]["probe"] == "ports"


def test_discovery_routes_python_source_log_request_to_ops_file():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    requests = DiscoveryAgent._requests([{
        "probe": "logs",
        "args": {"path": "/srv/102/mains/master_main.py"},
        "purpose": "inspect startup entry source",
    }])

    assert requests[0].probe == "ops_file"
    assert requests[0].args == {
        "path": "/srv/102/mains/master_main.py",
        "view": "head",
        "max_chars": 20000,
    }


def test_discovery_derives_structured_git_record_from_unique_screen_source():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {"probe": "screen", "args": {}, "purpose": "source"}
                    ],
                }
            ),
            json.dumps({"status": "ready"}),
        ]
    )
    output = (
        "session=vemu_uestc_m git_roots=/home/lzl/vemu_uestc\n"
        "path=/home/lzl/vemu_uestc inside_work_tree=true revision=abc\n"
        "status=## develop...origin/develop\n"
        "remotes=origin\tgitee:example/vemu.git (fetch)"
    )
    calls = []
    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: calls.append(requests) or output,
    ).collect("use Screen prefix vemu_uestc as source")

    assert [item.request.probe for item in bundle.records] == [
        "screen",
        "git_repository",
    ]
    derived = bundle.records[1]
    assert derived.request.args == {"repository": "/home/lzl/vemu_uestc"}
    assert "derived authoritative Screen source" in derived.request.purpose
    assert len(calls) == 1


def test_discovery_preloads_running_platform_inventory_for_healthy_count_goal():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM([json.dumps({"status": "ready"})])
    calls = []
    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: calls.append(requests)
        or "inspect_running_platforms\nhealthy_count=2",
    ).collect(
        "检查当前服务器上有多少正常运行的平台，只有后端接口可用才算"
    )

    assert [item.request.probe for item in bundle.records] == ["running_platforms"]
    assert calls[0][0]["probe"] == "running_platforms"
    assert "inspect_running_platforms" in llm.calls[0]["messages"][-1]["content"]


def test_discovery_preloads_running_platform_inventory_before_repairing_abnormal_platforms():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM([json.dumps({"status": "ready"})])
    calls = []
    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: calls.append(requests)
        or (
            "inspect_running_platforms\nabnormal_count=2\n"
            "platform=vemu_uestc project_root=/srv/a backend_status=abnormal\n"
            "platform=vemu_uestc project_root=/srv/b backend_status=abnormal"
        ),
    ).collect("帮我修复后端不正常运行的平台")

    assert [item.request.probe for item in bundle.records] == ["running_platforms"]
    assert calls[0][0]["probe"] == "running_platforms"
    assert llm.calls == []


def test_named_platform_alias_continues_discovery_with_multiple_abnormal_roots():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM([json.dumps({"status": "ready"})])
    calls = []
    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: calls.append(requests)
        or (
            "inspect_running_platforms\nabnormal_count=2\n"
            "platform=102 project_root=/home/klonet-agent/102 backend_status=abnormal\n"
            "platform=klonet project_root=/home/lzl/xxy/klonet backend_status=abnormal"
        ),
    ).collect("修复 102 平台刚出现的启动异常")

    assert [item.request.probe for item in bundle.records] == [
        "running_platforms", "ops_file",
    ]
    assert len(llm.calls) == 1


def test_runtime_inventory_dynamically_grounds_followup_log_path(tmp_path, monkeypatch):
    from klonet_agent.ops.privileged import context as context_module
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    runtime = tmp_path / "102"
    logs = runtime / "logs"
    logs.mkdir(parents=True)
    log = logs / "master_gunicorn.log"
    log.write_text("RuntimeError: injected boot failure\n", encoding="utf-8")
    monkeypatch.setitem(
        context_module._RECOVERY_PROBES,
        "running_platforms",
        lambda args: (
            "inspect_running_platforms\n"
            "platform=102 project_root=%s backend_status=abnormal" % runtime
        ),
    )
    monkeypatch.setitem(
        context_module._RECOVERY_PROBES,
        "logs",
        lambda args: "read_logs\n" + Path(args["path"]).read_text(encoding="utf-8"),
    )
    builder = PrivilegedPlanContextBuilder()
    builder.begin_probe_session()

    inventory = builder.run_recovery_diagnostics([
        {"probe": "running_platforms", "args": {}, "purpose": "inventory"}
    ])
    evidence = builder.run_recovery_diagnostics([
        {"probe": "logs", "args": {"path": str(log)}, "purpose": "startup failure"}
    ])

    assert "project_root=%s" % runtime in inventory
    assert "RuntimeError: injected boot failure" in evidence
    assert "path_outside_grounded_project_roots" not in evidence

    builder.end_probe_session()
    refused = builder.run_recovery_diagnostics([
        {"probe": "logs", "args": {"path": str(log)}, "purpose": "new turn"}
    ])
    assert "path_outside_grounded_project_roots" in refused


def test_screen_and_process_evidence_ground_their_verified_project_root(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged import context as context_module
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    runtime = tmp_path / "v4e2e"
    mains = runtime / "mains"
    logs = runtime / "logs"
    mains.mkdir(parents=True)
    logs.mkdir()
    log = logs / "master.log"
    log.write_text("PermissionError: log path is not writable\n", encoding="utf-8")
    monkeypatch.setitem(
        context_module._RECOVERY_PROBES,
        "screen_session",
        lambda args: (
            "inspect_screen_session\n"
            "session=v4e2e_m cwd=%s git_roots=%s current_state=true"
            % (mains, runtime)
        ),
    )
    monkeypatch.setitem(
        context_module._RECOVERY_PROBES,
        "logs",
        lambda args: "read_logs\n" + Path(args["path"]).read_text(encoding="utf-8"),
    )
    builder = PrivilegedPlanContextBuilder()
    builder.begin_probe_session()

    builder.run_recovery_diagnostics([{
        "probe": "screen_session",
        "args": {"session": "v4e2e_m"},
        "purpose": "定位运行实例",
    }])
    evidence = builder.run_recovery_diagnostics([{
        "probe": "logs",
        "args": {"path": str(log)},
        "purpose": "读取完整异常",
    }])

    assert "PermissionError" in evidence
    assert "path_outside_grounded_project_roots" not in evidence


def test_planner_requested_log_derives_target_traceback_source_probe():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    bundle = EvidenceBundle(goal="修复 102 平台启动异常")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        "platform=102 project_root=/home/klonet-agent/102 backend_status=abnormal\n"
        "platform=other project_root=/srv/other backend_status=abnormal",
    ))
    calls = []

    def run(requests):
        request = requests[0]
        calls.append(request)
        if request["probe"] == "logs":
            return (
                'File "/home/klonet-agent/102/mains/master_main.py", line 10, in <module>\n'
                'File "/usr/lib/python3.8/importlib/__init__.py", line 1, in import_module\n'
                "RuntimeError: boot failure"
            )
        assert request["probe"] == "ops_file"
        return "read_ops_file\nraise RuntimeError('boot failure')"

    discovery = DiscoveryAgent(FakeLLM([]), probe_runner=run)
    discovery.collect_requests(
        [ProbeRequest("logs", {"path": "/home/klonet-agent/102/logs/master.log"}, "log")],
        bundle,
    )

    assert [call["probe"] for call in calls] == ["logs", "ops_file"]
    source = next(record for record in bundle.records if record.request.probe == "ops_file")
    assert source.request.args["path"] == "/home/klonet-agent/102/mains/master_main.py"


def test_selected_abnormal_master_inventory_collects_bound_process_log_and_source():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle, ProbeRequest
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    bundle = EvidenceBundle(goal="102 平台启动异常，请检查日志并修复")
    calls = []

    def run(requests):
        request = requests[0]
        calls.append(request)
        if request["probe"] == "running_platforms":
            return (
                "platform=102 project_root=/home/klonet-agent/102 "
                "backend_status=abnormal master_pids=1239000 "
                "master_endpoint=unreachable worker_endpoint=healthy"
            )
        if request["probe"] == "process_logs":
            assert request["args"] == {
                "pids": [1239000],
                "project_root": "/home/klonet-agent/102",
            }
            return (
                'File "/home/klonet-agent/102/mains/master_main.py", line 10, in <module>\n'
                "RuntimeError: boot failed"
            )
        assert request["probe"] == "ops_file"
        return "read_ops_file\nraise RuntimeError('boot failed')"

    discovery = DiscoveryAgent(FakeLLM([]), probe_runner=run)
    discovery.collect_requests(
        [ProbeRequest("running_platforms", {}, "inventory")],
        bundle,
    )

    assert [call["probe"] for call in calls] == [
        "running_platforms",
        "process_logs",
        "ops_file",
    ]


def test_selected_abnormal_master_reads_entry_when_process_log_has_no_traceback():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle, ProbeRequest
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    bundle = EvidenceBundle(goal="102 平台新增代码后无法启动，请修复")
    calls = []

    def run(requests):
        request = requests[0]
        calls.append(request)
        if request["probe"] == "running_platforms":
            return (
                "platform=102 project_root=/home/klonet-agent/102 "
                "backend_status=abnormal master_pids=1239000 "
                "master_endpoint=unreachable worker_endpoint=healthy"
            )
        if request["probe"] == "process_logs":
            return "inspect_process_logs\nno matching stdout or stderr file"
        assert request["probe"] == "ops_file"
        assert request["args"]["path"] == "/home/klonet-agent/102/mains/master_main.py"
        return "def KLONET_E2E_FAILURE():\n    raise RuntimeError('boom')"

    discovery = DiscoveryAgent(FakeLLM([]), probe_runner=run)
    discovery.collect_requests(
        [ProbeRequest("running_platforms", {}, "inventory")],
        bundle,
    )

    assert [call["probe"] for call in calls] == [
        "running_platforms", "process_logs", "ops_file",
    ]


def test_discovery_preloads_inventory_for_explicit_platform_repair_without_health_words():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM([json.dumps({"status": "ready"})])
    calls = []
    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: calls.append(requests)
        or "inspect_running_platforms\nabnormal_count=2",
    ).collect(
        "修复 /home/lzl/vemu_uestc 和 /home/lzl/test/vemu_uestc，作为两个独立平台"
    )

    assert [item.request.probe for item in bundle.records] == ["running_platforms"]
    assert calls[0][0]["probe"] == "running_platforms"


def test_discovery_preloads_inventory_when_continuing_partial_instance_recovery():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM([json.dumps({"status": "ready"})])
    calls = []
    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: calls.append(requests)
        or "inspect_running_platforms\nabnormal_count=2",
    ).collect(
        "继续完成两个 vemu_uestc 独立实例的修复；恢复后端 master 和 worker"
    )

    assert [item.request.probe for item in bundle.records] == ["running_platforms"]
    assert calls[0][0]["probe"] == "running_platforms"


def test_synthesis_repairs_unknown_evidence_reference_once():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.evidence_synthesis import EvidenceSynthesizer

    bundle = EvidenceBundle(goal="inspect")
    record = bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("screen", {}, "screen"),
            "vemu_uestc_m",
        )
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "confirmed_facts": [
                        {"text": "one platform", "evidence_refs": ["ev-invented"]}
                    ]
                }
            ),
            json.dumps(
                {
                    "confirmed_facts": [
                        {"text": "one platform", "evidence_refs": [record.evidence_id]}
                    ],
                    "uncertainties": [],
                    "missing_decisions": [],
                }
            ),
        ]
    )

    conclusion = EvidenceSynthesizer(llm).synthesize("inspect", bundle)

    assert conclusion.confirmed_facts[0].text == "one platform"
    assert len(llm.calls) == 2


def test_synthesis_includes_probe_arguments_for_instance_attribution():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.evidence_synthesis import EvidenceSynthesizer

    bundle = EvidenceBundle(goal="inspect")
    record = bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest(
                "http_endpoint",
                {"url": "http://127.0.0.1:47001/server_health/"},
                "v4e2e master health",
            ),
            "status=200 body=code=1",
        )
    )
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "confirmed_facts": [
                        {"text": "v4e2e master healthy", "evidence_refs": [record.evidence_id]}
                    ],
                    "uncertainties": [],
                    "missing_decisions": [],
                }
            )
        ]
    )

    EvidenceSynthesizer(llm).synthesize("inspect", bundle)

    payload = llm.calls[0]["messages"][1]["content"]
    assert '"args": {"url": "http://127.0.0.1:47001/server_health/"}' in payload


def test_synthesis_promotes_user_selected_screen_git_mapping_deterministically():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.evidence_synthesis import EvidenceSynthesizer

    bundle = EvidenceBundle(goal="use Screen prefix vemu_uestc as source")
    record = bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("screen", {}, "source"),
            (
                "screen_runtime\n"
                "session=vemu_uestc_m screen_pid=100 "
                "runtime_cwds=/home/lzl/vemu_uestc/mains "
                "git_roots=/home/lzl/vemu_uestc\n"
                "screen_git_repositories\ninspect_git_repository\n"
                "path=/home/lzl/vemu_uestc inside_work_tree=true "
                "revision=af418698\nstatus=## develop...origin/develop\n"
                "remotes=origin\tgitee:uestc-minenet/vemu_uestc.git (fetch)"
            ),
        )
    )
    llm = FakeLLM(
        [json.dumps({"confirmed_facts": [], "uncertainties": [], "missing_decisions": []})]
    )

    conclusion = EvidenceSynthesizer(llm).synthesize(bundle.goal, bundle)

    fact = conclusion.confirmed_facts[0]
    assert "/home/lzl/vemu_uestc" in fact.text
    assert "gitee:uestc-minenet/vemu_uestc.git" in fact.text
    assert "develop" in fact.text
    assert fact.evidence_refs == [record.evidence_id]


def test_response_fallback_reports_facts_and_uncertainty_without_llm():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceClaim,
        EvidenceConclusion,
    )
    from klonet_agent.ops.privileged.workflow.response import ResponseAgent

    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim("确认 vemu_uestc 正在运行", ["ev-1"])],
        uncertainties=[EvidenceClaim("agent102 证据不足", ["ev-2"])],
    )

    message = ResponseAgent(None).render_readonly("检查平台", conclusion)

    assert "确认 vemu_uestc 正在运行" in message
    assert "agent102 证据不足" in message
    assert "不确定" in message
