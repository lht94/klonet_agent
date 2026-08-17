from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_default_restart_components_follow_manifest_dependency_topology():
    from klonet_agent.ops.privileged.workflow.change_planner import (
        _ordered_default_restart_components,
    )
    from klonet_agent.ops.privileged.workflow.contracts import RuntimeComponentSpec

    specs = {
        "worker": RuntimeComponentSpec(name="worker", screen_suffix="w", start_after=("master",)),
        "metrics": RuntimeComponentSpec(name="metrics", screen_suffix="metrics", start_after=("worker",)),
        "master": RuntimeComponentSpec(name="master", screen_suffix="m"),
        "sidecar": RuntimeComponentSpec(
            name="sidecar", screen_suffix="sidecar", category="shared_dependency",
        ),
    }

    assert _ordered_default_restart_components(specs) == ["master", "worker", "metrics"]


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        output = self.outputs.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output))]
        )


def test_completed_execution_renders_step_receipt_instead_of_generic_success():
    from klonet_agent.ops.privileged.contracts import (
        CheckResult, ExecutionBinding, ExecutionEvidence, ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
    from klonet_agent.ops.privileged.workflow.mutation import _execution_receipt

    micro = PrivilegedStep(
        step_id="restart-master",
        title="重启 master",
        execution_binding=ExecutionBinding(
            kind="registered_action",
            risk="medium",
            action="restart_screen_component",
            args={"component": "master", "screen_session": "v4e2e_m"},
        ),
        status="completed",
        evidence=ExecutionEvidence(
            return_code=0,
            stdout="action=restart_screen_component component=master session=v4e2e_m",
            environment_changed=True,
        ),
        checks=[CheckResult(
            checker="backend_health", status="passed",
            expected="HTTP 200 code=1", observed="HTTP 200 code=1",
        )],
    )
    plan = ChangePlan(
        plan_id="priv-ops-receipt",
        goal="重启 v4e2e",
        risk="medium",
            steps=[ChangeStep(
            step_id="change-1", title="重启平台", objective="重启平台",
            risk="medium", expected_changes=["平台完成重启"],
            postconditions=[{"checker": "backend_health", "args": {
                "url": "http://127.0.0.1:47001/server_health/",
                "expected_code": 1,
            }}],
            implementation_plan=ImplementationPlan(
                implementation_id="impl-1", semantic_step_id="change-1",
                objective="重启平台", steps=[micro], status="completed",
            ),
            status="completed",
        )],
        status="completed",
    )

    rendered = _execution_receipt(plan)

    assert "计划 priv-ops-receipt 已执行完成" in rendered
    assert "重启 master" in rendered
    assert "restart_screen_component" not in rendered
    assert "backend_health：通过" in rendered


def test_confirmation_localizes_all_baseline_component_restart_expectations():
    from klonet_agent.ops.privileged.workflow.mutation import _localized_plan_text

    assert _localized_plan_text(
        "restart requested celery role and process readiness succeeds"
    ) == "按要求重启 celery 角色，并确认进程就绪"
    assert _localized_plan_text(
        "start missing web_terminal role at 47003 and listener readiness succeeds"
    ) == "启动缺失的 web_terminal 角色（端口 47003），并确认监听就绪"


def test_platform_restart_compiler_keeps_all_managed_application_components():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    bundle = EvidenceBundle(goal="帮我重启 v4e2e 平台")
    knowledge = bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("klonet_knowledge", {"query": bundle.goal}, "startup contract"),
        "source=startup_shutdown.md\nstartup_cwd=<project_root>",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime inventory"),
        (
            "platform=v4e2e project_root=/home/lzl/klonet_v4_e2e "
            "roles=celery,master,web_terminal,worker "
            "configured_ports=master_port:47001,worker_port:47002,"
            "web_terminal_port:47003 master_port=47001 master_endpoint=healthy "
            "worker_port=47002 worker_endpoint=healthy "
            "runtime_identities=10:1000:/opt/python3.8"
        ),
    ))

    data = ChangePlannerAgent._deterministic_runtime_restart(
        "帮我重启 v4e2e 平台", bundle,
    )

    assert data is not None
    text = " ".join([
        data["changes"][0]["objective"],
        *data["changes"][0]["expected_changes"],
    ])
    for component in ("master", "celery", "web_terminal", "worker"):
        assert component in text
    assert "preserve healthy" not in text
    assert knowledge.evidence_id in data["changes"][0]["evidence_refs"]


def test_healthy_role_normalization_never_rewrites_platform_restart():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    bundle = EvidenceBundle(goal="帮我重启 v4e2e 平台")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime inventory"),
        "platform=v4e2e project_root=/srv/v4e2e roles=master,worker "
        "configured_ports=master_port:47001,worker_port:47002,"
        "web_terminal_port:47003 "
        "master_port=47001 master_endpoint=healthy "
        "worker_port=47002 worker_endpoint=healthy "
        "web_terminal_port=47003 runtime_identities=10:1000:/opt/python3.8",
    ))
    data = ChangePlannerAgent._deterministic_runtime_restart(
        "帮我重启 v4e2e 平台", bundle,
    )

    ChangePlannerAgent._normalize_healthy_runtime_role_changes(
        data, bundle, goal="帮我重启 v4e2e 平台",
    )

    serialized = str(data)
    assert "preserve healthy" not in serialized
    assert "restart requested master" in serialized
    assert "restart requested worker" in serialized


def test_v4_e2e_alias_uses_deterministic_restart_and_existing_ports():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    bundle = EvidenceBundle(goal="帮我重启v4_e2e平台")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime inventory"),
        "platform=v4e2e project_root=/home/lzl/klonet_v4_e2e "
        "roles=celery,master,worker backend_status=healthy "
        "configured_ports=master_port:47001,worker_port:47002,"
        "public_port:45553,web_terminal_port:47003 "
        "master_port=47001 master_endpoint=healthy "
        "worker_port=47002 worker_endpoint=healthy "
        "web_terminal_port=47003 runtime_identities=10:1000:/opt/python3.8",
    ))

    data = ChangePlannerAgent._deterministic_runtime_restart(
        "帮我重启v4_e2e平台",
        bundle,
        intent_context={
            "operation": "restart", "scope": "platform",
            "resolved_project_root": "/home/lzl/klonet_v4_e2e",
        },
    )

    assert data is not None
    assert len(data["changes"]) == 1
    assert "停止" not in data["changes"][0]["title"]
    ports = {
        item["role"]: item["value"] for item in data["resources"]
        if item["kind"] == "port"
    }
    assert ports == {
        "master_port": 47001,
        "worker_port": 47002,
        "web_terminal_port": 47003,
    }


def test_platform_restart_compiler_includes_manifest_managed_future_component():
    import base64

    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )

    specs = [
        {"name": "master", "screen_suffix": "m", "start_after": []},
        {"name": "celery", "screen_suffix": "c", "start_after": ["master"]},
        {"name": "web_terminal", "screen_suffix": "web", "start_after": ["celery"]},
        {"name": "worker", "screen_suffix": "w", "start_after": ["web_terminal"]},
        {
            "name": "metrics", "screen_suffix": "metrics",
            "command_argv": ["/opt/python", "-m", "metrics_service"],
            "preflight_argv": ["/opt/python", "-c", "import metrics_service"],
            "start_after": ["worker"],
            "health_checks": [{
                "checker": "port_listening", "args": {"port": 47009},
            }],
        },
        {
            "name": "redis", "category": "shared_dependency",
            "screen_suffix": "redis", "start_after": [],
        },
    ]
    encoded = base64.urlsafe_b64encode(
        json.dumps(specs).encode("utf-8")
    ).decode("ascii")
    bundle = EvidenceBundle(goal="重启 v4e2e 平台")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime inventory"),
        (
            "platform=v4e2e project_root=/srv/v4e2e roles=master,worker "
            "configured_ports=master_port:47001,worker_port:47002,"
            "web_terminal_port:47003 master_port=47001 master_endpoint=healthy "
            "worker_port=47002 worker_endpoint=healthy "
            "component_specs_b64=%s runtime_identities=10:1000:/opt/python"
        ) % encoded,
    ))

    data = ChangePlannerAgent._deterministic_runtime_restart(
        "重启 v4e2e 平台", bundle,
    )

    assert data is not None
    expected = " ".join(data["changes"][0]["expected_changes"])
    assert "managed component metrics" in expected
    assert "redis" not in expected
    resource = next(
        item for item in data["resources"]
        if item["role"] == "runtime_component_spec:metrics"
    )
    assert "metrics_service" in resource["value"]


def _bundle_and_conclusion():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceClaim,
        EvidenceConclusion,
        EvidenceRecord,
        ProbeRequest,
    )

    bundle = EvidenceBundle(goal="deploy v4e2e")
    record = bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("git_repository", {"repository": "/srv/source"}, "remote"),
            "origin=gitee:example/platform.git branch=develop",
        )
    )
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("ports", {"ports": [47001]}, "freeze port"),
            "inspect_ports\nno matching listeners",
        )
    )
    conclusion = EvidenceConclusion(
        confirmed_facts=[EvidenceClaim("source repository identified", [record.evidence_id])]
    )
    return bundle, conclusion, record.evidence_id


def test_runtime_repair_plan_covers_every_unhealthy_backend_role():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="repair two runtimes")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("running_platforms", {}, "classify runtime health"),
            "\n".join([
                "inspect_running_platforms",
                "platform=vemu_uestc project_root=/home/lzl/vemu_uestc "
                "roles=celery,master,web_terminal backend_status=abnormal "
                "missing_roles=worker master_port=45551 master_endpoint=unreachable "
                "http_status=0 detail=TimeoutError worker_port=45552 "
                "worker_endpoint=not_checked reason=role_not_running",
            ]),
        )
    )
    data = {
        "resources": [{
            "name": "formal_master_port",
            "kind": "port",
            "status": "frozen",
            "role": "master_port",
            "value": 45559,
            "source": "evidence",
            "consumers": ["change-2.master_port"],
        }, {
            "name": "formal_worker_port",
            "kind": "port",
            "status": "frozen",
            "role": "worker_port",
            "value": 45552,
            "source": "existing_runtime",
            "consumers": ["change-2.worker_port"],
        }],
        "changes": [{
            "step_id": "change-2",
            "title": "Restore formal worker",
            "objective": "Start worker for /home/lzl/vemu_uestc",
            "reason": "worker is missing",
            "expected_changes": [
                "worker starts",
                "formal master remains running and is not restarted",
            ],
            "postconditions": [{
                "checker": "port_listening", "args": {"port": 45552},
            }],
        }],
    }

    ChangePlannerAgent._normalize_runtime_repair_coverage(data, bundle)

    change = data["changes"][0]
    combined = " ".join([change["objective"], *change["expected_changes"]])
    assert "master" in combined
    assert "worker" in combined
    assert {
        check["args"].get("url")
        for check in change["postconditions"]
        if check["checker"] == "backend_health"
    } == {
        "http://127.0.0.1:45559/server_health/",
        "http://127.0.0.1:45552/server_health/",
    }
    assert "restart unhealthy master" in combined
    assert "start missing worker" in combined
    assert "not restarted" not in combined


def test_explicit_v4e2e_restart_compiles_without_llm_or_rediscovery():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="帮我重启 v4e2e 的 master 和 worker")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime inventory"),
        "platform=v4e2e project_root=/home/lzl/klonet_workflow_e2e "
        "backend_status=abnormal master_pids=none worker_pids=none "
        "master_identities=none worker_identities=none "
        "master_port=47001 master_endpoint=not_checked reason=role_not_running "
        "worker_port=47002 worker_endpoint=not_checked reason=role_not_running",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("process", {"keywords": ["v4e2e"]}, "runtime identity"),
        "pid=2031141 cwd=/home/lzl/klonet_workflow_e2e/mains "
        "cmdline=/home/lzl/miniconda3/envs/klonet-py38/bin/python3.8 -m celery",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("privilege_capabilities", {}, "capabilities"),
        "current_uid=1000 current_gid=1000",
    ))
    llm = FakeLLM([])

    outcome = ChangePlannerAgent(llm).plan(
        bundle.goal, bundle, EvidenceConclusion(),
    )

    assert outcome.status == "need_execution"
    assert llm.calls == []
    assert outcome.plan is not None
    assert outcome.plan.steps[0].expected_changes == [
        "start missing master role at 47001 and backend health succeeds",
        "start missing worker role at 47002 and backend health succeeds",
    ]
    assert {resource.role for resource in outcome.plan.resources} >= {
        "instance_root", "instance_identifier", "master_port", "worker_port",
        "run_as_uid", "python_executable",
    }


def test_explicit_restart_inherits_identity_from_selected_roots_other_role():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceConclusion, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="帮我重启 v4e2e 的 master 和 worker")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime inventory"),
        "platform=102 project_root=/srv/102 runtime_identities="
        "10:0:/usr/bin/python3.8 master_port=10001 master_endpoint=healthy "
        "worker_port=10002 worker_endpoint=healthy\n"
        "platform=v4e2e project_root=/home/lzl/klonet_workflow_e2e roles=celery "
        "master_pids=none worker_pids=none master_identities=none "
        "worker_identities=none runtime_identities="
        "2031141:1000:/home/lzl/miniconda3/envs/klonet-py38/bin/python3.8 "
        "master_port=47001 master_endpoint=not_checked reason=role_not_running "
        "worker_port=47002 worker_endpoint=not_checked reason=role_not_running",
    ))

    outcome = ChangePlannerAgent(FakeLLM([])).plan(
        bundle.goal, bundle, EvidenceConclusion(),
    )

    assert outcome.status == "need_execution"
    assert outcome.plan is not None
    resources = {item.role: item.value for item in outcome.plan.resources}
    assert resources["run_as_uid"] == "1000"
    assert resources["python_executable"] == (
        "/home/lzl/miniconda3/envs/klonet-py38/bin/python3.8"
    )


