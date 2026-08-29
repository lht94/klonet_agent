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


def test_invalid_fact_comparison_is_repaired_at_discovery_contract_boundary():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    invalid = json.dumps({
        "status": "need_evidence",
        "probe_requests": [{
            "probe": "ports",
            "args": {"ports": [47001]},
            "purpose": "check port",
            "required_facts": [{
                "fact_id": "fact-port-free",
                "predicate": "port.available",
                "expected": 47001,
                "comparison": "is_free",
            }],
            "subject": {"kind": "port_set", "value": [47001]},
        }],
    })
    llm = FakeLLM([invalid, json.dumps({"status": "ready"})])

    bundle = DiscoveryAgent(
        llm, probe_runner=lambda requests: "must not run",
    ).collect("inspect port")

    assert bundle.blocked_reason == ""
    assert len(llm.calls) == 2
    repair = llm.calls[1]["messages"][-1]["content"]
    assert "equals, contains, contains_all, or present" in repair


def test_registered_probe_predicate_mismatch_is_repaired_before_execution():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    mismatched = json.dumps({
        "status": "need_evidence",
        "probe_requests": [{
            "probe": "ports",
            "args": {"ports": [47001]},
            "purpose": "check port",
            "required_facts": [{
                "fact_id": "fact-port-free",
                "predicate": "port.is_free",
                "expected": 47001,
                "comparison": "contains",
            }],
            "subject": {"kind": "port_set", "value": [47001]},
        }],
    })
    llm = FakeLLM([mismatched, json.dumps({"status": "ready"})])
    calls = []

    bundle = DiscoveryAgent(
        llm, probe_runner=lambda requests: calls.append(requests) or "unused",
    ).collect("inspect port")

    assert bundle.blocked_reason == ""
    assert calls == []
    repair = llm.calls[1]["messages"][-1]["content"]
    assert "supported=port.available" in repair


def test_provider_scalar_probe_args_are_normalized_before_registered_execution():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    request = json.dumps({
        "status": "need_evidence",
        "probe_requests": [{
            "probe": "ports",
            "args": {"ports": "45553,45554"},
            "purpose": "freeze occupied ports",
            "required_facts": [{
                "fact_id": "fact-existing-ports",
                "predicate": "port.in_use",
                "expected": "45553,45554",
                "comparison": "contains_all",
            }],
        }],
    })
    llm = FakeLLM([request, json.dumps({"status": "ready"})])
    calls = []

    bundle = DiscoveryAgent(
        llm,
        probe_runner=lambda values: calls.append(values) or (
            "inspect_ports\nchecked_ports=45553,45554\n"
            "occupied_ports=45553,45554\navailable_ports=none"
        ),
    ).collect("allocate new ports")

    assert calls[0][0]["args"] == {"ports": ["45553", "45554"]}
    assert bundle.records[-1].observations[0].status == "confirmed"
    assert not any(item.request.probe == "readonly_command" for item in bundle.records)


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


def test_user_decided_source_root_is_grounded_without_relaxing_other_paths(tmp_path):
    from klonet_agent.ops.privileged.context import PrivilegedPlanContextBuilder

    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    builder = PrivilegedPlanContextBuilder()
    builder.begin_probe_session()
    builder.register_user_decided_project_root(str(source))

    accepted = builder.run_recovery_diagnostics([{
        "probe": "project_layout",
        "args": {"project_roots": [str(source)]},
        "purpose": "inspect the user-decided source",
    }])
    refused = builder.run_recovery_diagnostics([{
        "probe": "project_layout",
        "args": {"project_roots": [str(outside)]},
        "purpose": "inspect an unrelated model-proposed path",
    }])

    assert "path_outside_grounded_project_roots" not in accepted
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


