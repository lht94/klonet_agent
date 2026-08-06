from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


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


def _bundle_and_conclusion():
    from klonet_agent.ops.privileged.v4.contracts import (
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


def test_change_plan_authorization_hash_covers_resources_steps_and_bindings():
    from klonet_agent.ops.privileged.contracts import ExecutionBinding, PlanResource
    from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4, ChangeStepV4

    step = ChangeStepV4(
        step_id="clone",
        title="clone source",
        objective="clone source into isolated root",
        risk="high",
        expected_changes=["/srv/v4e2e is created"],
        postconditions=[{"checker": "git_repository", "args": {"repository": "/srv/v4e2e"}}],
    )
    plan = ChangePlanV4(
        plan_id="priv-v4-test",
        goal="deploy",
        risk="high",
        steps=[step],
        resources=[
            PlanResource(
                name="instance_root",
                kind="path",
                status="frozen",
                role="instance_root",
                value="/srv/v4e2e",
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
        args={"operation": "clone", "repository": "/srv/v4e2e", "url": "gitee:x/y.git"},
        postconditions=step.postconditions,
    )
    assert plan.is_authorized is False


def test_container_plan_requires_docker_image_discovery_before_binding():
    from klonet_agent.ops.privileged.v4.contracts import (
        ChangePlanV4,
        ChangeStepV4,
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    plan = ChangePlanV4(
        plan_id="priv-v4-images",
        goal="deploy isolated redis",
        risk="high",
        steps=[
            ChangeStepV4(
                step_id="redis",
                title="Provision isolated Redis container v4e2e-redis",
                objective="Create the new Redis container",
                risk="high",
                expected_changes=["container is running"],
                postconditions=[
                    {"checker": "container_running", "args": {"container": "v4e2e-redis"}}
                ],
            )
        ],
    )
    bundle = EvidenceBundle(goal=plan.goal)

    missing = V4ChangePlannerAgent.finalize_candidate(plan, bundle)

    assert missing.status == "need_evidence"
    assert missing.probe_requests[0].probe == "docker_images"
    bundle.add(
        EvidenceRecord.from_probe(
            ProbeRequest("docker_images", {}, "select an installed image"),
            "inspect_docker_images\nredis latest sha256:a sha256:b now 1MB",
        )
    )
    assert V4ChangePlannerAgent.finalize_candidate(plan, bundle).status == "ready"


def test_v4_store_uses_separate_directory_and_recovers_without_reexecution(tmp_path):
    from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4, ChangeStepV4
    from klonet_agent.ops.privileged.v4.store import V4PlanStore

    plan = ChangePlanV4(
        plan_id="priv-v4-recover",
        goal="deploy",
        risk="medium",
        steps=[
            ChangeStepV4(
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
    store = V4PlanStore(tmp_path, user_id="u", project_id="p")

    store.save(plan)
    recovered = store.recover(plan.plan_id)

    assert "privileged_ops_plans_v4" in str(store.plan_dir)
    assert recovered.status == "paused"
    assert recovered.steps[0].status == "execution_unknown"
    assert recovered.steps[0].execution_attempts == 0


def test_v4_store_recovers_interrupted_hierarchical_step_without_reexecution(tmp_path):
    from klonet_agent.ops.privileged.contracts import (
        ExecutionBinding,
        ImplementationPlan,
        PrivilegedStep,
    )
    from klonet_agent.ops.privileged.v4.contracts import ChangePlanV4, ChangeStepV4
    from klonet_agent.ops.privileged.v4.store import V4PlanStore

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
    plan = ChangePlanV4(
        plan_id="priv-v4-nested",
        goal="deploy",
        risk="high",
        steps=[
            ChangeStepV4(
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
    store = V4PlanStore(tmp_path, user_id="u", project_id="p")
    store.save(plan)

    recovered = store.recover(plan.plan_id)

    nested = recovered.steps[0].implementation_plan.steps[0]
    assert recovered.status == "paused"
    assert nested.status == "execution_unknown"
    assert nested.execution_attempts == 1


def test_change_planner_returns_structured_evidence_gap_without_plan():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    outcome = V4ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    assert outcome.status == "need_evidence"
    assert outcome.plan is None
    assert outcome.probe_requests[0].probe == "ports"


def test_planner_rejects_redundant_source_probe_when_screen_git_is_authoritative():
    from klonet_agent.ops.privileged.v4.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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
    assert V4ChangePlannerAgent._authoritative_screen_source_roots(
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
    assert V4ChangePlannerAgent._authoritative_screen_source_roots(
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
    assert V4ChangePlannerAgent._authoritative_screen_source_roots(
        derived_only.goal,
        derived_only,
    ) == {"/home/lzl/vemu_uestc"}

    with pytest.raises(ValueError, match="authoritative Screen source evidence"):
        V4ChangePlannerAgent(None)._outcome(
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
    mixed = V4ChangePlannerAgent(None)._outcome(
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
    assert [item.probe for item in mixed.probe_requests] == ["ports"]


def test_change_planner_builds_only_mutating_change_steps():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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
                            "value": "/srv/v4e2e",
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
                            "objective": "clone source into /srv/v4e2e",
                            "reason": "isolated deployment",
                            "evidence_refs": [evidence_id],
                            "depends_on": [],
                            "risk": "high",
                            "expected_changes": ["/srv/v4e2e is created"],
                            "postconditions": [
                                {
                                    "checker": "file_exists",
                                    "args": {"path": "/srv/v4e2e/.git"},
                                }
                            ],
                        }
                    ],
                }
            )
        ]
    )

    outcome = V4ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    assert outcome.status == "ready"
    assert outcome.plan.schema_version == 4
    assert outcome.plan.steps[0].risk == "high"
    assert outcome.plan.steps[0].evidence_refs == [evidence_id]


def test_change_planner_rejects_readonly_or_summary_step_safely():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    outcome = V4ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    assert outcome.status == "blocked"
    assert "cannot be readonly" in outcome.reason


def test_change_planner_exhausted_schema_repair_returns_blocked_with_strict_hint():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    outcome = V4ChangePlannerAgent(llm).plan(
        "restart isolated service", bundle, conclusion
    )

    assert outcome.status == "blocked"
    assert "postconditions" in outcome.reason
    repair_prompt = llm.calls[1]["messages"][-1]["content"]
    assert '"postconditions"' in repair_prompt
    assert '"checker"' in repair_prompt


def test_change_planner_repairs_blocked_discoverable_implementation_details():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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
                            "args": {"path": "/srv/v4e2e"},
                        }
                    ],
                }
            ],
        }
    )
    llm = FakeLLM([invalid_block, ready])

    outcome = V4ChangePlannerAgent(llm).plan(
        "restart isolated service", bundle, conclusion
    )

    assert outcome.status == "ready"
    assert len(llm.calls) == 2
    assert "Discovery or Binding" in llm.calls[1]["messages"][-1]["content"]


def test_change_planner_allows_three_bounded_contract_repairs():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    outcome = V4ChangePlannerAgent(llm).plan(
        "restart isolated service", bundle, conclusion
    )

    assert outcome.status == "ready"
    assert len(llm.calls) == 4


def test_change_planner_bounds_model_output_and_omits_runaway_repair_context():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    outcome = V4ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    assert outcome.status == "blocked"
    assert len(llm.calls) == 2
    assert llm.calls[0]["kwargs"]["max_tokens"] == 8000
    repair_messages = llm.calls[1]["messages"]
    assistant = next(
        item["content"] for item in repair_messages if item["role"] == "assistant"
    )
    assert assistant == "Previous planner output omitted: contract size exceeded."


def test_change_planner_forces_bounded_function_schema():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    V4ChangePlannerAgent(llm).plan("deploy", bundle, conclusion)

    call = llm.calls[0]
    assert call["kwargs"]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_v4_change_plan"},
    }
    function = call["tools"][0]["function"]
    assert function["name"] == "submit_v4_change_plan"
    properties = function["parameters"]["properties"]
    assert properties["assumptions"]["maxItems"] == 12
    assert properties["assumptions"]["items"]["maxLength"] == 500
    assert properties["resources"]["maxItems"] == 64
    assert properties["changes"]["maxItems"] == 12


def test_planner_compiles_checker_aliases_and_clone_resource_consumers():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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
                        "args": {"path": "/srv/v4e2e", "revision": "abc"},
                    },
                    {
                        "checker": "file_contains",
                        "args": {"path": "/srv/v4e2e/config.py", "pattern": "x"},
                    },
                ],
            }
        ]
    }
    resources = [
        PlanResource("root", "path", "frozen", "instance_root", "/srv/v4e2e"),
        PlanResource("remote", "identifier", "frozen", "source_remote", "g:x/y"),
        PlanResource("branch", "identifier", "frozen", "source_branch", "develop"),
    ]

    V4ChangePlannerAgent._normalize_postcondition_args(data)
    V4ChangePlannerAgent._normalize_core_resource_consumers(data, resources)

    assert data["changes"][0]["postconditions"][0]["args"] == {
        "container": "v4e2e-mysql"
    }
    assert data["changes"][0]["postconditions"][1]["args"] == {
        "repository": "/srv/v4e2e",
        "revision": "abc",
    }
    assert data["changes"][0]["postconditions"][2]["args"] == {
        "path": "/srv/v4e2e/config.py",
        "text": "x",
    }
    assert resources[0].consumers == ["clone-source.repository"]
    assert resources[1].consumers == ["clone-source.url"]
    assert resources[2].consumers == ["clone-source.ref"]