def test_runtime_repair_replaces_legacy_http_checks_with_backend_health_contract():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="repair formal")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "health"),
        "platform=vemu project_root=/home/lzl/vemu_uestc "
        "backend_status=abnormal master_port=45551 master_endpoint=unreachable "
        "worker_port=45552 worker_endpoint=not_checked reason=role_not_running",
    ))
    data = {
        "resources": [],
        "changes": [{
            "step_id": "repair",
            "title": "Restore formal master and worker",
            "objective": "Repair /home/lzl/vemu_uestc",
            "expected_changes": ["start roles"],
            "postconditions": [
                {"checker": "http_status", "args": {"url": "http://127.0.0.1:45551"}},
                {"checker": "http_status", "args": {"url": "http://192.168.1.124:45552/check_health"}},
            ],
        }],
    }

    ChangePlannerAgent._normalize_runtime_repair_coverage(data, bundle)

    health = [
        item for item in data["changes"][0]["postconditions"]
        if item["checker"] == "backend_health"
    ]
    assert health == [
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45551/server_health/", "expected_code": 1}},
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45552/server_health/", "expected_code": 1}},
    ]


def test_runtime_repair_freezes_pid_for_unhealthy_live_role():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="repair /srv/102")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("running_platforms", {}, "health"),
            "platform=102 project_root=/srv/102 backend_status=abnormal "
            "master_pids=1239000,1239001 master_port=27694 "
            "master_identities=1239000:997:/usr/bin/python3.8 "
            "master_endpoint=unreachable worker_port=27695 worker_endpoint=healthy",
        )
    )
    data = {
        "resources": [],
        "changes": [
            {
                "step_id": "repair", "title": "Restart master",
                "objective": "Repair master for /srv/102",
                "expected_changes": ["master recovers"],
                "postconditions": [
                    {"checker": "backend_health", "args": {
                        "url": "http://127.0.0.1:27694/server_health/"
                    }}
                ],
            }
        ],
    }

    ChangePlannerAgent._normalize_runtime_repair_coverage(data, bundle)

    pid = next(item for item in data["resources"] if item["role"] == "master_pid")
    assert pid["value"] == 1239000
    assert pid["source"] == "running_platforms"
    assert pid["consumers"] == ["repair.master_pid"]
    uid = next(item for item in data["resources"] if item["role"] == "master_uid")
    python = next(
        item for item in data["resources"]
        if item["role"] == "master_python_executable"
    )
    assert uid["value"] == 997
    assert uid["consumers"] == ["repair.run_as_uid"]
    assert python["value"] == "/usr/bin/python3.8"
    assert python["consumers"] == ["repair.python_executable"]


def test_authoritative_healthy_worker_restart_is_compiled_to_verification():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="repair 102 master and verify worker")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        "platform=102 project_root=/home/klonet-agent/102 backend_status=abnormal "
        "master_port=27694 master_endpoint=unreachable "
        "worker_port=27695 worker_endpoint=healthy",
    ))
    data = {"changes": [
        {
            "step_id": "master", "title": "Repair and restart master",
            "objective": "Repair master for /home/klonet-agent/102",
            "expected_changes": ["master restarts"], "postconditions": [],
        },
        {
            "step_id": "worker", "title": "Restart worker and verify",
            "objective": "Restart worker for /home/klonet-agent/102",
            "expected_changes": ["worker restarts"], "postconditions": [],
        },
    ]}

    ChangePlannerAgent._normalize_healthy_runtime_role_changes(data, bundle)

    worker = data["changes"][1]
    assert worker["title"] == "Verify healthy worker backend"
    assert worker["risk"] == "readonly"
    assert worker["expected_changes"] == []
    assert worker["postconditions"] == [{
        "checker": "backend_health",
        "args": {
            "url": "http://127.0.0.1:27695/server_health/",
            "expected_code": 1,
        },
    }]


def test_migrated_healthy_worker_is_rechecked_on_its_new_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"changes": [{
        "step_id": "migrate",
        "title": "Migrate test master/worker",
        "objective": "Migrate and start master and worker",
        "expected_changes": [
            "master starts on 45554 and becomes healthy",
            "worker starts on 45555 and becomes healthy",
        ],
        "postconditions": [
            {"checker": "http_status", "args": {"url": "http://127.0.0.1:45554"}},
            {"checker": "http_status", "args": {"url": "http://127.0.0.1:45555"}},
        ],
    }]}
    resources = [
        PlanResource("test_master", "port", "frozen", "master_port", 45554, "planner", consumers=["migrate.master_port"]),
        PlanResource("test_worker", "port", "frozen", "worker_port", 45555, "planner", consumers=["migrate.worker_port"]),
    ]

    ChangePlannerAgent._normalize_backend_role_health_contracts(data, resources)

    assert data["changes"][0]["postconditions"] == [
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45554/server_health/", "expected_code": 1}},
        {"checker": "backend_health", "args": {"url": "http://127.0.0.1:45555/server_health/", "expected_code": 1}},
    ]


def test_verify_only_worker_http_check_uses_backend_health_contract():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"changes": [{
        "step_id": "repair-master",
        "title": "Repair master and verify worker",
        "objective": "Recover master for /srv/102",
        "expected_changes": ["master restarts"],
        "postconditions": [{
            "checker": "http_status",
            "args": {
                "url": "http://127.0.0.1:27695",
                "expected_status": 200,
            },
        }],
    }]}
    resources = [
        PlanResource(
            "worker_port", "port", "frozen", "worker_port", 27695,
            "running_platforms", consumers=["repair-master.worker_port"],
        ),
    ]

    ChangePlannerAgent._normalize_backend_role_health_contracts(data, resources)

    assert data["changes"][0]["postconditions"] == [{
        "checker": "backend_health",
        "args": {
            "url": "http://127.0.0.1:27695/server_health/",
            "expected_code": 1,
        },
    }]


def test_change_plan_authorization_hash_covers_resources_steps_and_bindings():
    from klonet_agent.ops.privileged.contracts import ExecutionBinding, PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep

    step = ChangeStep(
        step_id="clone",
        title="clone source",
        objective="clone source into isolated root",
        risk="high",
        expected_changes=["/srv/appe2e is created"],
        postconditions=[{"checker": "git_repository", "args": {"repository": "/srv/appe2e"}}],
    )
    plan = ChangePlan(
        plan_id="priv-ops-test",
        goal="deploy",
        risk="high",
        steps=[step],
        resources=[
            PlanResource(
                name="instance_root",
                kind="path",
                status="frozen",
                role="instance_root",
                value="/srv/appe2e",
                source="user_input",
            )
        ],
    )

    plan.authorize()
    assert plan.is_authorized is True
    step.execution_binding = ExecutionBinding(
        kind="registered_action",
        risk="high",
        action="git_operation",
        args={"operation": "clone", "repository": "/srv/appe2e", "url": "gitee:x/y.git"},
        postconditions=step.postconditions,
    )
    assert plan.is_authorized is False


def test_container_plan_requires_docker_image_discovery_before_binding():
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan,
        ChangeStep,
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    plan = ChangePlan(
        plan_id="priv-ops-images",
        goal="deploy isolated redis",
        risk="high",
        steps=[
            ChangeStep(
                step_id="redis",
                title="Provision isolated stateful containers",
                objective="Create the new MySQL, Redis, and RabbitMQ containers",
                risk="high",
                expected_changes=["container is running"],
                postconditions=[
                    {"checker": "container_running", "args": {"container": "v4e2e-redis"}}
                ],
            )
        ],
    )
    bundle = EvidenceBundle(goal=plan.goal)

    missing = ChangePlannerAgent.finalize_candidate(plan, bundle)

    assert missing.status == "need_evidence"
    assert missing.evidence_requests[0].probe == "docker_images"
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("docker_images", {}, "select an installed image"),
            "inspect_docker_images\nredis latest sha256:a sha256:b now 1MB",
        )
    )
    assert ChangePlannerAgent.finalize_candidate(plan, bundle).status == "need_execution"


def test_container_candidate_batches_port_and_image_discovery():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import (
        ChangePlan,
        ChangeStep,
        EvidenceBundle,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    port = PlanResource(
        "redis_port", "port", "frozen", "redis_port", 45557, "planner_choice"
    )
    plan = ChangePlan(
        plan_id="priv-ops-batch",
        goal="deploy isolated containers",
        risk="high",
        resources=[port],
        steps=[
            ChangeStep(
                step_id="stateful",
                title="Provision isolated stateful containers",
                objective="Create new MySQL, Redis, and RabbitMQ containers",
                risk="high",
                expected_changes=["containers run"],
                postconditions=[{"checker": "exit_code_zero", "args": {}}],
            )
        ],
    )

    outcome = ChangePlannerAgent.finalize_candidate(
        plan, EvidenceBundle(goal=plan.goal)
    )

    assert {request.probe for request in outcome.evidence_requests} == {
        "ports", "docker_images"
    }


def test_workflow_store_uses_separate_directory_and_recovers_without_reexecution(tmp_path):
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    plan = ChangePlan(
        plan_id="priv-ops-recover",
        goal="deploy",
        risk="medium",
        steps=[
            ChangeStep(
                step_id="write",
                title="write config",
                objective="write config",
                risk="medium",
                expected_changes=["config changes"],
                postconditions=[{"checker": "file_exists", "args": {"path": "/srv/config"}}],
                status="running",
            )
        ],
        status="executing",
    )
    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")

    store.save(plan)
    recovered = store.recover(plan.plan_id)

    assert "privileged_ops_plans" in str(store.plan_dir)
    assert recovered.status == "paused"
    assert recovered.steps[0].status == "execution_unknown"
    assert recovered.steps[0].execution_attempts == 0


def test_workflow_store_recovers_interrupted_hierarchical_step_without_reexecution(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.workflow.contracts import ChangePlan, ChangeStep
    from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore

    micro = PrivilegedStep(
        step_id="deploy-1",
        title="start",
        risk="high",
        status="running",
        execution_attempts=1,
        execution_binding=ExecutionBinding(
            kind="registered_action",
            risk="high",
            action="service_control",
            args={"service": "v4e2e", "operation": "start"},
            postconditions=[{"checker": "exit_code_zero"}],
        ),
    )
    plan = ChangePlan(
        plan_id="priv-ops-nested",
        goal="deploy",
        risk="high",
        steps=[
            ChangeStep(
                step_id="deploy",
                title="deploy",
                objective="deploy",
                risk="high",
                expected_changes=["instance created"],
                postconditions=[{"checker": "exit_code_zero"}],
                status="running",
                implementation_plan=ImplementationPlan(
                    implementation_id="impl-deploy",
                    semantic_step_id="deploy",
                    objective="deploy",
                    steps=[micro],
                    status="executing",
                ),
            )
        ],
        status="executing",
    )
    store = ChangePlanStore(tmp_path, user_id="u", project_id="p")
    store.save(plan)

    recovered = store.recover(plan.plan_id)

    nested = recovered.steps[0].implementation_plan.steps[0]
    assert recovered.status == "paused"
    assert nested.status == "execution_unknown"
    assert nested.execution_attempts == 1


def test_change_planner_returns_structured_evidence_gap_without_plan():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, _ = _bundle_and_conclusion()
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "need_evidence",
                    "probe_requests": [
                        {
                            "probe": "ports",
                            "args": {"ports": [47001]},
                            "purpose": "freeze an unused port",
                        }
                    ],
                }
            )
        ]
    )

    outcome = ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    assert outcome.status == "need_evidence"
    assert outcome.plan is None
    assert outcome.evidence_requests[0].probe == "ports"


def test_planner_rejects_redundant_source_probe_when_screen_git_is_authoritative():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="use Screen prefix vemu_uestc as source")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("screen", {}, "source"),
            (
                "session=vemu_uestc_m runtime_cwds=/home/lzl/vemu_uestc/mains "
                "git_roots=/home/lzl/vemu_uestc\n"
                "path=/home/lzl/vemu_uestc inside_work_tree=true revision=abc\n"
                "status=## develop...origin/develop\n"
                "remotes=origin\tgitee:example/vemu.git (fetch)"
            ),
        )
    )
    assert ChangePlannerAgent._authoritative_screen_source_roots(
        "use the selected Screen source",
        bundle,
    ) == {"/home/lzl/vemu_uestc"}
    section_only = EvidenceBundle(goal="use the selected Screen source")
    section_only.add(
        EvidenceRecord.from_probe(
            ProbeRequest("screen", {}, "source"),
            (
                "path=/home/lzl/vemu_uestc inside_work_tree=true revision=abc\n"
                "status=## develop...origin/develop\n"
                "remotes=origin\tgitee:example/vemu.git (fetch)"
            ),
        )
    )
    assert ChangePlannerAgent._authoritative_screen_source_roots(
        section_only.goal,
        section_only,
    ) == {"/home/lzl/vemu_uestc"}
    derived_only = EvidenceBundle(goal="use Screen source")
    derived_only.add(
        EvidenceRecord.from_probe(
            ProbeRequest(
                "git_repository",
                {"repository": "/home/lzl/vemu_uestc"},
                "derived authoritative Screen source Git repository",
            ),
            "inside_work_tree=true remote=gitee:example/vemu.git branch=develop",
        )
    )
    assert ChangePlannerAgent._authoritative_screen_source_roots(
        derived_only.goal,
        derived_only,
    ) == {"/home/lzl/vemu_uestc"}

    with pytest.raises(ValueError, match="authoritative Screen source evidence"):
        ChangePlannerAgent(None)._outcome(
            {
                "status": "need_evidence",
                "probe_requests": [
                    {
                        "probe": "git_repository",
                        "args": {"repository": "/home/lzl/vemu_uestc"},
                        "purpose": "determine source remote and branch",
                    }
                ],
            },
            bundle.goal,
            bundle,
        )
    mixed = ChangePlannerAgent(None)._outcome(
        {
            "status": "need_evidence",
            "probe_requests": [
                {
                    "probe": "git_repository",
                    "args": {"repository": "/home/lzl/vemu_uestc"},
                    "purpose": "determine source remote and branch",
                },
                {
                    "probe": "ports",
                    "args": {"ports": [47001]},
                    "purpose": "verify candidate port",
                },
            ],
        },
        bundle.goal,
        bundle,
    )
    assert [item.probe for item in mixed.evidence_requests] == ["ports"]