def test_synthesis_transport_timeout_falls_back_without_json_repair_retry():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.evidence_synthesis import (
        EvidenceSynthesizer,
    )

    class TimeoutLLM:
        def __init__(self):
            self.calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            raise TimeoutError("provider timed out")

    bundle = EvidenceBundle(goal="inspect")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("screen", {}, "screen evidence"),
        "session=test_w status=running",
    ))
    llm = TimeoutLLM()

    conclusion = EvidenceSynthesizer(llm).synthesize(bundle.goal, bundle)

    assert llm.calls == 1
    assert conclusion is not None


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


def test_synthesis_returns_structured_confirmed_causal_chain():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.evidence_synthesis import EvidenceSynthesizer

    bundle = EvidenceBundle(goal="检查 worker 报错")
    record = bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("logs", {"path": "/srv/app/logs/error.log"}, "完整异常"),
        "FileNotFoundError: /srv/app/missing/.__error.lock",
    ))
    llm = FakeLLM([json.dumps({
        "confirmed_facts": [{
            "text": "日志目录不存在导致锁文件创建失败",
            "evidence_refs": [record.evidence_id],
        }],
        "uncertainties": [],
        "missing_decisions": [],
        "diagnosis": {
            "status": "cause_confirmed",
            "symptom": "worker 初始化报错",
            "failure_point": "日志处理器创建锁文件",
            "root_cause": "配置指向不存在的日志目录",
            "evidence_refs": [record.evidence_id],
        },
    }, ensure_ascii=False)])

    conclusion = EvidenceSynthesizer(llm).synthesize(bundle.goal, bundle)

    assert conclusion.diagnosis.status == "cause_confirmed"
    assert conclusion.diagnosis.failure_point == "日志处理器创建锁文件"
    assert conclusion.diagnosis.evidence_refs == [record.evidence_id]


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
def test_runtime_component_manifest_extends_managed_applications_but_not_shared_dependencies(tmp_path):
    import json

    from klonet_agent.tools.environment import _runtime_component_specs

    manifest = tmp_path / ".klonet"
    manifest.mkdir()
    (manifest / "runtime_components.json").write_text(json.dumps({
        "components": [{
            "name": "metrics",
            "screen_suffix": "metrics",
            "command_argv": ["/opt/python", "-m", "metrics_service"],
            "preflight_argv": ["/opt/python", "-c", "import metrics_service"],
            "ports": [47009],
            "health_checks": [{
                "checker": "port_listening", "args": {"port": 47009},
            }],
            "start_after": ["worker"],
        }, {
            "name": "redis_sidecar",
            "category": "shared_dependency",
            "screen_suffix": "redis",
            "command_argv": ["redis-server", "redis.conf"],
            "preflight_argv": ["redis-server", "--version"],
        }],
    }), encoding="utf-8")

    specs = _runtime_component_specs(tmp_path, {"metrics"})
    by_name = {item["name"]: item for item in specs}

    assert by_name["metrics"]["default_restart"] is True
    assert by_name["metrics"]["command_argv"][-1] == "metrics_service"
    assert by_name["redis_sidecar"]["category"] == "shared_dependency"
    assert {"master", "celery", "web_terminal", "worker"}.issubset(by_name)


def test_runtime_component_inventory_freezes_safe_observed_custom_argv(tmp_path):
    from klonet_agent.tools.environment import _runtime_component_specs

    rows = {"data_server": [{
        "pid": 1205,
        "ppid": 1,
        "pgid": 1205,
        "uid": 1000,
        "executable": "/opt/envs/test/bin/python3.8",
        "cwd": str(tmp_path),
        "cmd": (
            "/opt/envs/test/bin/python3.8 -m gunicorn -c "
            "data_server_gun.py data_server_main:flask_app"
        ),
        "role": "data_server",
    }]}

    specs = _runtime_component_specs(tmp_path, {"data_server"}, rows)
    data_server = next(item for item in specs if item["name"] == "data_server")

    assert data_server["default_restart"] is False
    assert data_server["discovery_status"] == "observed_runtime_contract"
    assert data_server["command_argv"] == [
        "/opt/envs/test/bin/python3.8", "-m", "gunicorn", "-c",
        "data_server_gun.py", "data_server_main:flask_app",
    ]
    assert data_server["preflight_argv"] == [
        "/opt/envs/test/bin/python3.8", "-m", "gunicorn", "--check-config",
        "-c", "data_server_gun.py", "data_server_main:flask_app",
    ]


