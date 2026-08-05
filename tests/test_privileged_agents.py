from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class FakeLLM:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append(
            {"messages": messages, "tools": tools, "kwargs": kwargs}
        )
        content = self.contents.pop(0)
        if tools:
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name=tools[0]["function"]["name"],
                            arguments=content,
                        )
                    )
                ],
            )
        else:
            message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _planner_payload(**overrides):
    payload = {
        "status": "ready",
        "goal": "restart nginx",
        "risk": "low",
        "assumptions": [],
        "steps": [
            {
                "step_id": "reload-nginx",
                "title": "reload nginx",
                "objective": "reload nginx with the reviewed configuration",
                "reason": "the configuration must become active",
                "evidence_refs": ["systemd available"],
                "depends_on": [],
                "expected_effects": ["nginx reloads"],
                "success_criteria": [
                    "nginx configuration syntax is valid",
                    "nginx service is active",
                ],
                "risk_suggestion": "medium",
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_planner_uses_toolless_prompt_and_emits_semantic_plan_only():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM([_planner_payload()])

    plan = PrivilegedPlannerAgent(llm).plan(
        "restart nginx",
        environment_context="systemd available",
    )

    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"] is None
    assert llm.calls[0]["kwargs"]["reasoning_effort"] == "high"
    prompt = llm.calls[0]["messages"]
    assert prompt[0]["role"] == "system"
    assert "Planner" in prompt[0]["content"]
    assert "Verifier" not in prompt[0]["content"]
    assert plan.risk == "medium"
    assert plan.status == "draft"
    assert plan.is_authorized is False
    assert plan.steps[0].action == ""
    assert plan.steps[0].execution_binding is None
    assert plan.steps[0].success_criteria == [
        "nginx configuration syntax is valid",
        "nginx service is active",
    ]


def test_planner_allows_model_to_raise_but_not_lower_registry_risk():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM([_planner_payload(risk="high")])

    plan = PrivilegedPlannerAgent(llm).plan("restart nginx")

    assert plan.risk == "high"
    assert plan.status == "draft"
    assert plan.steps[0].risk == "medium"
    assert plan.steps[0].approval_scope == "plan"


def test_planner_repairs_invalid_json_once():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(["not json", _planner_payload()])

    plan = PrivilegedPlannerAgent(llm).plan("restart nginx")

    assert plan.goal == "restart nginx"
    assert len(llm.calls) == 2
    assert "repair" in llm.calls[1]["messages"][-1]["content"].lower()


def test_planner_fails_safe_after_invalid_repairs_are_exhausted():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(["not json"] * 4)

    try:
        PrivilegedPlannerAgent(llm).plan("restart nginx")
    except ValueError as exc:
        assert "valid semantic plan" in str(exc)
    else:
        raise AssertionError("planner must fail safe")


def test_planner_can_repair_two_independent_contract_errors():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(["not json", "still not json", _planner_payload()])

    plan = PrivilegedPlannerAgent(llm).plan("restart nginx")

    assert plan.goal == "restart nginx"
    assert len(llm.calls) == 3


def test_new_instance_plan_requires_isolated_prepared_instance_root():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.planner import _validate_deployment_plan_shape

    steps = [
        PrivilegedStep(
            step_id="start",
            title="启动 lht 平台组件",
            objective="启动一个新的 lht 平台实例",
            risk="medium",
        ),
    ]
    resources = [
        PlanResource(
            name="source_root",
            kind="path",
            status="frozen",
            role="source_repo_root",
            value="/home/lzl/vemu_uestc",
            source="evidence",
        )
    ]

    with pytest.raises(ValueError, match="isolation_missing"):
        _validate_deployment_plan_shape("新增 lht 平台实例", steps, resources)


def test_new_instance_http_health_must_depend_on_backend_start():
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.planner import _validate_deployment_plan_shape

    prepare = PrivilegedStep(
        step_id="prepare",
        title="复制源码目录",
        objective="复制源码到独立 lht 目录",
        risk="medium",
    )
    start = PrivilegedStep(
        step_id="start",
        title="启动 lht 平台组件",
        objective="启动新平台服务",
        depends_on=["prepare"],
        risk="medium",
    )
    nginx = PrivilegedStep(
        step_id="nginx",
        title="配置 Nginx 路由",
        objective="配置 /lht/ 反向代理",
        success_criteria=["HTTP 路由响应 200"],
        risk="medium",
    )
    resources = [
        PlanResource(
            name="instance_root",
            kind="path",
            status="frozen",
            role="instance_root",
            value="/home/lzl/lht",
            source="derived",
            consumers=["prepare.destination"],
        ),
        PlanResource(
            name="source_root",
            kind="path",
            status="frozen",
            role="source_repo_root",
            value="/home/lzl/vemu_uestc",
            source="evidence",
            consumers=["prepare.source"],
        ),
    ]

    with pytest.raises(ValueError, match="http_health_requires_backend_start"):
        _validate_deployment_plan_shape(
            "新增 lht 平台实例",
            [prepare, nginx, start],
            resources,
        )


def test_new_instance_copy_source_must_come_from_grounded_environment(tmp_path):
    from klonet_agent.ops.privileged.context import GroundedPlanContext
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.planner import _validate_deployment_plan_shape

    grounded = tmp_path / "grounded-source"
    stale = tmp_path / "stale-knowledge-source"
    destination = tmp_path / "lht"
    grounded.mkdir()
    stale.mkdir()
    steps = [
        PrivilegedStep(
            step_id="copy",
            title="复制源码目录",
            objective="复制源码到独立 lht 目录",
            expected_changes=[str(destination)],
            success_criteria=["目标目录存在"],
            risk="medium",
        )
    ]
    resources = [
        PlanResource(
            name="instance_root",
            kind="path",
            status="frozen",
            role="instance_root",
            value=str(destination),
            source="derived",
            consumers=["copy.destination"],
        ),
        PlanResource(
            name="source_root",
            kind="path",
            status="frozen",
            role="source_repo_root",
            value=str(stale),
            source="environment_evidence",
            consumers=["copy.source"],
        ),
    ]
    context = GroundedPlanContext(
        knowledge_evidence="old documentation mentions the stale path",
        environment_evidence="current source was inspected",
        action_catalog="",
        facts={
            "environment_model": {
                "projects": [
                    {
                        "candidate_root": str(grounded),
                        "source_repo_root": str(grounded),
                        "platform_root": str(grounded),
                        "backend_package_root": str(grounded),
                    }
                ]
            }
        },
    )

    with pytest.raises(ValueError, match="not_grounded_in_environment"):
        _validate_deployment_plan_shape(
            "新增 lht 平台实例",
            steps,
            resources,
            grounded_context=context,
        )


def test_authoring_nginx_http_proxy_text_is_not_a_health_check(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.planner import _validate_deployment_plan_shape

    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "lht"
    prepare = PrivilegedStep(
        step_id="copy",
        title="复制源码",
        objective="复制源码到新实例",
        success_criteria=["目标目录存在"],
        risk="medium",
    )
    author = PrivilegedStep(
        step_id="author_nginx",
        title="编写 Nginx 配置",
        objective="写入 proxy_pass http://127.0.0.1:46001",
        depends_on=["copy"],
        success_criteria=["配置含 proxy_pass http://127.0.0.1:46001"],
        risk="medium",
    )
    start = PrivilegedStep(
        step_id="start",
        title="启动平台组件",
        objective="启动新平台服务",
        depends_on=["copy"],
        success_criteria=["screen 会话存在"],
        risk="high",
    )
    resources = [
        PlanResource(
            name="instance_root",
            kind="path",
            status="frozen",
            role="instance_root",
            value=str(destination),
            source="derived",
            consumers=["copy.destination"],
        ),
        PlanResource(
            name="source_root",
            kind="path",
            status="frozen",
            role="source_repo_root",
            value=str(source),
            source="environment_evidence",
            consumers=["copy.source"],
        ),
    ]

    _validate_deployment_plan_shape(
        "新增 lht 平台实例",
        [prepare, author, start],
        resources,
    )


def test_instance_directory_preparation_does_not_require_a_copy_source(tmp_path):
    from klonet_agent.ops.privileged.contracts import PlanResource, PrivilegedStep
    from klonet_agent.ops.privileged.planner import _validate_deployment_plan_shape

    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "lht"
    create = PrivilegedStep(
        step_id="prepare_root",
        title="准备实例根目录",
        objective="创建 lht 实例目录",
        success_criteria=["目录存在"],
        risk="low",
    )
    copy = PrivilegedStep(
        step_id="copy_source",
        title="复制源码",
        objective="复制源码到 lht",
        depends_on=["prepare_root"],
        success_criteria=["源码存在"],
        risk="medium",
    )
    start = PrivilegedStep(
        step_id="start",
        title="启动平台组件",
        objective="启动新平台服务",
        depends_on=["copy_source"],
        success_criteria=["会话存在"],
        risk="high",
    )
    resources = [
        PlanResource(
            name="instance_root",
            kind="path",
            status="frozen",
            role="instance_root",
            value=str(destination),
            source="derived",
            consumers=[
                "prepare_root.path",
                "copy_source.destination",
            ],
        ),
        PlanResource(
            name="source_root",
            kind="path",
            status="frozen",
            role="source_repo_root",
            value=str(source),
            source="environment_evidence",
            consumers=["copy_source.source"],
        ),
    ]

    _validate_deployment_plan_shape(
        "新增 lht 平台实例",
        [create, copy, start],
        resources,
    )


def test_copy_source_conflict_prefers_existing_directory(tmp_path):
    from klonet_agent.ops.privileged.contracts import PrivilegedStep
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    existing = tmp_path / "source"
    existing.mkdir()
    future = tmp_path / "lht" / "source"
    steps = [
        PrivilegedStep(
            step_id="copy",
            title="复制源码",
            objective="复制源码到新实例",
            success_criteria=["目标存在"],
            risk="medium",
        ),
        PrivilegedStep(
            step_id="configure",
            title="配置新实例",
            objective="配置 lht 项目",
            depends_on=["copy"],
            success_criteria=["配置完成"],
            risk="medium",
        ),
    ]
    resources = PrivilegedPlannerAgent._build_plan_resources(
        {
            "resources": [
                {
                    "name": "existing_source",
                    "kind": "path",
                    "status": "frozen",
                    "role": "source_repo_root",
                    "value": str(existing),
                    "source": "environment_evidence",
                    "consumers": [
                        "copy.source",
                        "configure.project_root",
                    ],
                },
                {
                    "name": "future_source",
                    "kind": "path",
                    "status": "frozen",
                    "role": "copied_source_root",
                    "value": str(future),
                    "source": "derived",
                    "consumers": ["copy.source"],
                },
                {
                    "name": "instance_root",
                    "kind": "path",
                    "status": "frozen",
                    "role": "instance_root",
                    "value": str(tmp_path / "lht"),
                    "source": "derived",
                    "consumers": ["configure.project_root"],
                },
            ]
        },
        steps,
    )

    owners = [
        item for item in resources if "copy.source" in item.consumers
    ]
    assert [(item.name, item.value) for item in owners] == [
        ("existing_source", str(existing))
    ]
    project_roots = [
        item for item in resources
        if "configure.project_root" in item.consumers
    ]
    assert [item.name for item in project_roots] == ["instance_root"]


def test_planner_rejects_oversized_internal_plan_and_requests_compaction():
    oversized = {
        "status": "ready",
        "goal": "deploy platform",
        "risk": "medium",
        "steps": [
            {
                "step_id": "step-%s" % index,
                "title": "检查 %s" % index,
                "objective": "inspect item %s" % index,
                "reason": "collect required evidence",
                "success_criteria": ["item is observed"],
                "risk_suggestion": "readonly",
            }
            for index in range(9)
        ],
    }
    llm = FakeLLM([json.dumps(oversized), _planner_payload()])

    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    plan = PrivilegedPlannerAgent(llm).plan("deploy platform")

    assert len(llm.calls) == 2
    assert "maximum of 8" in llm.calls[1]["messages"][-1]["content"]
    assert len(plan.steps) == 1


def test_planner_rejects_hard_denied_command_even_if_model_calls_it_low_risk():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    denied = _planner_payload(
                goal="wipe host",
                steps=[
                    {
                        "step_id": "wipe",
                        "title": "wipe",
                        "command": "sudo rm -rf /",
                        "risk": "low",
                        "postconditions": [],
                    }
                ],
            )
    llm = FakeLLM([denied] * 4)

    try:
        PrivilegedPlannerAgent(llm).plan("wipe host")
    except ValueError as exc:
        assert "must not choose execution implementation" in str(exc)
    else:
        raise AssertionError("catastrophic command must be denied")


def test_semantic_nginx_plan_keeps_observable_success_criteria():
    from klonet_agent.ops.privileged.planner import PrivilegedPlannerAgent

    llm = FakeLLM(
        [
            _planner_payload(
                goal="deploy nginx config",
                resources=[
                    {
                        "name": "nginx_config_path",
                        "kind": "path",
                        "status": "frozen",
                        "value": "/etc/nginx/nginx.conf",
                        "source": "environment_evidence",
                        "reason": "",
                        "resolve_before": "",
                        "consumers": [],
                    }
                ],
            )
        ]
    )

    plan = PrivilegedPlannerAgent(llm).plan("deploy nginx config")

    assert plan.status == "draft"
    assert plan.authorized_hash == ""
    assert "nginx configuration syntax is valid" in plan.steps[0].success_criteria
    assert "nginx service is active" in plan.steps[0].success_criteria


def _verified_step(return_code=0, timed_out=False, postconditions=None):
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence, PrivilegedStep

    return PrivilegedStep(
        step_id="restart-nginx",
        title="restart nginx",
        command="sudo systemctl restart nginx",
        risk="medium",
        status="executed",
        postconditions=postconditions
        if postconditions is not None
        else [{"checker": "exit_code_zero", "args": {}}],
        evidence=ExecutionEvidence(
            return_code=return_code,
            timed_out=timed_out,
            stdout="ok",
        ),
    )


def _plan_with_step(step):
    from klonet_agent.ops.privileged.contracts import PrivilegedPlan

    return PrivilegedPlan(
        plan_id="priv-123",
        goal="restart nginx",
        risk="medium",
        status="verifying",
        steps=[step],
    )


def test_verifier_uses_agent_when_exit_code_is_the_only_evidence():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "summary": "evidence proves service is healthy",
                    "confirmed_facts": ["exit code is zero"],
                    "failed_criteria": [],
                    "missing_evidence": [],
                    "reflection": "",
                    "recommended_next_focus": "",
                }
            )
        ]
    )
    step = _verified_step()

    decision = PrivilegedVerifierAgent(llm).verify_step(_plan_with_step(step), step)

    assert decision.status == "passed"
    assert decision.goal_achieved is True
    assert len(llm.calls) == 1