def test_change_planner_builds_only_mutating_change_steps():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "ready",
                    "goal": "deploy v4e2e",
                    "assumptions": [],
                    "resources": [
                        {
                            "name": "instance_root",
                            "kind": "path",
                            "status": "frozen",
                            "role": "instance_root",
                            "value": "/srv/appe2e",
                            "source": "user_input",
                            "consumers": ["clone.repository"],
                        },
                        {
                            "name": "source_remote",
                            "kind": "identifier",
                            "status": "frozen",
                            "role": "source_remote",
                            "value": "gitee:example/platform.git",
                            "source": "evidence",
                            "consumers": ["clone.url"],
                        },
                        {
                            "name": "source_branch",
                            "kind": "identifier",
                            "status": "frozen",
                            "role": "source_branch",
                            "value": "develop",
                            "source": "evidence",
                            "consumers": ["clone.ref"],
                        },
                        {
                            "name": "instance_identifier",
                            "kind": "identifier",
                            "status": "frozen",
                            "role": "instance_identifier",
                            "value": "v4e2e",
                            "source": "user_input",
                            "consumers": ["clone.instance_name"],
                        },
                        {
                            "name": "master_port",
                            "kind": "port",
                            "status": "frozen",
                            "role": "master_port",
                            "value": 47001,
                            "source": "evidence",
                            "consumers": ["clone.port"],
                        },
                    ],
                    "changes": [
                        {
                            "step_id": "clone",
                            "title": "clone source",
                            "objective": "clone source into /srv/appe2e",
                            "reason": "isolated deployment",
                            "evidence_refs": [evidence_id],
                            "depends_on": [],
                            "risk": "high",
                            "expected_changes": ["/srv/appe2e is created"],
                            "postconditions": [
                                {
                                    "checker": "file_exists",
                                    "args": {"path": "/srv/appe2e/.git"},
                                }
                            ],
                        }
                    ],
                }
            )
        ]
    )

    outcome = ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    assert outcome.status == "need_execution"
    assert outcome.plan.schema_version == 4
    assert outcome.plan.steps[0].risk == "high"
    assert outcome.plan.steps[0].evidence_refs == [evidence_id]


def test_change_planner_rejects_readonly_or_summary_step_safely():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    payload = {
        "status": "ready",
        "goal": "deploy",
        "resources": [],
        "changes": [
            {
                "step_id": "summarize",
                "title": "形成去重后的平台清单",
                "objective": "summarize evidence",
                "reason": "answer user",
                "evidence_refs": [evidence_id],
                "depends_on": [],
                "risk": "readonly",
                "expected_changes": [],
                "postconditions": [],
            }
        ],
    }
    llm = FakeLLM([json.dumps(payload), json.dumps(payload)])

    outcome = ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    assert outcome.status == "blocked"
    assert "cannot be readonly" in outcome.reason


def test_change_planner_exhausted_schema_repair_returns_blocked_with_strict_hint():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    invalid = json.dumps(
        {
            "status": "ready",
            "goal": "restart isolated service",
            "resources": [],
            "changes": [
                {
                    "step_id": "clone",
                    "title": "clone",
                    "objective": "clone source",
                    "evidence_refs": [evidence_id],
                    "risk": "high",
                    "expected_changes": ["repository created"],
                }
            ],
        }
    )
    llm = FakeLLM([invalid, invalid])

    outcome = ChangePlannerAgent(llm).plan(
        "restart isolated service", bundle, conclusion
    )

    assert outcome.status == "blocked"
    assert "postconditions" in outcome.reason
    repair_prompt = llm.calls[1]["messages"][-1]["content"]
    assert '"postconditions"' in repair_prompt
    assert '"checker"' in repair_prompt


def test_change_planner_repairs_blocked_discoverable_implementation_details():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    invalid_block = json.dumps(
        {
            "status": "blocked",
            "reason": "implementation details missing",
            "missing_decisions": [
                "port selection",
                "IP addresses",
                "nginx config content",
                "screen session names",
                "startup commands",
            ],
        }
    )
    ready = json.dumps(
        {
            "status": "ready",
            "goal": "restart isolated service",
            "resources": [],
            "changes": [
                {
                    "step_id": "deploy",
                    "title": "deploy isolated instance",
                    "objective": "deploy isolated instance",
                    "reason": "user requested deployment",
                    "evidence_refs": [evidence_id],
                    "depends_on": [],
                    "risk": "high",
                    "expected_changes": ["isolated instance is created"],
                    "postconditions": [
                        {
                            "checker": "file_exists",
                            "args": {"path": "/srv/appe2e"},
                        }
                    ],
                }
            ],
        }
    )
    llm = FakeLLM([invalid_block, ready])

    outcome = ChangePlannerAgent(llm).plan(
        "restart isolated service", bundle, conclusion
    )

    assert outcome.status == "need_execution"
    assert len(llm.calls) == 2
    assert len(llm.calls) == 2
    assert "Discovery or Binding" in llm.calls[1]["messages"][-1]["content"]


def test_change_planner_allows_three_bounded_contract_repairs():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    invalid = json.dumps(
        {
            "status": "blocked",
            "reason": "implementation details missing",
            "missing_decisions": ["port selection"],
        }
    )
    ready = json.dumps(
        {
            "status": "ready",
            "goal": "restart isolated service",
            "resources": [],
            "changes": [
                {
                    "step_id": "restart",
                    "title": "restart isolated service",
                    "objective": "restart isolated service",
                    "reason": "requested",
                    "evidence_refs": [evidence_id],
                    "depends_on": [],
                    "risk": "high",
                    "expected_changes": ["service restarted"],
                    "postconditions": [
                        {"checker": "service_active", "args": {"service": "demo"}}
                    ],
                }
            ],
        }
    )
    llm = FakeLLM([invalid, invalid, invalid, ready])

    outcome = ChangePlannerAgent(llm).plan(
        "restart isolated service", bundle, conclusion
    )

    assert outcome.status == "need_execution"
    assert len(llm.calls) == 4


def test_change_planner_repairs_impossible_logs_request_for_missing_process():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    invalid = {
        "status": "need_evidence",
        "probe_requests": [
            {
                "probe": "process_logs",
                "args": {"project_root": "/srv/missing-role"},
                "purpose": "read logs for a role confirmed not running",
            }
        ],
    }
    ready = {
        "status": "ready",
        "goal": "start missing master",
        "resources": [],
        "changes": [
            {
                "step_id": "start", "title": "Start missing master",
                "objective": "Start master for /srv/missing-role",
                "reason": "running inventory confirms role_not_running",
                "evidence_refs": [evidence_id], "depends_on": [],
                "risk": "medium", "expected_changes": ["master starts"],
                "postconditions": [
                    {"checker": "process_running", "args": {"pattern": "master_main"}}
                ],
            }
        ],
    }
    llm = FakeLLM([json.dumps(invalid), json.dumps(ready)])

    outcome = ChangePlannerAgent(llm).plan(
        "start missing master", bundle, conclusion
    )

    assert outcome.status == "need_execution"


def test_existing_runtime_role_port_overrides_source_literal_for_repair():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="repair 102 master and verify worker")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        (
            "platform=102 project_root=/home/klonet-agent/102 "
            "master_port=27694 worker_port=27695 "
            "configured_ports=master_port:27694,worker_port:27695 "
            "backend_status=abnormal"
        ),
    ))
    data = {
        "changes": [
            {
                "step_id": "repair",
                "title": "Restart 102 master",
                "objective": (
                    "Restart master for /home/klonet-agent/102 and verify worker "
                    "on mistakenly inferred port 12000"
                ),
                "postconditions": [
                    {
                        "checker": "backend_health",
                        "args": {"url": "http://127.0.0.1:12000/server_health/"},
                    }
                ],
            }
        ]
    }
    resource = PlanResource(
        name="worker_port",
        kind="port",
        status="frozen",
        role="worker_port",
        value=12000,
        source="planner_decision",
        consumers=["repair.worker_port"],
    )

    ChangePlannerAgent._compile_existing_runtime_role_ports(
        data,
        [resource],
        bundle,
    )

    assert resource.value == 27695
    assert resource.source == "existing_runtime"
    assert data["changes"][0]["postconditions"][0]["args"]["url"] == (
        "http://127.0.0.1:27695/server_health/"
    )


def test_change_planner_repairs_recheck_of_confirmed_missing_role():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceRecord, ProbeRequest
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("running_platforms", {}, "inventory"),
            "inspect_running_platforms\n"
            "platform=demo project_root=/srv/demo backend_status=abnormal "
            "master_endpoint=not_checked reason=role_not_running "
            "worker_endpoint=not_checked reason=role_not_running",
        )
    )
    invalid = {
        "status": "need_evidence",
        "probe_requests": [
            {
                "probe": "screen_session",
                "args": {"session": "demo_m"},
                "purpose": "confirm whether missing master screen exists",
            }
        ],
    }
    ready = {
        "status": "ready", "goal": "start missing master", "resources": [],
        "changes": [
            {
                "step_id": "start", "title": "Start missing master",
                "objective": "Start master for /srv/demo", "reason": "role missing",
                "evidence_refs": [evidence_id], "depends_on": [], "risk": "medium",
                "expected_changes": ["master starts"],
                "postconditions": [
                    {"checker": "process_running", "args": {"pattern": "master_main"}}
                ],
            }
        ],
    }
    llm = FakeLLM([json.dumps(invalid), json.dumps(ready)])

    outcome = ChangePlannerAgent(llm).plan(
        "start missing master", bundle, conclusion
    )

    assert outcome.status == "need_execution"
    assert len(llm.calls) == 2


def test_change_planner_bounds_model_output_and_omits_runaway_repair_context():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, _evidence_id = _bundle_and_conclusion()
    runaway = '{"status":"ready","assumptions":[' + ("x" * 40000)
    blocked = json.dumps(
        {
            "status": "blocked",
            "reason": "bounded test stop",
            "missing_decisions": [],
        }
    )
    llm = FakeLLM([runaway, blocked])

    outcome = ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    assert outcome.status == "blocked"
    assert len(llm.calls) == 2
    assert llm.calls[0]["kwargs"]["max_tokens"] == 8000
    repair_messages = llm.calls[1]["messages"]
    assistant = next(
        item["content"] for item in repair_messages if item["role"] == "assistant"
    )
    assert assistant == "Previous planner output omitted: contract size exceeded."
    assert "at most 4 semantic changes" in repair_messages[-1]["content"]


def test_change_planner_compacts_multi_root_repairs_before_first_generation():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, _evidence_id = _bundle_and_conclusion()
    blocked = json.dumps(
        {
            "status": "blocked",
            "reason": "test stop",
            "missing_decisions": [],
        }
    )
    llm = FakeLLM([blocked])

    ChangePlannerAgent(llm).plan(
        "repair /home/lzl/vemu_uestc and /home/lzl/test/vemu_uestc",
        bundle,
        conclusion,
    )

    messages = llm.calls[0]["messages"]
    assert "at most 4 semantic changes" in messages[-1]["content"]
    assert "Binding" in messages[-1]["content"]


def test_change_planner_stops_after_compact_retry_is_still_oversized():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, _evidence_id = _bundle_and_conclusion()
    runaway = '{"status":"ready","changes":[' + ("x" * 40000)
    llm = FakeLLM([runaway, runaway, runaway])

    outcome = ChangePlannerAgent(llm).plan("repair /srv/a and /srv/b", bundle, conclusion)

    assert outcome.status == "blocked"
    assert "bounded output" in outcome.reason
    assert len(llm.calls) == 2


def test_change_planner_forces_bounded_function_schema():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, _evidence_id = _bundle_and_conclusion()
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "test stop",
                    "missing_decisions": [],
                }
            )
        ]
    )

    ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    call = llm.calls[0]
    assert call["kwargs"]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_change_plan"},
    }
    function = call["tools"][0]["function"]
    assert function["name"] == "submit_change_plan"
    properties = function["parameters"]["properties"]
    assert properties["assumptions"]["maxItems"] == 12
    assert properties["assumptions"]["items"]["maxLength"] == 500
    assert properties["resources"]["maxItems"] == 64
    assert properties["changes"]["maxItems"] == 12


def test_planner_compiles_checker_aliases_and_clone_resource_consumers():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "step_id": "clone-source",
                "title": "Clone source repository",
                "objective": "Clone the authoritative Git repository",
                "postconditions": [
                    {
                        "checker": "container_running",
                        "args": {"name": "v4e2e-mysql"},
                    },
                    {
                        "checker": "git_revision",
                        "args": {"path": "/srv/appe2e", "revision": "abc"},
                    },
                    {
                        "checker": "file_contains",
                        "args": {"path": "/srv/appe2e/config.py", "pattern": "x"},
                    },
                ],
            }
        ]
    }
    resources = [
        PlanResource("root", "path", "frozen", "instance_root", "/srv/appe2e"),
        PlanResource("remote", "identifier", "frozen", "source_remote", "g:x/y"),
        PlanResource("branch", "identifier", "frozen", "source_branch", "develop"),
    ]

    ChangePlannerAgent._normalize_postcondition_args(data)
    ChangePlannerAgent._normalize_core_resource_consumers(data, resources)

    assert data["changes"][0]["postconditions"][0]["args"] == {
        "container": "v4e2e-mysql"
    }
    assert data["changes"][0]["postconditions"][1]["args"] == {
        "repository": "/srv/appe2e",
        "revision": "abc",
    }
    assert data["changes"][0]["postconditions"][2]["args"] == {
        "path": "/srv/appe2e/config.py",
        "text": "x",
    }
    assert resources[0].consumers == ["clone-source.repository"]
    assert resources[1].consumers == ["clone-source.url"]
    assert resources[2].consumers == ["clone-source.ref"]


def test_planner_compiles_runtime_dependencies_and_stale_nginx_consumers():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {"step_id": "clone", "title": "Clone Git source repository", "objective": "Clone source", "depends_on": []},
            {"step_id": "redis", "title": "Provision Redis container", "objective": "Create new Redis container", "depends_on": []},
            {"step_id": "runtime", "title": "Start complete Screen runtime", "objective": "Start master celery worker web terminal", "depends_on": []},
            {"step_id": "nginx", "title": "Activate Nginx site", "objective": "Reload Nginx", "depends_on": []},
        ]
    }
    resources = [
        PlanResource(
            "nginx_port", "port", "frozen", "nginx_listen_port", 47008,
            "evidence", consumers=["change-99.listen"],
        ),
        PlanResource(
            "instance_root", "path", "frozen", "instance_root", "/srv/appe2e",
            "user_input", consumers=["change-99.instance_root"],
        ),
    ]

    ChangePlannerAgent._normalize_semantic_dependencies(data)
    ChangePlannerAgent._normalize_resource_consumer_owners(data, resources)

    by_id = {item["step_id"]: item for item in data["changes"]}
    assert by_id["redis"]["depends_on"] == ["clone"]
    assert by_id["runtime"]["depends_on"] == ["redis"]
    assert by_id["nginx"]["depends_on"] == ["runtime"]
    assert resources[0].consumers == ["nginx.listen"]
    assert resources[1].consumers == ["nginx.instance_root"]