def test_discovery_collects_reusable_klonet_knowledge_before_host_probes():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    calls = []
    progress = []
    agent = DiscoveryAgent(
        FakeLLM([json.dumps({"status": "ready"})]),
        probe_runner=lambda requests: "inspect_running_platforms\nhealthy_count=1",
        knowledge_search=lambda query, **kwargs: calls.append((query, kwargs)) or (
            "source=knowledge/klonet/ops/startup_shutdown.md\n"
            "启动前将 mains 入口文件复制到项目根目录，并从项目根目录启动 Screen"
        ),
        on_progress=progress.append,
    )

    bundle = agent.collect("帮我重启 v4_e2e 平台")

    assert bundle.records[0].request.probe == "klonet_knowledge"
    assert "startup_shutdown.md" in bundle.records[0].output
    assert calls[0][1]["task_type"] == "operation_guide"
    assert any("正在检索 Klonet 知识库" in item for item in progress)


def test_explicit_restart_stops_after_rag_and_complete_runtime_inventory():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM([])
    progress = []
    inventory = (
        "platform=v4e2e project_root=/home/lzl/klonet_v4_e2e "
        "roles=celery,master,worker configured_ports=master_port:47001,"
        "worker_port:47002,web_terminal_port:47003 "
        "component_specs_b64=eyJtYXN0ZXIiOnt9fQ== "
        "runtime_identities=10:1000:/opt/python3.8"
    )
    agent = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: inventory,
        knowledge_search=lambda query, **kwargs: (
            "retrieval_status: reliable\n"
            "- path: knowledge/klonet/ops/startup_shutdown.md\n"
            "startup_cwd=<project_root>"
        ),
        on_progress=progress.append,
    )

    bundle = agent.collect("帮我重启 v4_e2e 平台")

    assert [item.request.probe for item in bundle.records] == [
        "klonet_knowledge", "running_platforms"
    ]
    assert any("reliable；startup_shutdown.md" in item for item in progress)


def test_binding_context_preserves_same_knowledge_evidence_id():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow

    bundle = EvidenceBundle(goal="重启平台")
    record = bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("klonet_knowledge", {"query": "重启平台"}, "流程知识"),
        "source=startup_shutdown.md\n从项目根目录启动 Screen",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "运行事实"),
        "platform=v4e2e project_root=/srv/v4e2e",
    ))

    context = MutationWorkflow._binding_context(bundle)

    assert record.evidence_id in context.knowledge_evidence
    assert "startup_shutdown.md" in context.knowledge_evidence
    assert "project_root=/srv/v4e2e" in context.environment_evidence


def test_unregistered_evidence_request_binds_safe_readonly_command():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    commands = []
    request = ProbeRequest(
        "command_available", {"commands": ["screen"]},
        "确认 screen 可执行文件", ({
            "fact_id": "fact-screen-executable",
            "predicate": "command.path",
            "expected": "/usr/bin/screen",
            "comparison": "contains",
        },), subject={"kind": "command", "value": "screen"},
    )
    llm = FakeLLM([json.dumps({
        "status": "command", "command": "which screen",
        "covers": ["fact-screen-executable"],
        "subject": {"kind": "command", "value": "screen"},
        "extractors": [{
            "fact_id": "fact-screen-executable",
            "kind": "output_contains", "expected": "/usr/bin/screen",
        }],
        "reason": "locate the executable",
    })])
    agent = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: (_ for _ in ()).throw(
            AssertionError("unknown capability must not reach the probe runner")
        ),
        readonly_command_runner=lambda command: commands.append(command) or "/usr/bin/screen",
    )
    bundle = EvidenceBundle(goal="确认 screen 命令")

    agent.collect_requests([request], bundle)

    assert commands == ["which screen"]
    assert [item.request.probe for item in bundle.records] == ["readonly_command"]
    assert bundle.records[0].status == "available"
    assert "/usr/bin/screen" in bundle.records[0].output