def test_verifier_receives_goal_and_semantic_step_context():
    from klonet_agent.ops.privileged.contracts import ExecutionEvidence, PrivilegedPlan
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "summary": "current user and home directory were identified",
                    "confirmed_facts": ["current user observed"],
                    "failed_criteria": [],
                    "missing_evidence": [],
                    "reflection": "",
                    "recommended_next_focus": "",
                }
            )
        ]
    )
    step = _verified_step()
    step.title = "识别当前用户和主目录"
    step.command = "whoami && echo $HOME"
    step.evidence = ExecutionEvidence(
        return_code=0,
        stdout="klonet-agent\n/home/klonet-agent\n",
    )
    plan = PrivilegedPlan(
        plan_id="priv-deploy",
        goal="部署 Klonet 平台",
        risk="medium",
        status="verifying",
        steps=[step],
    )

    decision = PrivilegedVerifierAgent(llm).verify_step(plan, step)

    assert decision.status == "passed"
    assert len(llm.calls) == 1
    assert "部署 Klonet 平台" in llm.calls[0]["messages"][1]["content"]


def test_verifier_trusts_passed_state_checker_without_calling_llm(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    target = tmp_path / "ready"
    target.write_text("ready", encoding="utf-8")
    step = _verified_step(
        postconditions=[
            {"checker": "file_exists", "args": {"path": str(target)}},
        ],
    )
    llm = FakeLLM([])

    decision = PrivilegedVerifierAgent(llm).verify_step(
        _plan_with_step(step),
        step,
    )

    assert decision.status == "passed"
    assert decision.goal_achieved is True
    assert decision.reason == "all deterministic state checks passed"
    assert llm.calls == []


def test_verifier_can_mark_exit_code_only_evidence_inconclusive():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "inconclusive",
                    "summary": "exit code alone does not prove the service restarted",
                    "confirmed_facts": ["exit code is zero"],
                    "failed_criteria": [],
                    "missing_evidence": ["service state"],
                    "reflection": "execution success is not state success",
                    "recommended_next_focus": "check service state",
                }
            )
        ]
    )
    step = _verified_step()

    decision = PrivilegedVerifierAgent(llm).verify_step(
        _plan_with_step(step),
        step,
    )

    assert decision.status == "inconclusive"
    assert decision.goal_achieved is False
    assert len(llm.calls) == 1