def test_planner_prunes_stale_consumers_and_grounds_future_paths_to_matching_steps():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "step_id": "change-1",
                "title": "Clone source",
                "objective": "Clone into /home/lzl/klonet_workflow_e2e",
            },
            {
                "step_id": "change-3",
                "title": "Configure application",
                "objective": "Edit /home/lzl/klonet_workflow_e2e/vemu_config/config.py",
            },
            {
                "step_id": "change-5",
                "title": "Activate Nginx",
                "objective": "Create /etc/nginx/sites-available/klonet-v4-e2e",
            },
        ]
    }
    resources = [
        PlanResource(
            "instance_identifier", "identifier", "frozen",
            "instance_identifier", "v4e2e", "user_input",
            consumers=["change-1.instance_name", "change-6.instance_name"],
        ),
        PlanResource(
            "config_path", "path", "frozen", "config_path",
            "/home/lzl/klonet_workflow_e2e/vemu_config/config.py", "derived",
            consumers=["change-5.path", "change-6.path"],
        ),
        PlanResource(
            "nginx_config_path", "path", "frozen", "nginx_config_path",
            "/etc/nginx/sites-available/klonet-v4-e2e", "derived",
            consumers=["change-5.path", "change-6.path"],
        ),
    ]

    ChangePlannerAgent._normalize_resource_consumer_owners(data, resources)

    assert resources[0].consumers == ["change-1.instance_name"]
    assert resources[1].consumers == ["change-3.path"]
    assert resources[2].consumers == ["change-5.path"]


def test_planner_compiles_canonical_container_names_and_runtime_entry_source():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "resources": [
            {
                "name": "instance_identifier",
                "kind": "identifier",
                "status": "frozen",
                "role": "instance_identifier",
                "value": "v4e2e",
                "source": "user_input",
                "consumers": ["mysql.container"],
            },
            {
                "name": "mysql_container",
                "kind": "identifier",
                "status": "frozen",
                "role": "container_name",
                "value": "v4e2e_mysql",
                "source": "derived",
                "consumers": ["mysql.container_name"],
            },
        ],
        "changes": [
            {
                "step_id": "mysql",
                "title": "Provision v4e2e_mysql container",
                "objective": "Create v4e2e_mysql",
                "postconditions": [
                    {"checker": "container_running", "args": {"name": "v4e2e_mysql"}}
                ],
            },
            {
                "step_id": "master",
                "title": "Start master Screen component",
                "objective": "Launch Screen session v4e2e_m",
                "postconditions": [],
            },
        ],
    }

    ChangePlannerAgent._normalize_instance_container_names(data)
    resources = [
        PlanResource.from_dict(item) for item in data["resources"]
    ] + [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root",
            "/home/lzl/klonet_workflow_e2e", "user_input",
        )
    ]
    resources = ChangePlannerAgent._normalize_derived_resources(data, resources)

    assert data["resources"][1]["value"] == "v4e2e-mysql"
    assert data["changes"][0]["postconditions"][0]["args"]["name"] == "v4e2e-mysql"
    mains = next(item for item in resources if item.role == "runtime_mains_root")
    assert mains.value == "/home/lzl/klonet_workflow_e2e/mains"
    assert mains.consumers == ["master.source_root"]


def test_planner_uses_existing_nested_backend_mains_for_runtime_root():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        instance_root = temp_dir / "klonet"
        nested_mains = instance_root / "vemu_uestc" / "mains"
        nested_mains.mkdir(parents=True)
        for name in REQUIRED_ENTRY_FILES:
            (nested_mains / name).write_text("# entry\n", encoding="utf-8")
        data = {
            "changes": [
                {
                    "step_id": "start", "title": "Start master screen",
                    "objective": "Start master for %s" % instance_root,
                    "expected_changes": ["master starts"], "postconditions": [],
                }
            ]
        }
        resources = [
            PlanResource(
                "instance_root", "path", "frozen", "instance_root",
                str(instance_root), "evidence", consumers=["start.instance_root"],
            )
        ]

        normalized = ChangePlannerAgent._normalize_derived_resources(
            data, resources
        )

    mains = next(item for item in normalized if item.role == "runtime_mains_root")
    assert mains.value == str(nested_mains)
    assert mains.consumers == ["start.source_root"]


def test_planner_derives_config_path_after_checker_path_normalization():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "step_id": "config",
                "title": "Configure v4e2e in vemu_config/config.py",
                "objective": "Set WtxConfig ports and IPs",
                "postconditions": [
                    {
                        "checker": "python_attribute_equals",
                        "args": {
                            "module": "vemu_config.config",
                            "attribute": "master_port",
                            "expected": 47001,
                        },
                    }
                ],
            }
        ]
    }
    root = PlanResource(
        "instance_root", "path", "frozen", "instance_root",
        "/home/lzl/klonet_workflow_e2e", "user_input",
        consumers=["clone.repository"],
    )

    normalized = ChangePlannerAgent._normalize_derived_resources(data, [root])

    config = next(item for item in normalized if item.role == "config_path")
    assert config.value == "/home/lzl/klonet_workflow_e2e/vemu_config/config.py"
    assert config.consumers == ["config.path"]


def test_ports_probe_explicitly_reports_checked_occupied_and_available(monkeypatch):
    from klonet_agent.ops.privileged import probes

    monkeypatch.setattr(
        probes,
        "_run",
        lambda *_args, **_kwargs: (
            "State Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
            "LISTEN 0 128 127.0.0.1:47002 0.0.0.0:*"
        ),
    )

    result = probes._ports({"ports": [47001, 47002, 47003]})

    assert "checked_ports=47001,47002,47003" in result
    assert "occupied_ports=47002" in result
    assert "available_ports=47001,47003" in result


def test_planner_compiles_python_checker_aliases_and_drops_dependency_install():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "resources": [
            {"value": "/srv/appe2e/vemu_config/config.py"},
        ],
        "changes": [
            {"step_id": "clone", "depends_on": [], "postconditions": []},
            {
                "step_id": "install",
                "title": "Install Python dependencies",
                "objective": "pip install requirements",
                "depends_on": ["clone"],
                "postconditions": [],
            },
            {
                "step_id": "config",
                "title": "Configure instance settings",
                "depends_on": ["install"],
                "postconditions": [
                    {
                        "checker": "python_attribute_equals",
                        "args": {
                            "path": "/srv/appe2e/vemu_config/config.py",
                            "attribute": "master_port",
                            "value": 47001,
                        },
                    },
                    {
                        "checker": "python_import_succeeds",
                        "args": {"path": "/srv/appe2e/vemu_config/config.py"},
                    },
                ],
            },
        ],
    }

    ChangePlannerAgent._normalize_ungrounded_dependency_installs(data)
    ChangePlannerAgent._normalize_postcondition_args(data)

    assert [item["step_id"] for item in data["changes"]] == ["clone", "config"]
    assert data["changes"][1]["depends_on"] == ["clone"]
    assert data["changes"][1]["postconditions"][0]["args"] == {
        "module": "vemu_config.config",
        "attribute": "master_port",
        "expected": 47001,
    }
    assert data["changes"][1]["postconditions"][1]["args"] == {
        "module": "vemu_config.config",
    }


@pytest.mark.parametrize("alias", ["candidates", "candidate_ports"])
def test_planner_canonicalizes_port_probe_candidate_aliases(alias):
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    requests = ChangePlannerAgent._probe_requests(
        [
            {
                "probe": "ports",
                "args": {alias: [45561, "45562", 45561]},
                "purpose": "freeze candidates",
            }
        ]
    )

    assert requests[0].args == {"ports": [45561, 45562]}


def test_planner_bounds_port_probe_candidates():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    requests = ChangePlannerAgent._probe_requests(
        [
            {
                "probe": "ports",
                "args": {"candidate_ports": list(range(10000, 10200))},
                "purpose": "find a small free set",
            }
        ]
    )

    assert requests[0].args == {"ports": list(range(10000, 10064))}


def test_planner_routes_python_source_log_request_to_ops_file():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    requests = ChangePlannerAgent._probe_requests([{
        "probe": "logs",
        "args": {"path": "/srv/102/mains/master_main.py"},
        "purpose": "inspect startup entry source",
    }])

    assert requests[0].probe == "ops_file"
    assert requests[0].args["view"] == "head"
    assert requests[0].args["max_chars"] == 20000


def test_planner_reassigns_occupied_host_port_from_probed_free_candidates():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="deploy")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest(
                "ports",
                {"ports": [45551, 45552, 5011]},
                "freeze candidates",
            ),
            "inspect_ports\nLISTEN 0 128 127.0.0.1:45551 0.0.0.0:*",
        )
    )
    data = {
        "changes": [
            {
                "step_id": "start",
                "title": "Start master on port 45551",
                "objective": "Listen on 45551",
                "postconditions": [
                    {"checker": "port_listening", "args": {"port": 45551}}
                ],
            }
        ]
    }
    resources = [
        PlanResource(
            "master_port", "port", "frozen", "master_port", 45551,
            "model_selected", consumers=["start.master_port"],
        ),
        PlanResource(
            "worker_port", "port", "frozen", "worker_port", 45552,
            "model_selected", consumers=["start.worker_port"],
        ),
    ]

    ChangePlannerAgent._normalize_occupied_host_ports(data, resources, bundle)

    assert resources[0].value == 5011
    assert resources[1].value == 45552
    assert data["changes"][0]["title"] == "Start master on port 5011"
    assert data["changes"][0]["postconditions"][0]["args"]["port"] == 5011


def test_planner_never_reassigns_observed_old_runtime_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="migrate test worker")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("ports", {"ports": [45552, 45554, 45555]}, "candidates"),
        "LISTEN 0 128 0.0.0.0:45552\n",
    ))
    data = {"changes": [{
        "step_id": "stop-test",
        "title": "Stop root-bound worker on port 45552",
        "objective": (
            "Stop worker under /home/lzl/test/vemu_uestc that owns 45552"
        ),
        "postconditions": [
            {"checker": "port_not_listening", "args": {"port": 45552}},
        ],
    }]}
    resource = PlanResource(
        "test_worker_old_port",
        "port",
        "frozen",
        "test_worker_old_port",
        45552,
        "evidence",
        consumers=["stop-test.test_worker_old_port"],
    )

    ChangePlannerAgent._normalize_occupied_host_ports(
        data, [resource], bundle,
    )

    assert resource.value == 45552
    assert resource.source == "evidence"
    assert data["changes"][0]["title"].endswith("45552")
    assert data["changes"][0]["postconditions"][0]["args"]["port"] == 45552


def test_planner_preserves_occupied_ports_owned_by_repaired_runtime_root():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="repair formal")
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("running_platforms", {}, "inventory"),
            "platform=vemu_uestc project_root=/home/lzl/vemu_uestc "
            "backend_status=abnormal master_port=45551 worker_port=45552",
        )
    )
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("ports", {"ports": [45551, 45552, 5011]}, "ports"),
            "LISTEN 0 128 127.0.0.1:45551\nLISTEN 0 128 127.0.0.1:45552",
        )
    )
    data = {
        "changes": [
            {
                "step_id": "formal",
                "title": "Restore formal worker",
                "objective": "Repair /home/lzl/vemu_uestc on 45551 and 45552",
                "postconditions": [
                    {"checker": "port_listening", "args": {"port": 45552}}
                ],
            }
        ]
    }
    resources = [
        PlanResource(
            "formal_master_port", "port", "frozen", "master_port", 45551,
            "evidence", consumers=["formal.master_port"],
        ),
        PlanResource(
            "formal_worker_port", "port", "frozen", "worker_port", 45552,
            "evidence", consumers=["formal.worker_port"],
        ),
    ]

    ChangePlannerAgent._mark_existing_runtime_ports(data, resources, bundle)
    ChangePlannerAgent._normalize_occupied_host_ports(data, resources, bundle)

    assert [resource.value for resource in resources] == [45551, 45552]
    assert {resource.source for resource in resources} == {"existing_runtime"}
    assert data["changes"][0]["postconditions"][0]["args"]["port"] == 45552


def test_generic_derived_port_matching_repaired_root_is_existing_runtime():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="repair formal")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime"),
        "platform=vemu project_root=/home/lzl/vemu_uestc "
        "backend_status=abnormal master_port=45551 worker_port=45552",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("ports", {"ports": [45551, 45556]}, "candidates"),
        "LISTEN 0 128 0.0.0.0:45551",
    ))
    data = {"changes": [{
        "step_id": "formal",
        "title": "Restore formal master",
        "objective": "Restart master for /home/lzl/vemu_uestc on 45551",
        "postconditions": [],
    }]}
    resource = PlanResource(
        "derived_host_port_45551", "port", "frozen", "host_port", 45551,
        "derived", consumers=["formal.port_45551"],
    )

    ChangePlannerAgent._mark_existing_runtime_ports(data, [resource], bundle)
    ChangePlannerAgent._normalize_occupied_host_ports(data, [resource], bundle)

    assert resource.value == 45551
    assert resource.source == "existing_runtime"
    assert resource.role == "master_port"


def test_planner_reassigns_new_port_that_collides_with_another_active_config_role():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="isolate test")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        "platform=vemu_uestc project_root=/home/lzl/vemu_uestc "
        "configured_ports=master_port:45551,worker_port:45552,public_port:45553",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("ports", {"ports": [45553, 45554]}, "candidates"),
        "inspect_ports\nno matching listeners",
    ))
    data = {"changes": [
        {
            "step_id": "test",
            "title": "Migrate test master from 45551 to 45553",
            "objective": "Configure /home/lzl/test/vemu_uestc master on 45553",
            "postconditions": [{"checker": "port_listening", "args": {"port": 45553}}],
        },
        {
            "step_id": "formal",
            "title": "Confirm formal public port 45553",
            "objective": "Keep /home/lzl/vemu_uestc public port 45553",
            "postconditions": [],
        },
    ]}
    resources = [
        PlanResource(
            "test_master", "port", "frozen", "master_port", 45553,
            "evidence", consumers=["test.master_port"],
        ),
        PlanResource(
            "formal_public", "port", "frozen", "public_port", 45553,
            "evidence", consumers=["formal.public_port"],
        ),
    ]

    ChangePlannerAgent._mark_existing_config_ports(data, resources, bundle)
    ChangePlannerAgent._normalize_occupied_host_ports(data, resources, bundle)

    assert resources[0].value == 45554
    assert resources[1].value == 45553
    assert resources[1].source == "existing_config"
    assert data["changes"][0]["postconditions"][0]["args"]["port"] == 45554
    assert "45553" in data["changes"][1]["objective"]