def test_registered_probe_uses_shell_for_only_the_unresolved_fact_ids():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    commands = []
    llm = FakeLLM([json.dumps({
        "status": "command", "command": "ps -p 1234 -o args=",
        "covers": ["fact-master-cmdline", "fact-master-python"],
        "subject": {"kind": "pid_set", "value": [1234]},
        "extractors": [
            {"fact_id": "fact-master-cmdline", "kind": "output_contains", "expected": "gunicorn"},
            {"fact_id": "fact-master-python", "kind": "output_contains", "expected": "/python"},
        ],
        "reason": "registered evidence lacks full cmdline",
    })])
    agent = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: "pid=1234 cwd=unknown cmdline=truncated",
        readonly_command_runner=lambda command: commands.append(command) or (
            "/opt/conda/envs/test/bin/python -m gunicorn master_main:flask_app"
        ),
    )
    bundle = EvidenceBundle(goal="重启 test master")

    request = ProbeRequest(
        "process_detail", {"pids": [1234]}, "继承 master 运行身份",
        (
            {"fact_id": "fact-master-cmdline", "predicate": "process.cmdline", "expected": True, "comparison": "present"},
            {"fact_id": "fact-master-python", "predicate": "process.python_executable", "expected": True, "comparison": "present"},
        ), "refresh",
    )
    agent.collect_requests([request], bundle)

    assert commands == ["ps -p 1234 -o args="]
    assert [item.request.probe for item in bundle.records] == [
        "process_detail", "readonly_command",
    ]
    assert all(item.status == "available" for item in bundle.records)


def test_readonly_fallback_does_not_repeat_no_progress_command():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    commands = []
    llm = FakeLLM([
        json.dumps({
            "status": "command", "command": "ls -la /srv/klonet",
            "covers": ["fact-run-as-uid", "fact-python-executable"],
            "subject": {"kind": "path", "value": "/srv/klonet"},
            "extractors": [
                {"fact_id": "fact-run-as-uid", "kind": "output_nonempty"},
                {"fact_id": "fact-python-executable", "kind": "output_nonempty"},
            ],
            "reason": "inspect root",
        }),
        json.dumps({
            "status": "command", "command": "ls -la /srv/klonet",
            "covers": ["fact-run-as-uid", "fact-python-executable"],
            "subject": {"kind": "path", "value": "/srv/klonet"},
            "extractors": [
                {"fact_id": "fact-run-as-uid", "kind": "output_nonempty"},
                {"fact_id": "fact-python-executable", "kind": "output_nonempty"},
            ],
            "reason": "repeat inspection",
        }),
    ])
    agent = DiscoveryAgent(
        llm,
        probe_runner=None,
        readonly_command_runner=lambda command: commands.append(command) or "files",
    )
    bundle = EvidenceBundle(goal="find runtime identity")
    request = ProbeRequest(
        "runtime_startup_identity", {"project_root": "/srv/klonet"},
        "find runtime identity", (
            {"fact_id": "fact-run-as-uid", "predicate": "process.uid", "expected": True, "comparison": "present"},
            {"fact_id": "fact-python-executable", "predicate": "process.python_executable", "expected": True, "comparison": "present"},
        ),
        "refresh", "gap-runtime-identity",
    )

    agent.collect_requests([request], bundle)
    agent.collect_requests([request], bundle)

    assert commands == ["ls -la /srv/klonet"]
    assert bundle.records[-1].status == "unavailable"
    assert "repeated a command" in bundle.records[-1].output
    second_payload = json.loads(llm.calls[1]["messages"][1]["content"])
    assert second_payload["prior_fallback_attempts"][0]["command"] == commands[0]