@pytest.mark.parametrize("alias", ["candidates", "candidate_ports"])
def test_planner_canonicalizes_port_probe_candidate_aliases(alias):
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    requests = V4ChangePlannerAgent._probe_requests(
        [
            {
                "probe": "ports",
                "args": {alias: [45561, "45562", 45561]},
                "purpose": "freeze candidates",
            }
        ]
    )

    assert requests[0].args == {"ports": [45561, 45562]}


def test_planner_reassigns_occupied_host_port_from_probed_free_candidates():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.contracts import (
        EvidenceBundle,
        EvidenceRecord,
        ProbeRequest,
    )
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    V4ChangePlannerAgent._normalize_occupied_host_ports(data, resources, bundle)

    assert resources[0].value == 5011
    assert resources[1].value == 45552
    assert data["changes"][0]["title"] == "Start master on port 5011"
    assert data["changes"][0]["postconditions"][0]["args"]["port"] == 5011


def test_deployment_planner_repairs_missing_resources_and_bad_checker_contract():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    invalid = {
        "status": "ready",
        "goal": "deploy isolated instance to /srv/v4e2e",
        "resources": [],
        "changes": [
            {
                "step_id": "deploy",
                "title": "deploy",
                "objective": "clone into /srv/v4e2e",
                "reason": "deploy",
                "evidence_refs": [evidence_id],
                "depends_on": [],
                "risk": "high",
                "expected_changes": ["/srv/v4e2e is created"],
                "postconditions": [
                    {
                        "checker": "file_contains",
                        "args": {"path": "/srv/v4e2e/config.py", "content": "x"},
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
                "value": "/srv/v4e2e",
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
                "value": "/srv/v4e2e/config.py",
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
                        "args": {"path": "/srv/v4e2e/config.py", "text": "x"},
                    }
                ],
            }
        ],
    }
    llm = FakeLLM([json.dumps(invalid), json.dumps(valid)])

    outcome = V4ChangePlannerAgent(llm).plan(
        "deploy isolated instance to /srv/v4e2e",
        bundle,
        conclusion,
    )

    assert outcome.status == "ready"
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    bundle, _, _ = _bundle_and_conclusion()
    resources = [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root", "/srv/v4e2e",
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
                    {"checker": "file_exists", "args": {"path": "/srv/v4e2e"}}
                ],
            }
        ],
        "assumptions": [],
    }

    errors = V4ChangePlannerAgent._ready_contract_errors(
        data,
        (
            "deploy isolated instance to /srv/v4e2e; "
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    bundle, _, _ = _bundle_and_conclusion()
    resources = [
        PlanResource(
            "instance_root", "path", "frozen", "instance_root", "/srv/v4e2e",
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
            "/srv/v4e2e/config.py", "derived", consumers=["configure.path"],
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
                    {"checker": "file_exists", "args": {"path": "/srv/v4e2e/.git"}}
                ],
            },
            {
                "step_id": "configure", "risk": "medium",
                "expected_changes": [
                    "Set master_port to 47001", "Set worker_port to 47002"
                ],
                "postconditions": [
                    {"checker": "file_contains", "args": {"path": "/srv/v4e2e/config.py", "text": "master_port = 47001"}},
                    {"checker": "file_contains", "args": {"path": "/srv/v4e2e/config.py", "text": "worker_port = 47002"}},
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

    errors = V4ChangePlannerAgent._ready_contract_errors(
        data,
        "deploy isolated instance to /srv/v4e2e; instance name fixed as v4e2e",
        resources,
        bundle,
    )

    assert "change configure uses unfrozen port=47002" in errors
    assert not any("configuration assertions" in error for error in errors)
    assert not any("multiple screen sessions" in error for error in errors)


def test_change_planner_rejects_resource_consumer_with_multiple_owners():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._ready_contract_errors(
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    resources = [
        PlanResource(
            "instance_root",
            "path",
            "frozen",
            "instance_root",
            "/srv/v4e2e",
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

    normalized = V4ChangePlannerAgent._normalize_derived_resources(
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    data = {
        "changes": [
            {
                "postconditions": [
                    {
                        "checker": "git_revision",
                        "args": {"path": "/srv/v4e2e", "revision": "abc123"},
                    },
                    {
                        "checker": "file_contains",
                        "args": {"path": "/srv/v4e2e/config.py", "content": "47001"},
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

    V4ChangePlannerAgent._normalize_postcondition_args(data)

    checks = data["changes"][0]["postconditions"]
    assert checks[0]["args"] == {
        "repository": "/srv/v4e2e",
        "revision": "abc123",
    }
    assert checks[1]["args"] == {
        "path": "/srv/v4e2e/config.py",
        "text": "47001",
    }
    assert checks[2]["args"] == {"session": "v4e2e_web"}
    assert checks[3]["args"] == {"pattern": "v4e2e-redis"}


def test_change_planner_derives_frozen_source_revision_for_clone_binding():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    resources = [
        PlanResource(
            "instance_root",
            "path",
            "frozen",
            "instance_root",
            "/srv/v4e2e",
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
                            "repository": "/srv/v4e2e",
                            "revision": "a" * 40,
                        },
                    }
                ],
            }
        ]
    }

    normalized = V4ChangePlannerAgent._normalize_derived_resources(data, resources)

    revision = next(item for item in normalized if item.role == "source_revision")
    assert revision.value == "a" * 40
    assert revision.consumers == ["clone.revision"]


def test_change_planner_derives_fixed_nginx_name_from_site_path():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    normalized = V4ChangePlannerAgent._normalize_derived_resources(
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._complete_klonet_contract_errors(
        data, resources
    )

    assert "complete Klonet runtime missing components=celery" in errors
    assert "complete Klonet runtime missing port resources=web_terminal_port" in errors
    assert "complete Klonet runtime missing Screen sessions=v4e2e_c,v4e2e_m,v4e2e_w" in errors


def test_complete_klonet_deployment_contract_requires_config_fields_and_master_nginx_upstream():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._complete_klonet_contract_errors(data, resources)

    assert (
        "complete Klonet configuration missing attributes="
        "master_port,worker_port,web_terminal_port,mysql_port,redis_port,"
        "rabbitmq_port,master_ip,mysql_ip,rabbitmq_ip,celery_redis_port_db,"
        "celery_rabbitmq_port_db,proj_config"
    ) in errors
    assert "complete Klonet Nginx must proxy to frozen master_port=47001" in errors


def test_complete_klonet_deployment_contract_rejects_unsupported_data_server_component():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    resource = PlanResource(
        "data_server_port", "port", "frozen", "data_server_port", 47004,
        "planner_choice", consumers=["config.data_server_port"],
    )
    errors = V4ChangePlannerAgent._complete_klonet_contract_errors(
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    errors = V4ChangePlannerAgent._complete_klonet_contract_errors(
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


def test_change_planner_does_not_rederive_explicit_internal_port_as_host_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    normalized = V4ChangePlannerAgent._normalize_derived_resources(
        data, resources
    )

    port_resources = {item.value: item for item in normalized if item.kind == "port"}
    assert port_resources[6379].role == "container_internal_port"
    assert port_resources[47006].role == "selected_host_port"
    assert len([item for item in normalized if item.value == 6379]) == 1


def test_change_planner_freezes_standard_stateful_container_internal_ports():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    normalized = V4ChangePlannerAgent._normalize_derived_resources(data, [])
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    root = PlanResource(
        "instance_root",
        "path",
        "frozen",
        "instance_root",
        "/srv/v4e2e",
        "user_input",
        consumers=["change-1.repository"],
    )
    data = {
        "changes": [
            {
                "step_id": "change-2",
                "postconditions": [
                    {"checker": "file_exists", "args": {"path": "/srv/v4e2e/a.py"}},
                    {"checker": "file_exists", "args": {"path": "/srv/v4e2e/b.py"}},
                ],
            }
        ]
    }

    normalized = V4ChangePlannerAgent._normalize_derived_resources(data, [root])
    derived = [item for item in normalized if item.value in {"/srv/v4e2e/a.py", "/srv/v4e2e/b.py"}]

    assert len(derived) == 2
    assert {item.consumers[0] for item in derived} == {
        "change-2.path_1",
        "change-2.path_2",
    }


def test_isolation_contract_distinguishes_negated_and_positive_reuse_claims():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    negative = V4ChangePlannerAgent._ready_contract_errors(
        {"changes": [], "assumptions": ["Never reuse or share existing containers."]},
        "deploy an isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )
    positive = V4ChangePlannerAgent._ready_contract_errors(
        {"changes": [], "assumptions": ["Use the existing shared Redis container."]},
        "deploy an isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert "isolated deployment cannot reuse existing resources" not in negative
    assert "isolated deployment cannot reuse existing resources" in positive


def test_isolated_application_start_must_depend_on_stateful_provisioning():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._ready_contract_errors(
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance without reusing existing services",
        [],
        _bundle_and_conclusion()[0],
    )

    assert "isolated stateful service must use a new named container=redis" in errors


def test_celery_start_is_not_misclassified_as_stateful_service_provisioning():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert not any("stateful service must use" in error for error in errors)


def test_isolated_nginx_must_depend_on_started_application():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": changes, "assumptions": []},
        "deploy a new isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )

    assert "Nginx activation must depend on earlier application start=nginx:start-app" in errors


def test_change_planner_topologically_orders_forward_semantic_dependencies():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    data = {
        "changes": [
            {"step_id": "start", "depends_on": ["state"]},
            {"step_id": "state", "depends_on": ["clone"]},
            {"step_id": "clone", "depends_on": []},
        ]
    }

    V4ChangePlannerAgent._normalize_change_order(data)

    assert [item["step_id"] for item in data["changes"]] == [
        "clone", "state", "start"
    ]


def test_change_planner_rejects_verification_only_semantic_change():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._ready_contract_errors(
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    V4ChangePlannerAgent._normalize_verification_changes(data)

    assert [item["step_id"] for item in data["changes"]] == ["nginx"]
    assert data["changes"][0]["postconditions"][-1]["checker"] == "http_status"
    assert data["resources"][0]["consumers"] == ["nginx.url"]


def test_isolated_nginx_requires_explicit_frozen_dedicated_listen_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    missing = V4ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy v4e2e", "changes": [change], "assumptions": []},
        "deploy a new isolated instance",
        [],
        _bundle_and_conclusion()[0],
    )
    explicit = V4ChangePlannerAgent._ready_contract_errors(
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
    shared = V4ChangePlannerAgent._ready_contract_errors(
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
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    errors = V4ChangePlannerAgent._ready_contract_errors(
        {"goal": "deploy isolated instance", "changes": changes, "assumptions": []},
        "deploy isolated instance",
        [port],
        _bundle_and_conclusion()[0],
    )

    assert not any("Nginx activation must depend" in error for error in errors)
    assert not any("dedicated listen port" in error for error in errors)


def test_change_planner_adds_http_check_for_frozen_nginx_listen_port():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    V4ChangePlannerAgent._normalize_nginx_postconditions(data, resources)

    assert data["changes"][0]["postconditions"][-1] == {
        "checker": "http_status",
        "args": {"url": "http://127.0.0.1:47008/healthz", "expected_status": 200},
    }


def test_nginx_health_check_moves_from_prepare_to_activation():
    from klonet_agent.ops.privileged.contracts import PlanResource
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    V4ChangePlannerAgent._normalize_nginx_postconditions(data, resources)

    assert all(
        check["checker"] not in {"http_status", "port_listening"}
        for check in data["changes"][0]["postconditions"]
    )
    assert data["changes"][1]["postconditions"][-1] == {
        "checker": "http_status",
        "args": {"url": "http://127.0.0.1:47008/healthz", "expected_status": 200},
    }


def test_http_observation_does_not_claim_a_listening_port():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

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

    assert V4ChangePlannerAgent._declared_listening_ports_by_step(data) == {
        "verify": set(),
        "server": {47009},
    }


def test_deployment_planner_turns_unproven_frozen_port_into_evidence_request():
    from klonet_agent.ops.privileged.v4.contracts import EvidenceRecord, ProbeRequest
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    payload = {
        "status": "ready",
        "goal": "model-rewritten deployment goal",
        "resources": [
            {
                "name": "instance_root", "kind": "path", "status": "frozen",
                "role": "instance_root", "value": "/srv/v4e2e",
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
                    {"checker": "file_exists", "args": {"path": "/srv/v4e2e"}}
                ],
            }
        ],
    }
    llm = FakeLLM([json.dumps(payload)])

    outcome = V4ChangePlannerAgent(llm).plan(
        "deploy v4e2e to /srv/v4e2e", bundle, conclusion
    )

    assert outcome.status == "need_evidence"
    assert outcome.probe_requests == [
        ProbeRequest("ports", {"ports": [47002]}, "verify frozen port availability")
    ]
    assert outcome.candidate_plan is not None
    assert outcome.candidate_plan.goal == "deploy v4e2e to /srv/v4e2e"
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
    finalized = V4ChangePlannerAgent.finalize_candidate(
        outcome.candidate_plan,
        bundle,
    )

    assert finalized.status == "ready"
    assert finalized.plan is outcome.candidate_plan


def test_planner_normalizes_derived_config_path_and_hidden_selected_port():
    from klonet_agent.ops.privileged.v4.planner import V4ChangePlannerAgent

    bundle, conclusion, evidence_id = _bundle_and_conclusion()
    payload = {
        "status": "ready",
        "goal": "deploy v4e2e",
        "resources": [
            {
                "name": "instance_root", "kind": "path", "status": "frozen",
                "role": "instance_root", "value": "/srv/v4e2e",
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
                    {"checker": "file_exists", "args": {"path": "/srv/v4e2e/.git"}}
                ],
            },
            {
                "step_id": "configure", "title": "configure ports",
                "objective": "configure master port", "reason": "isolate",
                "evidence_refs": [evidence_id], "depends_on": ["clone"],
                "risk": "medium", "expected_changes": ["Set master_port to 47009"],
                "postconditions": [
                    {"checker": "file_contains", "args": {"path": "/srv/v4e2e/config.py", "text": "master_port = 47009"}}
                ],
            },
        ],
        "assumptions": [],
    }

    outcome = V4ChangePlannerAgent(FakeLLM([json.dumps(payload)])).plan(
        "deploy isolated v4e2e to /srv/v4e2e", bundle, conclusion
    )

    assert outcome.status == "need_evidence"
    assert outcome.probe_requests[0].args == {"ports": [47009]}
    resources = outcome.candidate_plan.resources
    assert any(item.kind == "port" and item.value == 47009 for item in resources)
    assert any(item.kind == "path" and item.value == "/srv/v4e2e/config.py" for item in resources)