def test_runtime_stop_scope_preserves_non_worker_roles_and_recovery_targets_later_start():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"changes": [
        {
            "step_id": "stop-test",
            "title": "停止 test worker 释放端口 45552",
            "objective": "终止 /home/lzl/test/vemu_uestc worker 和 data_server PIDs 1-9",
            "expected_changes": ["worker stops", "data_server stops", "master stops"],
            "postconditions": [],
        },
        {
            "step_id": "start-test",
            "title": "Start test master and worker",
            "objective": "Start /home/lzl/test/vemu_uestc on new ports",
            "expected_changes": ["master and worker start"],
            "postconditions": [],
        },
    ]}

    ChangePlannerAgent._normalize_runtime_stop_scope(data)

    stop = data["changes"][0]
    assert "only worker processes" in stop["objective"]
    assert "preserve master, data_server" in stop["objective"]
    assert stop["postconditions"] == [
        {"checker": "port_not_listening", "args": {"port": 45552}}
    ]


def test_runtime_stop_scope_uses_authoritative_goal_root_and_worker_port():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    goal = (
        "repair /home/lzl/vemu_uestc and /home/lzl/test/vemu_uestc; "
        "precisely stop the test-root worker occupying 45552"
    )
    bundle = EvidenceBundle(goal=goal)
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime"),
        "platform=vemu project_root=/home/lzl/test/vemu_uestc "
        "backend_status=abnormal master_port=45551 worker_port=45552",
    ))
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("port_owner", {"ports": [45552]}, "owner"),
        "- port_owner: detected - port=45552 pid=3049898 "
        "tree_root_pid=3049898 pgid=3049898 "
        "cmd=python -m gunicorn -c worker_gun.py worker_main:flask_app "
        "cwd=/home/lzl/test/vemu_uestc",
    ))
    data = {
        "changes": [{
            "step_id": "stop-test",
            "title": "Stop test master and worker on port 45551",
            "objective": "Stop master/worker for /home/lzl/test/vemu_uestc listener 45551",
            "expected_changes": [], "postconditions": [],
        }],
        "resources": [{
            "name": "test_old_port", "kind": "port", "role": "old_port",
            "value": 45551, "source": "model", "consumers": ["stop-test.old_port"],
        }],
    }

    ChangePlannerAgent._normalize_runtime_stop_scope(data, goal, bundle)

    stop = data["changes"][0]
    assert stop["title"] == "Stop root-bound worker on port 45552"
    assert "/home/lzl/test/vemu_uestc" in stop["objective"]
    assert data["resources"][0]["value"] == 45552
    assert data["resources"][0]["role"] == "worker_port"
    assert any(
        item["kind"] == "identifier"
        and item["role"] == "worker_pid"
        and item["value"] == 3049898
        and item["consumers"] == ["stop-test.pid"]
        for item in data["resources"]
    )
    assert any(
        item["kind"] == "path"
        and item["role"] == "runtime_cwd"
        and item["value"] == "/home/lzl/test/vemu_uestc"
        for item in data["resources"]
    )


def test_combined_migration_freezes_worker_pgid_from_running_inventory():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    goal = "stop /home/lzl/test/vemu_uestc worker on 45552 then move it to 45555"
    bundle = EvidenceBundle(goal=goal)
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "runtime"),
        "platform=vemu project_root=/home/lzl/test/vemu_uestc roles=master,worker "
        "worker_pids=3049898,3049923 worker_pgids=3049898 "
        "backend_status=healthy master_port=45554 worker_port=45552",
    ))
    data = {
        "changes": [{
            "step_id": "migrate-test",
            "title": "Repair test worker: stop old worker, set port, start worker",
            "objective": "Stop /home/lzl/test/vemu_uestc worker on port 45552 then start on 45555",
            "expected_changes": [], "postconditions": [],
        }],
        "resources": [{
            "name": "old_worker_port", "kind": "port", "role": "worker_port",
            "value": 45552, "source": "existing", "consumers": ["migrate-test.worker_port_old"],
        }],
    }

    ChangePlannerAgent._normalize_runtime_stop_scope(data, goal, bundle)

    assert data["changes"][0]["title"] == "Repair test worker: stop old worker, set port, start worker"
    assert "45555" in data["changes"][0]["objective"]
    assert any(
        resource["kind"] == "identifier"
        and resource["role"] == "worker_pid"
        and resource["value"] == 3049898
        for resource in data["resources"]
    )


def test_runtime_health_recovery_is_attached_to_later_start_not_stop_step():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="repair test")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        "platform=vemu project_root=/home/lzl/test/vemu_uestc "
        "backend_status=abnormal master_port=45551 master_endpoint=unreachable "
        "worker_port=45552 worker_endpoint=healthy",
    ))
    data = {
        "resources": [
            {
                "name": "test_master", "role": "master_port", "value": 45555,
                "consumers": ["start-test.master_port"],
            }
        ],
        "changes": [
            {
                "step_id": "stop-test", "title": "Stop test worker",
                "objective": "Stop /home/lzl/test/vemu_uestc worker",
                "expected_changes": ["worker stops"], "postconditions": [],
            },
            {
                "step_id": "start-test", "title": "Start test master and worker",
                "objective": "Start /home/lzl/test/vemu_uestc runtime",
                "expected_changes": ["master and worker start"], "postconditions": [],
            },
        ],
    }

    ChangePlannerAgent._normalize_runtime_repair_coverage(data, bundle)

    assert all("restart unhealthy" not in item for item in data["changes"][0]["expected_changes"])
    assert any("restart unhealthy master role at 45555" in item for item in data["changes"][1]["expected_changes"])


def test_generic_healthy_outcome_does_not_hide_restart_unhealthy_disposition():
    from klonet_agent.ops.privileged.workflow.contracts import (
        EvidenceBundle, EvidenceRecord, ProbeRequest,
    )
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle = EvidenceBundle(goal="restore formal")
    bundle.add(EvidenceRecord.from_probe(
        ProbeRequest("running_platforms", {}, "inventory"),
        "platform=vemu project_root=/home/lzl/vemu_uestc backend_status=abnormal "
        "master_port=45551 master_endpoint=unreachable worker_port=45552 "
        "worker_endpoint=not_checked reason=role_not_running",
    ))
    data = {
        "resources": [],
        "changes": [{
            "step_id": "formal",
            "title": "Restore production instance master and worker",
            "objective": "Start /home/lzl/vemu_uestc master and worker",
            "expected_changes": ["Production master and worker become healthy"],
            "postconditions": [],
        }],
    }

    ChangePlannerAgent._normalize_runtime_repair_coverage(data, bundle)

    expected = data["changes"][0]["expected_changes"]
    assert any("restart unhealthy master role at 45551" in item for item in expected)
    assert any("start missing worker role at 45552" in item for item in expected)


def test_same_root_master_worker_recovery_changes_are_merged():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "resources": [{
            "name": "worker_port", "consumers": ["worker.worker_port"],
        }],
        "changes": [
            {
                "step_id": "test", "title": "Migrate test runtime",
                "objective": "Migrate /home/lzl/test/vemu_uestc",
                "expected_changes": ["start master and worker"],
                "postconditions": [], "depends_on": [],
            },
            {
                "step_id": "worker", "title": "Restore formal worker",
                "objective": "Start worker for /home/lzl/vemu_uestc",
                "expected_changes": ["worker starts"],
                "postconditions": [{"checker": "port_listening", "args": {"port": 45552}}],
                "depends_on": ["test"],
            },
            {
                "step_id": "master", "title": "Restore formal master",
                "objective": "Restart master for /home/lzl/vemu_uestc",
                "expected_changes": ["master restarts"],
                "postconditions": [{"checker": "port_listening", "args": {"port": 45551}}],
                "depends_on": ["worker"],
            },
        ],
    }

    ChangePlannerAgent._collapse_redundant_runtime_repair_changes(data)

    assert [item["step_id"] for item in data["changes"]] == ["test", "worker"]
    assert "master restarts" in data["changes"][1]["expected_changes"]
    assert data["resources"][0]["consumers"] == ["worker.worker_port"]


def test_same_root_duplicate_stop_is_folded_into_recovery_owner():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "resources": [{
            "name": "project_root", "kind": "path", "status": "frozen",
            "role": "project_root", "value": "/srv/102",
            "consumers": ["repair.project_root", "stop.runtime_cwd"],
        }, {
            "name": "master_pid", "kind": "identifier", "status": "frozen",
            "role": "master_pid", "value": 1234,
            "consumers": ["stop.pid"],
        }],
        "changes": [
            {
                "step_id": "repair", "title": "Repair and recover master",
                "objective": "Recover master for /srv/102",
                "expected_changes": ["restart unhealthy master role"],
                "postconditions": [], "depends_on": [],
            },
            {
                "step_id": "stop", "title": "Stop current master process",
                "objective": "Terminate master for /srv/102",
                "expected_changes": ["PID 1234 stops"],
                "postconditions": [], "depends_on": ["repair"],
            },
        ],
    }

    ChangePlannerAgent._collapse_redundant_runtime_repair_changes(data)

    assert [item["step_id"] for item in data["changes"]] == ["repair"]
    assert "PID 1234 stops" in data["changes"][0]["expected_changes"]
    assert data["resources"][1]["consumers"] == ["repair.pid"]


def test_backend_port_availability_uses_explicit_existing_source():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    existing = PlanResource(
        "worker_port", "port", "frozen", "worker_port", 27695,
        "existing_runtime", consumers=["repair.worker_port"],
    )
    allocated = PlanResource(
        "new_worker_port", "port", "frozen", "worker_port", 45555,
        "planner_decision", consumers=["migrate.worker_port"],
    )

    assert not ChangePlannerAgent._requires_host_port_availability(existing)
    assert ChangePlannerAgent._requires_host_port_availability(allocated)


def test_health_url_only_port_is_derived_as_observation_not_allocation():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"changes": [{
        "step_id": "repair",
        "title": "Repair master",
        "objective": "Restart master on port 27694 and listening on 27694",
        "expected_changes": ["master listens on 27694"],
        "postconditions": [{
            "checker": "backend_health",
            "args": {
                "url": "http://127.0.0.1:27695/server_health/",
                "expected_code": 1,
            },
        }],
    }]}

    resources = ChangePlannerAgent._normalize_derived_resources(data, [])
    by_value = {int(item.value): item for item in resources if item.kind == "port"}

    assert by_value[27694].role == "selected_host_port"
    assert by_value[27695].role == "observed_endpoint_port"
    assert by_value[27695].source == "derived_observation"
    assert not ChangePlannerAgent._requires_host_port_availability(by_value[27695])


def test_same_root_recovery_changes_merge_for_arbitrary_project_name():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "resources": [
            {
                "name": "project_root", "kind": "path", "status": "frozen",
                "role": "project_root", "value": "/srv/platforms/alpha-102",
                "consumers": ["edit.project_root", "restart.project_root"],
            }
        ],
        "changes": [
            {
                "step_id": "edit", "title": "Remove bad startup code",
                "objective": "Repair and recover master for /srv/platforms/alpha-102",
                "expected_changes": ["restart unhealthy master role"],
                "postconditions": [], "depends_on": [],
            },
            {
                "step_id": "restart", "title": "Restart master",
                "objective": "Restart master for /srv/platforms/alpha-102",
                "expected_changes": ["master starts"],
                "postconditions": [], "depends_on": ["edit"],
            },
        ],
    }

    ChangePlannerAgent._collapse_redundant_runtime_repair_changes(data)

    assert [item["step_id"] for item in data["changes"]] == ["edit"]
    assert "master starts" in data["changes"][0]["expected_changes"]
    assert data["resources"][0]["consumers"] == ["edit.project_root"]


def test_instance_root_consumers_are_grounded_to_the_exact_sibling_change():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"changes": [
        {"step_id": "test", "objective": "Repair /home/lzl/test/vemu_uestc"},
        {"step_id": "formal", "objective": "Repair /home/lzl/vemu_uestc"},
    ]}
    resources = [
        PlanResource(
            "test_root", "path", "frozen", "instance_root",
            "/home/lzl/test/vemu_uestc", consumers=["test.instance_root"],
        ),
        PlanResource(
            "formal_root", "path", "frozen", "instance_root",
            "/home/lzl/vemu_uestc",
            consumers=["test.instance_root_2", "formal.instance_root"],
        ),
    ]

    ChangePlannerAgent._normalize_resource_consumer_owners(data, resources)

    assert resources[0].consumers == ["test.instance_root"]
    assert resources[1].consumers == ["formal.instance_root"]


def test_instance_prefixed_port_roles_are_canonicalized_before_health_coverage():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"resources": [
        {"name": "test_master_port", "kind": "port", "role": "test_master_port", "value": 45555},
        {"name": "prod_worker_port", "kind": "port", "role": "worker_port", "value": 45552},
    ]}

    ChangePlannerAgent._normalize_port_resource_roles(data)

    assert [item["role"] for item in data["resources"]] == [
        "master_port", "worker_port",
    ]


def test_new_destination_port_role_is_canonicalized():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"resources": [{
        "name": "test_master_new_port",
        "kind": "port",
        "role": "test_master_new_port",
        "value": 45555,
    }]}

    ChangePlannerAgent._normalize_port_resource_roles(data)

    assert data["resources"][0]["role"] == "master_port"


def test_destination_port_suffix_role_is_canonicalized():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"resources": [{
        "name": "test_worker_port_new",
        "kind": "port",
        "role": "test_worker_port_new",
        "value": 45555,
    }]}

    ChangePlannerAgent._normalize_port_resource_roles(data)

    assert data["resources"][0]["role"] == "worker_port"