def test_registered_probe_complete_result_does_not_run_shell_fallback():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM([json.dumps({
        "status": "satisfied",
        "reason": "all required process identity fields are present",
    })])
    agent = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: (
            "pid=1234 cwd=/srv/test run_as_uid=1000 "
            "python_executable=/opt/python cmdline=/opt/python -m gunicorn"
        ),
        readonly_command_runner=lambda command: (_ for _ in ()).throw(
            AssertionError("complete registered evidence must not execute fallback")
        ),
    )
    bundle = EvidenceBundle(goal="inspect")

    agent.collect_requests([
        ProbeRequest(
            "process_detail", {"pids": [1234]}, "process identity",
            ("cwd", "run_as_uid", "python executable", "full cmdline"),
        )
    ], bundle)

    assert [item.request.probe for item in bundle.records] == ["process_detail"]


def test_running_platforms_required_facts_do_not_trigger_shell_on_first_result():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    class ForbiddenLLM:
        def complete(self, **kwargs):
            raise AssertionError("usable registered inventory must not evaluate Shell")

    bundle = EvidenceBundle(goal="检查哪些平台正常运行")
    agent = DiscoveryAgent(
        ForbiddenLLM(),
        probe_runner=lambda requests: (
            "inspect_running_platforms\nruntime_candidate_count=2\n"
            "healthy_count=1\nabnormal_count=1\n"
            "platform=test project_root=/srv/test backend_status=healthy\n"
            "platform=broken project_root=/srv/broken backend_status=abnormal"
        ),
    )

    agent.collect_requests([
        ProbeRequest(
            "running_platforms", {}, "根据健康接口分类平台",
            ("platform_health_status",), "refresh",
        )
    ], bundle)

    assert [item.request.probe for item in bundle.records] == ["running_platforms"]
    assert bundle.records[0].status == "available"


def test_readonly_fallback_policy_rejection_is_unavailable_evidence():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    llm = FakeLLM([json.dumps({
        "status": "command", "command": "ps aux | tee /tmp/processes",
        "covers": ["fact-process-uid"],
        "subject": {"kind": "pid_set", "value": [1234]},
        "extractors": [{"fact_id": "fact-process-uid", "kind": "output_nonempty"}],
        "scope_expansion_reason": "incorrectly proposes a temporary output path",
        "reason": "unsafe candidate",
    })])
    agent = DiscoveryAgent(
        llm,
        probe_runner=lambda requests: "",
        readonly_command_runner=lambda command: (_ for _ in ()).throw(
            PermissionError("shell evaluation is not allowed")
        ),
    )
    bundle = EvidenceBundle(goal="inspect")

    agent.collect_requests([
        ProbeRequest(
            "process_owner", {"pids": [1234]}, "owner", ({
                "fact_id": "fact-process-uid", "predicate": "process.uid",
                "expected": True, "comparison": "present",
            },)
        )
    ], bundle)

    assert bundle.records[0].request.probe == "readonly_command"
    assert bundle.records[0].status == "unavailable"
    assert "PermissionError" in bundle.records[0].output


def test_registered_ports_probe_resolves_fact_ids_without_shell_fallback():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    class ForbiddenLLM:
        def complete(self, **kwargs):
            raise AssertionError("resolved registered facts must not enter Shell")

    request = ProbeRequest(
        "ports", {"ports": [47001, 47002]}, "freeze available ports", (
            {
                "fact_id": "fact-master-port-free",
                "predicate": "port.available",
                "expected": 47001,
                "comparison": "contains",
            },
            {
                "fact_id": "fact-worker-port-free",
                "predicate": "port.available",
                "expected": 47002,
                "comparison": "contains",
            },
        ), gap_id="gap-runtime-ports",
    )
    bundle = EvidenceBundle(goal="allocate ports")
    agent = DiscoveryAgent(
        ForbiddenLLM(),
        probe_runner=lambda requests: (
            "inspect_ports\nchecked_ports=47001,47002\n"
            "occupied_ports=none\navailable_ports=47001,47002"
        ),
    )

    agent.collect_requests([request], bundle)

    assert [item.status for item in bundle.records[0].observations] == [
        "confirmed", "confirmed",
    ]
    assert bundle.resolve_gap("gap-runtime-ports").unresolved_fact_ids == ()
    assert [item.request.probe for item in bundle.records] == ["ports"]


