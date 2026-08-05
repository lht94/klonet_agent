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
    assert "missing_required_args=text" in repair
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