def test_config_resource_is_rebound_to_existing_instance_config():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        root = temp_dir / "test" / "vemu_uestc"
        config = root / "vemu_config" / "config.py"
        config.parent.mkdir(parents=True)
        config.write_text("PROJ_CONFIG = WtxConfig()\n", encoding="utf-8")
        data = {"changes": [{
            "step_id": "change-2",
            "objective": "Configure %s" % root,
        }]}
        resources = [
            PlanResource(
                "test_root", "path", "frozen", "instance_root", str(root),
                consumers=["change-2.instance_root"],
            ),
            PlanResource(
                "test_config", "path", "frozen", "config_path",
                str(root / "config.py"), consumers=["change-2.path"],
            ),
        ]

        ChangePlannerAgent._normalize_existing_config_paths(data, resources)

        assert resources[1].value == str(config)
        assert resources[1].source == "derived_from_existing_instance_root"


def test_worker_gun_path_is_not_used_as_platform_port_config_path():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        root = temp_dir / "test" / "vemu_uestc"
        config = root / "vemu_config" / "config.py"
        worker_gun = root / "worker_gun.py"
        config.parent.mkdir(parents=True)
        config.write_text("PROJ_CONFIG = WtxConfig()\n", encoding="utf-8")
        worker_gun.write_text("bind = worker_port\n", encoding="utf-8")
        data = {"changes": [{
            "step_id": "change-1",
            "objective": "Change worker_port for %s from 45552 to 45555" % root,
        }]}
        resources = [
            PlanResource("test_root", "path", "frozen", "instance_root", str(root), consumers=["change-1.instance_root"]),
            PlanResource("test_worker_gun", "path", "frozen", "worker_gun", str(worker_gun), consumers=["change-1.path"]),
        ]

        ChangePlannerAgent._normalize_existing_config_paths(data, resources)

        assert resources[1].consumers == ["change-1.worker_gun"]
        assert any(
            resource.value == str(config)
            and resource.role == "config_path"
            and resource.consumers == ["change-1.path"]
            for resource in resources
        )


def test_deployment_planner_repairs_missing_resources_and_bad_checker_contract():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    invalid = {
        "status": "ready",
        "goal": "deploy isolated instance to /srv/appe2e",
        "resources": [],
        "changes": [
            {
                "step_id": "deploy",
                "title": "deploy",
                "objective": "clone into /srv/appe2e",
                "reason": "deploy",
                "evidence_refs": [evidence_id],
                "depends_on": [],
                "risk": "high",
                "expected_changes": ["/srv/appe2e is created"],
                "postconditions": [
                    {
                        "checker": "file_contains",
                        "args": {"path": "/srv/appe2e/config.py", "content": "x"},
                    }
                ],
            }
        ],
    }
    valid = {
        **invalid,
        "resources": [
            {
                "name": "instance_root",
                "kind": "path",
                "status": "frozen",
                "role": "instance_root",
                "value": "/srv/appe2e",
                "source": "user_input",
                "consumers": ["deploy.repository"],
            },
            {
                "name": "source_remote",
                "kind": "identifier",
                "status": "frozen",
                "role": "source_remote",
                "value": "gitee:example/platform.git",
                "source": "evidence",
                "consumers": ["deploy.url"],
            },
            {
                "name": "source_branch",
                "kind": "identifier",
                "status": "frozen",
                "role": "source_branch",
                "value": "develop",
                "source": "evidence",
                "consumers": ["deploy.ref"],
            },
            {
                "name": "instance_identifier",
                "kind": "identifier",
                "status": "frozen",
                "role": "instance_identifier",
                "value": "v4e2e",
                "source": "user_input",
                "consumers": ["deploy.instance_name"],
            },
            {
                "name": "config_path",
                "kind": "path",
                "status": "frozen",
                "role": "instance_config_path",
                "value": "/srv/appe2e/config.py",
                "source": "derived_from_evidence",
                "consumers": ["deploy.path"],
            },
            {
                "name": "master_port",
                "kind": "port",
                "status": "frozen",
                "role": "master_port",
                "value": 47001,
                "source": "evidence",
                "consumers": ["deploy.port"],
            },
        ],
        "changes": [
            {
                **invalid["changes"][0],
                "postconditions": [
                    {
                        "checker": "file_contains",
                        "args": {"path": "/srv/appe2e/config.py", "text": "x"},
                    }
                ],
            }
        ],
    }
    llm = FakeLLM([json.dumps(invalid), json.dumps(valid)])

    outcome = ChangePlannerAgent(llm).plan(
        "deploy isolated instance to /srv/appe2e",
        bundle,
        conclusion,
    )

    assert outcome.status == "need_execution"
    assert {item.role for item in outcome.plan.resources} >= {
        "instance_root",
        "source_remote",
        "source_branch",
        "master_port",
    }
    repair = llm.calls[1]["messages"][-1]["content"]
    assert "frozen resources" in repair
    assert "missing_required_args=text" not in repair
    assert "Freeze every future configuration file path" in repair


def test_deployment_contract_preserves_fixed_names_from_original_goal():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, _, _ = _bundle_and_conclusion()
    resources = [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root", "/srv/appe2e",
            "user_input", consumers=["deploy.repository"],
        ),
        PlanResource(
            "source_remote", "identifier", "frozen", "source_remote",
            "gitee:example/platform.git", "evidence", consumers=["deploy.url"],
        ),
        PlanResource(
            "source_branch", "identifier", "frozen", "source_branch", "develop",
            "evidence", consumers=["deploy.ref"],
        ),
        PlanResource(
            "instance_identifier", "identifier", "frozen", "instance_identifier",
            "wrong-name", "user_input", consumers=["deploy.instance_name"],
        ),
        PlanResource(
            "master_port", "port", "frozen", "master_port", 47001,
            "evidence", consumers=["deploy.port"],
        ),
        PlanResource(
            "worker_port", "port", "frozen", "worker_port", 47002,
            "evidence", consumers=["deploy.worker_port"],
        ),
    ]
    data = {
        "status": "ready",
        "goal": "deploy instance wrong-name from /home/lzl/vemu_uestc",
        "resources": [item.to_dict() for item in resources],
        "changes": [
            {
                "step_id": "deploy",
                "risk": "high",
                "expected_changes": ["created"],
                "postconditions": [
                    {"checker": "file_exists", "args": {"path": "/srv/appe2e"}}
                ],
            }
        ],
        "assumptions": [],
    }

    errors = ChangePlannerAgent._ready_contract_errors(
        data,
        (
            "deploy isolated instance to /srv/appe2e; "
            "实例名固定为 v4e2e，Nginx 配置名固定为 klonet-v4-e2e"
        ),
        resources,
        bundle,
    )

    assert "fixed instance identifiers are not frozen=v4e2e" in errors
    assert "fixed Nginx config names are not frozen=klonet-v4-e2e" in errors
    assert not any("/home/lzl/vemu_uestc" in error for error in errors)
    assert not any("consumes multiple port resources" in error for error in errors)


def test_deployment_contract_rejects_unfrozen_ports_but_allows_cohesive_semantic_steps():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, _, _ = _bundle_and_conclusion()
    resources = [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root", "/srv/appe2e",
            "user_input", consumers=["clone.repository"],
        ),
        PlanResource(
            "source_remote", "identifier", "frozen", "source_remote",
            "gitee:example/platform.git", "evidence", consumers=["clone.url"],
        ),
        PlanResource(
            "source_branch", "identifier", "frozen", "source_branch", "develop",
            "evidence", consumers=["clone.ref"],
        ),
        PlanResource(
            "instance_identifier", "identifier", "frozen", "instance_identifier",
            "v4e2e", "user_input", consumers=["clone.instance_name"],
        ),
        PlanResource(
            "master_port", "port", "frozen", "master_port", 47001,
            "evidence", consumers=["configure.master_port"],
        ),
        PlanResource(
            "config_path", "path", "frozen", "config_file_path",
            "/srv/appe2e/config.py", "derived", consumers=["configure.path"],
        ),
    ]
    data = {
        "status": "ready",
        "goal": "deploy v4e2e",
        "resources": [item.to_dict() for item in resources],
        "changes": [
            {
                "step_id": "clone", "risk": "medium",
                "expected_changes": ["clone repository"],
                "postconditions": [
                    {"checker": "file_exists", "args": {"path": "/srv/appe2e/.git"}}
                ],
            },
            {
                "step_id": "configure", "risk": "medium",
                "expected_changes": [
                    "Set master_port to 47001", "Set worker_port to 47002"
                ],
                "postconditions": [
                    {"checker": "file_contains", "args": {"path": "/srv/appe2e/config.py", "text": "master_port = 47001"}},
                    {"checker": "file_contains", "args": {"path": "/srv/appe2e/config.py", "text": "worker_port = 47002"}},
                ],
            },
            {
                "step_id": "start", "risk": "high",
                "expected_changes": ["Create sessions v4e2e_m and v4e2e_w"],
                "postconditions": [
                    {"checker": "screen_session_exists", "args": {"session": "v4e2e_m"}},
                    {"checker": "screen_session_exists", "args": {"session": "v4e2e_w"}},
                ],
            },
        ],
        "assumptions": [],
    }

    errors = ChangePlannerAgent._ready_contract_errors(
        data,
        "deploy isolated instance to /srv/appe2e; instance name fixed as v4e2e",
        resources,
        bundle,
    )

    assert "change configure uses unfrozen port=47002" in errors
    assert not any("configuration assertions" in error for error in errors)
    assert not any("multiple screen sessions" in error for error in errors)


def test_change_planner_rejects_resource_consumer_with_multiple_owners():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, _, _ = _bundle_and_conclusion()
    resources = [
        PlanResource(
            "instance_identifier",
            "identifier",
            "frozen",
            "instance_identifier",
            "v4e2e",
            "user_input",
            consumers=["change-3.config_name"],
        ),
        PlanResource(
            "nginx_config_name",
            "identifier",
            "frozen",
            "nginx_config_name",
            "klonet-v4-e2e",
            "user_input",
            consumers=["change-3.config_name"],
        ),
    ]

    errors = ChangePlannerAgent._ready_contract_errors(
        {"changes": [], "assumptions": []},
        "configure deployment",
        resources,
        bundle,
    )

    assert (
        "plan resource consumer has multiple owners="
        "change-3.config_name:instance_identifier,nginx_config_name"
    ) in errors


def test_change_planner_normalizes_ambiguous_consumer_owners_by_resource_role():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    resources = [
        PlanResource(
            "instance_root",
            "path",
            "frozen",
            "instance_root",
            "/srv/appe2e",
            "user_input",
            consumers=["change-2.config", "change-3.config_name"],
        ),
        PlanResource(
            "instance_identifier",
            "identifier",
            "frozen",
            "instance_identifier",
            "v4e2e",
            "user_input",
            consumers=["change-2.config"],
        ),
        PlanResource(
            "nginx_config_name",
            "identifier",
            "frozen",
            "nginx_config_name",
            "klonet-v4-e2e",
            "user_input",
            consumers=["change-3.config_name"],
        ),
    ]

    normalized = ChangePlannerAgent._normalize_derived_resources(
        {"changes": []}, resources
    )
    consumers = {
        resource.name: set(resource.consumers) for resource in normalized
    }

    assert consumers["instance_root"] == {
        "change-2.instance_root",
        "change-3.instance_root",
    }
    assert consumers["instance_identifier"] == {
        "change-2.instance_identifier"
    }
    assert consumers["nginx_config_name"] == {"change-3.config_name"}
    all_consumers = [
        consumer for resource in normalized for consumer in resource.consumers
    ]
    assert len(all_consumers) == len(set(all_consumers))


def test_change_planner_normalizes_unambiguous_checker_argument_aliases():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "postconditions": [
                    {
                        "checker": "git_revision",
                        "args": {"path": "/srv/appe2e", "revision": "abc123"},
                    },
                    {
                        "checker": "file_contains",
                        "args": {"path": "/srv/appe2e/config.py", "content": "47001"},
                    },
                    {
                        "checker": "screen_session_exists",
                        "args": {"name": "v4e2e_web"},
                    },
                    {
                        "checker": "process_running",
                        "args": {"name": "v4e2e-redis"},
                    },
                ]
            }
        ]
    }

    ChangePlannerAgent._normalize_postcondition_args(data)

    checks = data["changes"][0]["postconditions"]
    assert checks[0]["args"] == {
        "repository": "/srv/appe2e",
        "revision": "abc123",
    }
    assert checks[1]["args"] == {
        "path": "/srv/appe2e/config.py",
        "text": "47001",
    }
    assert checks[2]["args"] == {"session": "v4e2e_web"}
    assert checks[3]["args"] == {"pattern": "v4e2e-redis"}


def test_change_planner_derives_frozen_source_revision_for_clone_binding():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    resources = [
        PlanResource(
            "instance_root",
            "path",
            "frozen",
            "instance_root",
            "/srv/appe2e",
            "user_input",
            consumers=["clone.repository"],
        )
    ]
    data = {
        "changes": [
            {
                "step_id": "clone",
                "title": "Clone source and pin revision",
                "objective": "Create an exact isolated source checkout",
                "expected_changes": ["repository is cloned"],
                "postconditions": [
                    {
                        "checker": "git_revision",
                        "args": {
                            "repository": "/srv/appe2e",
                            "revision": "a" * 40,
                        },
                    }
                ],
            }
        ]
    }

    normalized = ChangePlannerAgent._normalize_derived_resources(data, resources)

    revision = next(item for item in normalized if item.role == "source_revision")
    assert revision.value == "a" * 40
    assert revision.consumers == ["clone.revision"]


def test_change_planner_derives_fixed_nginx_name_from_site_path():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    normalized = ChangePlannerAgent._normalize_derived_resources(
        {
            "changes": [
                {
                    "step_id": "nginx",
                    "title": "Create Nginx site",
                    "objective": "Install /etc/nginx/sites-available/klonet-v4-e2e",
                    "expected_changes": [
                        "Enable /etc/nginx/sites-enabled/klonet-v4-e2e"
                    ],
                    "postconditions": [
                        {
                            "checker": "file_exists",
                            "args": {
                                "path": "/etc/nginx/sites-available/klonet-v4-e2e"
                            },
                        }
                    ],
                }
            ]
        },
        [],
    )

    resource = next(item for item in normalized if item.role == "nginx_config_name")
    assert resource.value == "klonet-v4-e2e"
    assert resource.consumers == ["nginx.config_name"]