def test_shell_fallback_cannot_expand_a_frozen_path_subject():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    request = ProbeRequest(
        "long_tail_layout",
        {"path": "/srv/source"},
        "check exact source",
        ({
            "fact_id": "fact-source-marker",
            "predicate": "source.marker",
            "expected": "master_main.py",
            "comparison": "contains",
        },),
        gap_id="gap-source-marker",
        subject={"kind": "path", "value": "/srv/source"},
        exclusions=("/etc/nginx",),
    )
    llm = FakeLLM([json.dumps({
        "status": "command",
        "command": "find /home /root /opt /var -name master_main.py",
        "covers": ["fact-source-marker"],
        "subject": {"kind": "path", "value": "/srv/source"},
        "extractors": [{
            "fact_id": "fact-source-marker",
            "kind": "output_contains", "expected": "master_main.py",
        }],
        "scope_expansion_reason": "search common deployment roots",
        "reason": "broad search",
    })])
    commands = []
    bundle = EvidenceBundle(goal="use /srv/source")
    agent = DiscoveryAgent(
        llm,
        probe_runner=None,
        readonly_command_runner=lambda command: commands.append(command) or "",
    )

    agent.collect_requests([request], bundle)

    assert commands == []
    assert bundle.records[-1].status == "unavailable"
    assert "exceeded the frozen path subject" in bundle.records[-1].output


def test_shell_fallback_cannot_confirm_equals_fact_from_nonempty_output():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    request = ProbeRequest(
        "long_tail_owner", {"pids": [1234]}, "resolve exact uid", ({
            "fact_id": "fact-process-uid",
            "predicate": "process.uid",
            "expected": 1000,
            "comparison": "equals",
        },), gap_id="gap-process-owner",
    )
    llm = FakeLLM([json.dumps({
        "status": "command",
        "command": "ps -p 1234 -o uid=",
        "covers": ["fact-process-uid"],
        "subject": {"kind": "pid_set", "value": [1234]},
        "extractors": [{
            "fact_id": "fact-process-uid", "kind": "output_nonempty",
        }],
        "reason": "read uid",
    })])
    commands = []
    bundle = EvidenceBundle(goal="resolve process owner")
    agent = DiscoveryAgent(
        llm, probe_runner=None,
        readonly_command_runner=lambda command: commands.append(command) or "1000",
    )

    agent.collect_requests([request], bundle)

    assert commands == []
    assert bundle.records[-1].status == "unavailable"
    assert "output_nonempty can only resolve" in bundle.records[-1].output


def test_project_layout_probe_does_not_read_unrelated_host_configuration(
    tmp_path, monkeypatch,
):
    from klonet_agent.ops.privileged.environment_facts import EnvironmentFactCollector
    from klonet_agent.ops.privileged.probes import _project_layout

    monkeypatch.setattr(
        EnvironmentFactCollector,
        "_nginx_facts",
        lambda self: (_ for _ in ()).throw(
            AssertionError("project layout must not inspect Nginx")
        ),
    )

    output = _project_layout({"project_roots": [str(tmp_path)]})

    data = json.loads(output)
    assert set(data) == {"schema_version", "projects"}