def test_verifier_never_allows_llm_passed_to_override_nonzero_exit():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "goal_achieved": True,
                    "reason": "looks fine",
                    "next_action": "",
                }
            )
        ]
    )
    step = _verified_step(return_code=9)

    decision = PrivilegedVerifierAgent(llm).verify_step(_plan_with_step(step), step)

    assert decision.status == "failed"
    assert decision.goal_achieved is False
    assert "return_code=9" in decision.failures


def test_verifier_never_allows_zero_exit_to_hide_failed_state_check(tmp_path):
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "status": "passed",
                    "goal_achieved": True,
                    "reason": "command returned zero",
                    "next_action": "",
                }
            )
        ]
    )
    step = _verified_step(
        return_code=0,
        postconditions=[
            {"checker": "file_exists", "args": {"path": str(tmp_path / "missing")}}
        ],
    )

    decision = PrivilegedVerifierAgent(llm).verify_step(_plan_with_step(step), step)

    assert decision.status == "failed"
    assert decision.goal_achieved is False
    assert decision.failures == ["file_exists"]


def test_verifier_treats_required_unavailable_checker_as_inconclusive():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = _verified_step(
        postconditions=[{"checker": "unknown-required-checker", "args": {}}]
    )

    decision = PrivilegedVerifierAgent(None).verify_step(_plan_with_step(step), step)

    assert decision.status == "inconclusive"
    assert decision.goal_achieved is False
    assert decision.missing_evidence == ["unknown-required-checker"]


def test_verifier_marks_timeout_blocked_and_does_not_reexecute():
    from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent

    step = _verified_step(return_code=None, timed_out=True)

    decision = PrivilegedVerifierAgent(None).verify_step(_plan_with_step(step), step)

    assert decision.status == "blocked"
    assert decision.next_action == "inspect current state; do not auto-reexecute"