def test_complete_klonet_deployment_contract_requires_all_runtime_components_and_ports():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    resources = [
        PlanResource(
            "instance_identifier", "identifier", "frozen", "instance_identifier",
            "v4e2e", "user_input", consumers=["start.instance_name"],
        ),
        PlanResource(
            "web_port", "port", "frozen", "service_port", 47001,
            "evidence", consumers=["start.web_port"],
        ),
        PlanResource(
            "master_port", "port", "frozen", "master_port", 47002,
            "evidence", consumers=["start.master_port"],
        ),
        PlanResource(
            "worker_port", "port", "frozen", "worker_port", 47003,
            "evidence", consumers=["start.worker_port"],
        ),
    ]
    data = {
        "goal": "deploy a complete isolated Klonet platform instance",
        "assumptions": [],
        "changes": [
            {
                "step_id": "start",
                "title": "Start application services",
                "objective": "Start web, master and worker Screen sessions",
                "depends_on": [],
                "risk": "high",
                "expected_changes": [
                    "Create v4e2e_web", "Create v4e2e_master", "Create v4e2e_worker"
                ],
                "postconditions": [
                    {"checker": "screen_session_exists", "args": {"session": "v4e2e_web"}},
                    {"checker": "screen_session_exists", "args": {"session": "v4e2e_master"}},
                    {"checker": "screen_session_exists", "args": {"session": "v4e2e_worker"}},
                ],
            }
        ],
    }

    errors = ChangePlannerAgent._complete_klonet_contract_errors(
        data, resources
    )

    assert "complete Klonet runtime missing components=celery" in errors
    assert "complete Klonet runtime missing port resources=web_terminal_port" in errors
    assert "complete Klonet runtime missing Screen sessions=v4e2e_c,v4e2e_m,v4e2e_w" in errors


def test_complete_klonet_deployment_contract_requires_config_fields_and_master_nginx_upstream():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    resources = [
        PlanResource(
            "instance_identifier", "identifier", "frozen", "instance_identifier",
            "v4e2e", "user_input", consumers=["start.instance_name"],
        ),
        PlanResource(
            "master_port", "port", "frozen", "master_port", 47001,
            "evidence", consumers=["config.master_port"],
        ),
        PlanResource(
            "worker_port", "port", "frozen", "worker_port", 47002,
            "evidence", consumers=["config.worker_port"],
        ),
        PlanResource(
            "web_terminal_port", "port", "frozen", "web_terminal_port", 47003,
            "evidence", consumers=["config.web_terminal_port"],
        ),
    ]
    data = {
        "goal": "deploy a complete isolated Klonet platform instance",
        "changes": [
            {
                "step_id": "config",
                "title": "Configure isolated services",
                "objective": "Set application and MySQL, Redis, RabbitMQ ports",
                "expected_changes": ["configure all ports"],
                "postconditions": [],
            },
            {
                "step_id": "start",
                "title": "Start master, celery, web terminal and worker",
                "objective": "Start complete application runtime",
                "expected_changes": ["v4e2e_m v4e2e_c v4e2e_web v4e2e_w"],
                "postconditions": [
                    {"checker": "screen_session_exists", "args": {"session": name}}
                    for name in ("v4e2e_m", "v4e2e_c", "v4e2e_web", "v4e2e_w")
                ],
            },
            {
                "step_id": "nginx",
                "title": "Install Nginx site",
                "objective": "Proxy the Nginx entry point to web terminal 47003",
                "expected_changes": ["Nginx proxies to 47003"],
                "postconditions": [],
            },
        ],
    }

    errors = ChangePlannerAgent._complete_klonet_contract_errors(data, resources)

    assert (
        "complete Klonet configuration missing attributes="
        "master_port,worker_port,web_terminal_port,mysql_port,redis_port,"
        "rabbitmq_port,master_ip,mysql_ip,rabbitmq_ip,celery_redis_port_db,"
        "celery_rabbitmq_port_db,proj_config"
    ) in errors
    assert "complete Klonet Nginx must proxy to frozen master_port=47001" in errors


def test_complete_klonet_deployment_contract_rejects_unsupported_data_server_component():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    resource = PlanResource(
        "data_server_port", "port", "frozen", "data_server_port", 47004,
        "planner_choice", consumers=["config.data_server_port"],
    )
    errors = ChangePlannerAgent._complete_klonet_contract_errors(
        {
            "goal": "deploy a complete isolated Klonet platform instance",
            "changes": [
                {
                    "step_id": "config",
                    "title": "Configure data server",
                    "objective": "Set data_server_port = 47004",
                    "expected_changes": ["data server is configured"],
                    "postconditions": [],
                }
            ],
        },
        [resource],
    )

    assert "complete Klonet runtime includes unsupported data_server component" in errors


def test_complete_klonet_deployment_contract_rejects_invented_database_migration():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    errors = ChangePlannerAgent._complete_klonet_contract_errors(
        {
            "goal": "deploy a complete isolated Klonet platform instance",
            "changes": [
                {
                    "step_id": "db-init",
                    "title": "Initialize database schema",
                    "objective": "Run migrations and seed the new database",
                    "expected_changes": ["Create initial tables"],
                    "postconditions": [],
                }
            ],
        },
        [],
    )

    assert (
        "complete Klonet runtime invents unsupported database initialization step"
        in errors
    )


def test_complete_klonet_contract_allows_create_all_during_application_start():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    errors = ChangePlannerAgent._complete_klonet_contract_errors(
        {
            "goal": "deploy a complete isolated Klonet platform instance",
            "changes": [{
                "step_id": "start",
                "title": "Start the four application Screen components",
                "objective": (
                    "Launch the application; app_factory create_all initializes "
                    "empty database tables during startup"
                ),
            }],
        },
        [],
    )

    assert not any("database initialization" in error for error in errors)


def test_complete_klonet_deployment_rejects_ungrounded_dependency_install_step():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    errors = ChangePlannerAgent._complete_klonet_contract_errors(
        {
            "goal": "deploy a complete isolated Klonet platform instance",
            "changes": [{
                "title": "Install Python dependencies for the new instance",
                "objective": "Run pip install for project requirements",
            }],
        },
        [],
    )

    assert "ungrounded dependency installation step" in " ".join(errors)


def test_change_planner_does_not_rederive_explicit_internal_port_as_host_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    resources = [
        PlanResource(
            "redis_container_internal_port",
            "port",
            "frozen",
            "container_internal_port",
            6379,
            "image_contract",
            consumers=["change-3.redis_internal_port"],
        )
    ]
    data = {
        "changes": [
            {
                "step_id": "change-3",
                "title": "Create isolated Redis container",
                "objective": "Publish host port 47006 to Redis internal port 6379",
                "expected_changes": ["Map 47006:6379 for the new container"],
                "postconditions": [
                    {"checker": "port_listening", "args": {"port": 47006}}
                ],
            }
        ]
    }

    normalized = ChangePlannerAgent._normalize_derived_resources(
        data, resources
    )

    port_resources = {item.value: item for item in normalized if item.kind == "port"}
    assert port_resources[6379].role == "container_internal_port"
    assert port_resources[47006].role == "selected_host_port"
    assert len([item for item in normalized if item.value == 6379]) == 1


def test_http_success_status_is_not_derived_as_a_host_port():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {"changes": [{
        "step_id": "recover",
        "title": "Recover backend",
        "objective": "Restore worker port health",
        "expected_changes": ["worker endpoint is healthy with HTTP status 200"],
        "postconditions": [],
    }]}

    normalized = ChangePlannerAgent._normalize_derived_resources(data, [])

    assert not any(item.kind == "port" and item.value == 200 for item in normalized)


def test_change_planner_freezes_standard_stateful_container_internal_ports():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "step_id": "stateful",
                "title": "Provision isolated MySQL, Redis, and RabbitMQ containers",
                "objective": "Create v4e2e-mysql, v4e2e-redis, and v4e2e-rabbitmq",
                "expected_changes": ["Start all three new containers"],
                "postconditions": [],
            }
        ]
    }

    normalized = ChangePlannerAgent._normalize_derived_resources(data, [])
    internal = {
        item.value: item
        for item in normalized
        if item.role == "container_internal_port"
    }

    assert set(internal) == {3306, 6379, 5672}
    assert internal[3306].consumers == ["stateful.mysql_internal_port"]
    assert internal[6379].consumers == ["stateful.redis_internal_port"]
    assert internal[5672].consumers == ["stateful.rabbitmq_internal_port"]


def test_change_planner_freezes_multiple_future_paths_with_unique_consumers():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    root = PlanResource(
        "instance_root",
        "path",
        "frozen",
        "instance_root",
        "/srv/appe2e",
        "user_input",
        consumers=["change-1.repository"],
    )
    data = {
        "changes": [
            {
                "step_id": "change-2",
                "postconditions": [
                    {"checker": "file_exists", "args": {"path": "/srv/appe2e/a.py"}},
                    {"checker": "file_exists", "args": {"path": "/srv/appe2e/b.py"}},
                ],
            }
        ]
    }

    normalized = ChangePlannerAgent._normalize_derived_resources(data, [root])
    derived = [item for item in normalized if item.value in {"/srv/appe2e/a.py", "/srv/appe2e/b.py"}]

    assert len(derived) == 2
    assert {item.consumers[0] for item in derived} == {
        "change-2.path_1",
        "change-2.path_2",
    }


def test_isolation_contract_distinguishes_negated_and_positive_reuse_claims():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    negative = ChangePlannerAgent._ready_contract_errors(
        {"changes": [], "assumptions": ["Never reuse or share existing containers."]},
        "deploy an isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )
    positive = ChangePlannerAgent._ready_contract_errors(
        {"changes": [], "assumptions": ["Use the existing shared Redis container."]},
        "deploy an isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    comparative = ChangePlannerAgent._ready_contract_errors(
        {
            "changes": [],
            "assumptions": [
                "Create a dedicated Redis container rather than reusing the shared existing container."
            ],
        },
        "deploy an isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert "isolated deployment cannot reuse existing resources" not in negative
    assert "isolated deployment cannot reuse existing resources" not in comparative
    assert "isolated deployment cannot reuse existing resources" in positive


def test_isolation_contract_does_not_classify_celery_consumer_as_stateful_provisioning():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    errors = ChangePlannerAgent._ready_contract_errors(
        {
            "changes": [
                {
                    "step_id": "celery",
                    "title": "Start celery Screen session",
                    "objective": "Connect celery to the isolated Redis and RabbitMQ endpoints",
                    "depends_on": [],
                    "expected_changes": ["celery starts"],
                    "postconditions": [
                        {"checker": "screen_session_exists", "args": {"name": "v4e2e_c"}}
                    ],
                }
            ],
            "assumptions": [],
        },
        "deploy an isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert not any(
        error == "isolated stateful service must use a new named container=celery"
        for error in errors
    )


def test_isolated_application_start_must_depend_on_stateful_provisioning():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    changes = [
        {
            "step_id": "start-app",
            "title": "Start application components",
            "objective": "Launch master, worker, and web terminal in Screen sessions",
            "depends_on": ["configure"],
            "expected_changes": ["Create application Screen sessions"],
            "postconditions": [
                {"checker": "screen_session_exists", "args": {"session": "v4e2e_m"}}
            ],
        },
        {
            "step_id": "provision-state",
            "title": "Provision isolated stateful services",
            "objective": "Create new MySQL, Redis, and RabbitMQ containers",
            "depends_on": ["clone"],
            "expected_changes": ["Create instance-named containers"],
            "postconditions": [
                {"checker": "port_listening", "args": {"port": 47004}}
            ],
        },
    ]

    errors = ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance without interfering with existing services",
        [],
        _bundle_and_conclusion()[0],
    )

    assert (
        "application start must depend on earlier stateful provisioning="
        "start-app:provision-state"
    ) in errors


def test_isolated_stateful_services_must_use_new_named_containers():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    changes = [
        {
            "step_id": "redis",
            "title": "Provision dedicated Redis process",
            "objective": "Start a new Redis server process on port 47005",
            "depends_on": [],
            "expected_changes": ["Start redis-server with a separate data directory"],
            "postconditions": [
                {"checker": "port_listening", "args": {"port": 47005}}
            ],
        }
    ]

    errors = ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance without reusing existing services",
        [],
        _bundle_and_conclusion()[0],
    )

    assert "isolated stateful service must use a new named container=redis" in errors


def test_celery_start_is_not_misclassified_as_stateful_service_provisioning():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    changes = [
        {
            "step_id": "celery",
            "title": "Start celery Screen session",
            "objective": "Launch the celery worker for background tasks",
            "depends_on": [],
            "expected_changes": [
                "Start celery and connect it to the isolated Redis and RabbitMQ"
            ],
            "postconditions": [
                {"checker": "screen_session_exists", "args": {"session": "v4e2e_c"}}
            ],
        }
    ]

    errors = ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert not any("stateful service must use" in error for error in errors)


def test_runtime_start_after_stateful_dependencies_is_not_stateful_provisioning():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    changes = [{
        "step_id": "runtime",
        "title": "Start v4e2e complete runtime under four Screen sessions",
        "objective": (
            "Launch master, celery, web terminal, and worker after stateful "
            "dependencies are up"
        ),
        "depends_on": [],
        "expected_changes": ["four Screen sessions start"],
    }]

    errors = ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert not any("stateful service must use" in error for error in errors)


def test_isolated_nginx_must_depend_on_started_application():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    changes = [
        {
            "step_id": "nginx",
            "title": "Activate Nginx configuration",
            "objective": "Reload Nginx to serve port 47008 and proxy to the application",
            "depends_on": ["configure"],
            "expected_changes": ["Create isolated Nginx site"],
            "postconditions": [{"checker": "nginx_config_valid", "args": {}}],
        },
        {
            "step_id": "start-app",
            "title": "Start application components",
            "objective": "Launch master, worker, web terminal, and celery in Screen",
            "depends_on": ["configure"],
            "expected_changes": ["Create application Screen sessions"],
            "postconditions": [
                {"checker": "screen_session_exists", "args": {"session": "v4e2e_m"}}
            ],
        },
    ]

    errors = ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert "Nginx activation must depend on earlier application start=nginx:start-app" in errors


def test_change_planner_topologically_orders_forward_semantic_dependencies():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {"step_id": "start", "depends_on": ["state"]},
            {"step_id": "state", "depends_on": ["clone"]},
            {"step_id": "clone", "depends_on": []},
        ]
    }

    ChangePlannerAgent._normalize_change_order(data)

    assert [item["step_id"] for item in data["changes"]] == [
        "clone", "state", "start"
    ]