def test_discovery_freezes_and_checks_an_explicit_source_before_model_discovery():
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    source = "/srv/source"
    observed = []

    def run(requests):
        observed.extend(requests)
        assert requests == [{
            "probe": "project_layout",
            "args": {"project_roots": [source]},
            "purpose": "检查用户明确提供的源码目录及入口文件",
        }]
        return json.dumps({
            "schema_version": 1,
            "projects": [{
                "candidate_root": source,
                "source_repo_root": source,
                "runtime_cwd": source,
                "layout_kind": "source_repository",
                "violations": [],
            }],
        })

    agent = DiscoveryAgent(
        FakeLLM([json.dumps({"status": "ready"})]),
        probe_runner=run,
    )

    bundle = agent.collect(
        "创建部署，源码模板目录：/srv/source，目标目录：/srv/target；不配置 Nginx",
    )

    assert observed
    assert [record.request.probe for record in bundle.records] == [
        "user_decision", "user_decision", "project_layout",
    ]
    source_decision = bundle.records[0]
    assert source_decision.request.subject.to_dict() == {
        "kind": "path", "value": source,
    }
    assert source_decision.observations[0].status == "confirmed"
    layout = bundle.records[-1]
    assert layout.request.subject.to_dict() == {"kind": "path", "value": source}
    assert layout.request.exclusions == ("nginx",)
    assert bundle.resolve_gap("gap-explicit-source-layout").unresolved_fact_ids == ()


def test_registered_project_layout_parses_production_probe_wrapper():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    output = '''## recovery_probe_1 name=project_layout purpose=inspect source
{
  "schema_version": 1,
  "projects": [{
    "candidate_root": "/srv/source",
    "layout_kind": "platform_with_nested_backend",
    "runtime_cwd": "/srv/source",
    "source_repo_root": "/srv/source/vemu_uestc",
    "violations": []
  }]
}'''
    request = ProbeRequest(
        "project_layout",
        {"project_roots": ["/srv/source"]},
        "inspect source",
        ({
            "fact_id": "fact-source-exists",
            "predicate": "path.exists",
            "expected": True,
            "comparison": "equals",
        }, {
            "fact_id": "fact-source-entries",
            "predicate": "project.entry_files",
            "expected": ["master_main.py", "worker_main.py"],
            "comparison": "contains_all",
        }),
        gap_id="gap-source-layout",
        subject={"kind": "path", "value": "/srv/source"},
    )
    llm = FakeLLM([])
    bundle = EvidenceBundle(goal="inspect source")

    DiscoveryAgent(
        llm, probe_runner=lambda requests: output,
    ).collect_requests([request], bundle)

    record = bundle.records[0]
    assert tuple(
        item.fact_id for item in record.observations
        if item.status == "confirmed"
    ) == ("fact-source-exists", "fact-source-entries")
    assert not any(item.request.probe == "readonly_command" for item in bundle.records)
    assert llm.calls == []


def test_registered_common_probes_resolve_typed_facts_without_shell():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    outputs = {
        "git_repository": (
            "## recovery_probe_1 name=git_repository purpose=inspect\n"
            "inspect_git_repository\npath=/srv/source inside_work_tree=true "
            "revision=abc123\nstatus=## main...origin/main\n"
            "remotes=origin https://example.invalid/repo.git (fetch)"
        ),
        "disk": (
            "## recovery_probe_1 name=disk purpose=inspect\n"
            "inspect_disk\nFilesystem Size Used Avail Use% Mounted on\n"
            "/dev/sda 100G 20G 80G 20% /"
        ),
        "path_permissions": (
            "## recovery_probe_1 name=path_permissions purpose=inspect\n"
            "inspect_path_permissions\n"
            "path=/srv/source exists=true mode=0o755 uid=1000 gid=1000"
        ),
    }
    requests = [
        ProbeRequest(
            "git_repository", {"repository": "/srv/source"}, "git",
            ({"fact_id": "fact-git-remote", "predicate": "git.remote",
              "expected": True, "comparison": "present"},),
            gap_id="gap-git", subject={"kind": "path", "value": "/srv/source"},
        ),
        ProbeRequest(
            "disk", {}, "disk",
            ({"fact_id": "fact-disk-capacity", "predicate": "disk.capacity",
              "expected": True, "comparison": "present"},),
            gap_id="gap-disk",
        ),
        ProbeRequest(
            "path_permissions", {"paths": ["/srv/source"]}, "permissions",
            ({"fact_id": "fact-path-exists", "predicate": "path.exists",
              "expected": True, "comparison": "equals"},),
            gap_id="gap-permissions",
            subject={"kind": "path", "value": "/srv/source"},
        ),
    ]
    llm = FakeLLM([])
    bundle = EvidenceBundle(goal="inspect")

    DiscoveryAgent(
        llm,
        probe_runner=lambda values: outputs[values[0]["probe"]],
    ).collect_requests(requests, bundle)

    assert all(
        observation.status == "confirmed"
        for record in bundle.records for observation in record.observations
    )
    assert not any(item.request.probe == "readonly_command" for item in bundle.records)
    assert llm.calls == []


def test_shell_scope_without_a_known_subject_requires_an_expansion_reason():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    request = ProbeRequest(
        "long_tail_source_search",
        {"name": "master_main.py"},
        "locate source because the user did not provide a path",
        ({
            "fact_id": "fact-source-location",
            "predicate": "source.location",
            "expected": True,
            "comparison": "present",
        },),
        gap_id="gap-source-location",
    )
    llm = FakeLLM([json.dumps({
        "status": "command",
        "command": "find /home /opt -name master_main.py",
        "covers": ["fact-source-location"],
        "subject": None,
        "extractors": [{
            "fact_id": "fact-source-location",
            "kind": "output_nonempty",
        }],
        "reason": "search common roots",
    })])
    commands = []
    bundle = EvidenceBundle(goal="locate source")
    agent = DiscoveryAgent(
        llm,
        probe_runner=None,
        readonly_command_runner=lambda command: commands.append(command) or "",
    )

    agent.collect_requests([request], bundle)

    assert commands == []
    assert "scope expansion reason" in bundle.records[-1].output


def test_default_progress_shows_fact_summary_without_raw_probe_output():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    raw = (
        "inspect_ports\nchecked_ports=47001\noccupied_ports=none\n"
        "available_ports=47001\n" + "Permission denied /secret/path\n" * 100
    )
    progress = []
    request = ProbeRequest(
        "ports", {"ports": [47001]}, "check port", ({
            "fact_id": "fact-port-free",
            "predicate": "port.available",
            "expected": 47001,
            "comparison": "contains",
        },),
        gap_id="gap-port",
    )
    agent = DiscoveryAgent(
        FakeLLM([]),
        probe_runner=lambda requests: raw,
        on_progress=progress.append,
    )

    agent.collect_requests([request], EvidenceBundle(goal="check port"))

    rendered = "\n".join(progress)
    assert "fact-port-free" in rendered
    assert "confirmed" in rendered
    assert "Permission denied" not in rendered
    assert "/secret/path" not in rendered
    assert "原始输出已保存为 ev-" in rendered


def test_ad_hoc_binding_evidence_joins_active_workflow_bundle():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    bundle = EvidenceBundle(goal="收编 test worker")
    discovery = DiscoveryAgent(
        FakeLLM([]),
        probe_runner=lambda requests: "pid=1234 cwd=/srv/test",
    )

    with discovery.evidence_scope(bundle):
        output = discovery.run_ad_hoc_requests([{
            "probe": "process_detail",
            "args": {"project_root": "/srv/test"},
            "purpose": "确认 worker 运行身份",
        }])

    assert "pid=1234" in output
    assert len(bundle.records) == 1
    assert bundle.records[0].request.probe == "process_detail"


def test_ad_hoc_evidence_without_scope_does_not_mutate_unrelated_bundle():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceBundle
    from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent

    unrelated = EvidenceBundle(goal="另一个工作流")
    discovery = DiscoveryAgent(
        FakeLLM([]),
        probe_runner=lambda requests: "pid=5678 cwd=/srv/other",
    )

    discovery.run_ad_hoc_requests([{
        "probe": "process_detail",
        "args": {"project_root": "/srv/other"},
        "purpose": "验证隔离",
    }])

    assert unrelated.records == []