def test_change_planner_rejects_verification_only_semantic_change():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    changes = [
        {
            "step_id": "verify",
            "title": "Verify the new instance is fully operational",
            "objective": "Check that MySQL, Redis, and RabbitMQ containers are running",
            "depends_on": ["start"],
            "expected_changes": ["Check all service status"],
            "postconditions": [
                {"checker": "process_running", "args": {"pattern": "v4e2e"}}
            ],
        }
    ]

    errors = ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert "verification-only change is not allowed=verify" in errors
    assert not any(
        error.endswith(":verify")
        for error in errors
        if error.startswith("application start must depend")
    )


def test_change_planner_moves_leaf_verification_into_predecessor_postconditions():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "resources": [
            {
                "name": "listen_port",
                "kind": "port",
                "status": "frozen",
                "role": "nginx_listen_port",
                "value": 47008,
                "source": "planner_choice",
                "consumers": ["verify.url"],
            }
        ],
        "changes": [
            {
                "step_id": "nginx",
                "title": "Create Nginx site",
                "depends_on": [],
                "postconditions": [{"checker": "nginx_config_valid", "args": {}}],
            },
            {
                "step_id": "verify",
                "title": "Verify the HTTP endpoint",
                "depends_on": ["nginx"],
                "postconditions": [
                    {
                        "checker": "http_status",
                        "args": {"url": "http://127.0.0.1:47008", "expected_status": 200},
                    }
                ],
            },
        ],
    }

    ChangePlannerAgent._normalize_verification_changes(data)

    assert [item["step_id"] for item in data["changes"]] == ["nginx"]
    assert data["changes"][0]["postconditions"][-1]["checker"] == "http_status"
    assert data["resources"][0]["consumers"] == ["nginx.url"]


def test_isolated_nginx_requires_explicit_frozen_dedicated_listen_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    change = {
        "step_id": "nginx",
        "title": "Create Nginx configuration",
        "objective": "Add an isolated Nginx site for v4e2e",
        "depends_on": ["start-app"],
        "expected_changes": ["Create and enable klonet-v4-e2e"],
        "postconditions": [
            {"checker": "http_status", "args": {"url": "http://127.0.0.1/", "expected_status": 200}}
        ],
    }
    port = PlanResource(
        "nginx_listen_port", "port", "frozen", "nginx_listen_port", 47008,
        "planner_choice", consumers=["nginx.listen_port"],
    )

    missing = ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": [change], "assumptions": []},
        "deploy a new isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )
    explicit = ChangePlannerAgent._ready_contract_errors(
        {
            "goal": "deploy v4e2e",
            "changes": [{
                **change,
                "postconditions": [{
                    "checker": "http_status",
                    "args": {"url": "http://127.0.0.1:47008/", "expected_status": 200},
                }],
            }],
            "assumptions": [],
        },
        "deploy a new isolated instance",
        [port],
        _bundle_and_conclusion()[0],
    )
    shared = ChangePlannerAgent._ready_contract_errors(
        {
            "goal": "deploy v4e2e",
            "changes": [
                {
                    "step_id": "start-app",
                    "title": "Start application components",
                    "objective": "Start the application public service on port 47008",
                    "depends_on": [],
                    "expected_changes": ["Listen on port 47008"],
                    "postconditions": [
                        {"checker": "port_listening", "args": {"port": 47008}}
                    ],
                },
                {
                    **change,
                    "postconditions": [{
                        "checker": "http_status",
                        "args": {"url": "http://127.0.0.1:47008/", "expected_status": 200},
                    }],
                },
            ],
            "assumptions": [],
        },
        "deploy a new isolated instance",
        [port],
        _bundle_and_conclusion()[0],
    )

    expected = "isolated Nginx requires an explicit frozen dedicated listen port=nginx"
    assert expected in missing
    assert expected not in explicit
    assert expected in shared


def test_isolated_nginx_allows_prepare_before_app_and_activation_after_app():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    port = PlanResource(
        "nginx_listen_port", "port", "frozen", "nginx_listen_port", 47008,
        "planner_choice", consumers=["nginx-config.listen_port"],
    )
    changes = [
        {
            "step_id": "nginx-config",
            "title": "Create Nginx site configuration",
            "objective": "Write the isolated site and symlink",
            "depends_on": [],
            "risk": "medium",
            "expected_changes": ["Create Nginx config listening on 47008"],
            "postconditions": [{"checker": "nginx_config_valid", "args": {}}],
        },
        {
            "step_id": "start-app",
            "title": "Start application Screen components",
            "objective": "Start master worker web terminal",
            "depends_on": [],
            "risk": "high",
            "expected_changes": ["Start application"],
            "postconditions": [
                {"checker": "screen_session_exists", "args": {"session": "demo_m"}}
            ],
        },
        {
            "step_id": "nginx-activate",
            "title": "Activate Nginx site",
            "objective": "Reload Nginx after the application is running",
            "depends_on": ["nginx-config", "start-app"],
            "risk": "medium",
            "expected_changes": ["Reload Nginx"],
            "postconditions": [
                {
                    "checker": "http_status",
                    "args": {"url": "http://127.0.0.1:47008", "status": 200},
                }
            ],
        },
    ]

    errors = ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy isolated instance", "changes": changes, "assumptions": []},
        "deploy isolated instance",
        [port],
        _bundle_and_conclusion()[0],
    )

    assert not any("Nginx activation must depend" in error for error in errors)
    assert not any("dedicated listen port" in error for error in errors)


def test_change_planner_adds_http_check_for_frozen_nginx_listen_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "step_id": "nginx",
                "title": "Create isolated Nginx site",
                "objective": "Listen on dedicated port 47008 and proxy to 47001",
                "postconditions": [
                    {"checker": "nginx_config_valid", "args": {}}
                ],
            }
        ]
    }
    resources = [
        PlanResource(
            "nginx_listen_port", "port", "frozen", "nginx_listen_port",
            47008, "planner_choice", consumers=["nginx.listen_port"],
        )
    ]

    ChangePlannerAgent._normalize_nginx_postconditions(data, resources)

    assert data["changes"][0]["postconditions"][-1] == {
        "checker": "http_status",
        "args": {"url": "http://127.0.0.1:47008/healthz", "expected_status": 200},
    }


def test_nginx_health_check_moves_from_prepare_to_activation():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "step_id": "prepare",
                "title": "Create Nginx site",
                "objective": "Write config for port 47008",
                "postconditions": [
                    {"checker": "port_listening", "args": {"port": 47008}},
                    {"checker": "http_status", "args": {
                        "url": "http://127.0.0.1:47008/", "status": 200}},
                ],
            },
            {
                "step_id": "activate",
                "title": "Reload Nginx after application start",
                "objective": "Activate the prepared site",
                "postconditions": [{"checker": "nginx_config_valid", "args": {}}],
            },
        ]
    }
    resources = [
        PlanResource(
            "nginx_port", "port", "frozen", "nginx_listen_port", 47008,
            "evidence", consumers=["prepare.listen_port"],
        )
    ]

    ChangePlannerAgent._normalize_nginx_postconditions(data, resources)

    assert all(
        check["checker"] not in {"http_status", "port_listening"}
        for check in data["changes"][0]["postconditions"]
    )
    assert data["changes"][1]["postconditions"][-1] == {
        "checker": "http_status",
        "args": {"url": "http://127.0.0.1:47008/healthz", "expected_status": 200},
    }


def test_http_observation_does_not_claim_a_listening_port():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "step_id": "verify",
                "title": "Observe endpoint",
                "objective": "Request the endpoint after startup",
                "postconditions": [
                    {
                        "checker": "http_status",
                        "args": {"url": "http://127.0.0.1:47008", "expected_status": 200},
                    }
                ],
            },
            {
                "step_id": "server",
                "title": "Start server",
                "objective": "Listen on port 47009",
                "expected_changes": ["Server listens on 47009"],
                "postconditions": [
                    {"checker": "port_listening", "args": {"port": 47009}}
                ],
            },
        ]
    }

    assert ChangePlannerAgent._declared_listening_ports_by_step(data) == {
        "verify": set(),
        "server": {47009},
    }


def test_deployment_planner_turns_unproven_frozen_port_into_evidence_request():
    from klonet_agent.ops.privileged.workflow.contracts import EvidenceRecord, ProbeRequest
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    payload = {
        "status": "ready",
        "goal": "model-rewritten deployment goal",
        "resources": [
            {
                "name": "instance_root", "kind": "path", "status": "frozen",
                "role": "instance_root", "value": "/srv/appe2e",
                "source": "user_input", "consumers": ["deploy.repository"],
            },
            {
                "name": "source_remote", "kind": "identifier", "status": "frozen",
                "role": "source_remote", "value": "gitee:example/platform.git",
                "source": "evidence", "consumers": ["deploy.url"],
            },
            {
                "name": "source_branch", "kind": "identifier", "status": "frozen",
                "role": "source_branch", "value": "develop",
                "source": "evidence", "consumers": ["deploy.ref"],
            },
            {
                "name": "instance_identifier", "kind": "identifier",
                "status": "frozen", "role": "instance_identifier", "value": "v4e2e",
                "source": "user_input", "consumers": ["deploy.instance_name"],
            },
            {
                "name": "service_port", "kind": "port", "status": "frozen",
                "role": "service_port", "value": 47002,
                "source": "evidence", "consumers": ["deploy.port"],
            },
            {
                "name": "redis_container_port", "kind": "port", "status": "frozen",
                "role": "container_internal_port", "value": 6379,
                "source": "image_contract", "consumers": ["deploy.container_port"],
            },
        ],
        "changes": [
            {
                "step_id": "deploy", "title": "deploy", "objective": "deploy",
                "reason": "deploy", "evidence_refs": [evidence_id], "depends_on": [],
                "risk": "high", "expected_changes": ["created"],
                "postconditions": [
                    {"checker": "file_exists", "args": {"path": "/srv/appe2e"}}
                ],
            }
        ],
    }
    llm = FakeLLM([json.dumps(payload)])

    outcome = ChangePlannerAgent(llm).plan(
        "deploy v4e2e to /srv/appe2e", bundle, conclusion
    )

    assert outcome.status == "need_evidence"
    assert outcome.evidence_requests == [
        ProbeRequest("ports", {"ports": [47002]}, "verify frozen port availability")
    ]
    assert outcome.candidate_plan is not None
    assert outcome.candidate_plan.goal == "deploy v4e2e to /srv/appe2e"
    assert next(
        item for item in outcome.candidate_plan.resources if item.kind == "port"
    ).value == 47002
    assert len(llm.calls) == 1

    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("ports", {"ports": [47002]}, "verify frozen port availability"),
            "inspect_ports\nno matching listeners",
        )
    )
    finalized = ChangePlannerAgent.finalize_candidate(
        outcome.candidate_plan,
        bundle,
    )

    assert finalized.status == "need_execution"
    assert finalized.plan is outcome.candidate_plan


def test_planner_normalizes_derived_config_path_and_hidden_selected_port():
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    payload = {
        "status": "ready",
        "goal": "deploy v4e2e",
        "resources": [
            {
                "name": "instance_root", "kind": "path", "status": "frozen",
                "role": "instance_root", "value": "/srv/appe2e",
                "source": "user_input", "consumers": ["clone.repository"],
            },
            {
                "name": "source_remote", "kind": "identifier", "status": "frozen",
                "role": "source_remote", "value": "gitee:example/platform.git",
                "source": "evidence", "consumers": ["clone.url"],
            },
            {
                "name": "source_branch", "kind": "identifier", "status": "frozen",
                "role": "source_branch", "value": "develop",
                "source": "evidence", "consumers": ["clone.ref"],
            },
            {
                "name": "instance_identifier", "kind": "identifier", "status": "frozen",
                "role": "instance_identifier", "value": "v4e2e",
                "source": "user_input", "consumers": ["clone.instance_name"],
            },
        ],
        "changes": [
            {
                "step_id": "clone", "title": "clone", "objective": "clone",
                "reason": "clone", "evidence_refs": [evidence_id], "depends_on": [],
                "risk": "medium", "expected_changes": ["clone repository"],
                "postconditions": [
                    {"checker": "file_exists", "args": {"path": "/srv/appe2e/.git"}}
                ],
            },
            {
                "step_id": "configure", "title": "configure ports",
                "objective": "configure master port", "reason": "isolate",
                "evidence_refs": [evidence_id], "depends_on": ["clone"],
                "risk": "medium", "expected_changes": ["Set master_port to 47009"],
                "postconditions": [
                    {"checker": "file_contains", "args": {"path": "/srv/appe2e/config.py", "text": "master_port = 47009"}}
                ],
            },
        ],
        "assumptions": [],
    }

    outcome = ChangePlannerAgent(FakeLLM([json.dumps(payload)])).plan(
        "deploy isolated v4e2e to /srv/appe2e", bundle, conclusion
    )

    assert outcome.status == "need_evidence"
    assert outcome.evidence_requests[0].args == {"ports": [47009]}
    resources = outcome.candidate_plan.resources
    assert any(item.kind == "port" and item.value == 47009 for item in resources)
    assert any(item.kind == "path" and item.value == "/srv/appe2e/config.py" for item in resources)


def test_derived_runtime_roots_are_scoped_per_same_named_instance():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent

    data = {
        "changes": [
            {
                "step_id": "change-1",
                "title": "Start test worker screen",
                "objective": "Start worker for /home/lzl/test/vemu_uestc",
                "expected_changes": ["worker starts"],
                "postconditions": [],
            },
            {
                "step_id": "change-2",
                "title": "Restart production master screen",
                "objective": "Restart master for /home/lzl/vemu_uestc",
                "expected_changes": ["master restarts"],
                "postconditions": [],
            },
        ]
    }
    resources = [
        PlanResource(
            "test_instance_root", "path", "frozen", "instance_root",
            "/home/lzl/test/vemu_uestc", "user_input",
            consumers=["change-1.instance_root"],
        ),
        PlanResource(
            "prod_instance_root", "path", "frozen", "instance_root",
            "/home/lzl/vemu_uestc", "user_input",
            consumers=["change-2.instance_root"],
        ),
    ]

    normalized = ChangePlannerAgent._normalize_derived_resources(data, resources)
    mains = {
        str(item.value): set(item.consumers)
        for item in normalized
        if item.role == "runtime_mains_root"
    }

    assert mains == {
        "/home/lzl/test/vemu_uestc/mains": {"change-1.source_root"},
        "/home/lzl/vemu_uestc/mains": {"change-2.source_root"},
    }
