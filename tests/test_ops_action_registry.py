def test_registry_rejects_unknown_action():
    from klonet_agent.ops.actions import DEFAULT_OPS_ACTION_REGISTRY

    assert DEFAULT_OPS_ACTION_REGISTRY.get("run_model_shell") is None


def test_registry_validates_resolved_paths_against_allowed_roots():
    from klonet_agent.ops.actions import OpsActionRegistry, OpsActionSpec
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        allowed = temp_dir / "allowed"
        allowed.mkdir()
        registry = OpsActionRegistry(
            [OpsActionSpec("deploy", "_deploy", path_args=("project_root",))],
            allowed_path_roots=[allowed],
        )
        spec = registry.require("deploy")

        assert registry.validate_args(spec, {"project_root": str(allowed / "103")}) == ""
        assert registry.validate_args(spec, {"project_root": str(temp_dir / "outside")}) == "path_not_allowlisted=project_root"
        assert registry.validate_args(spec, {"project_root": "relative/project"}) == "invalid_path_arg=project_root"


def test_controlled_runner_dispatches_through_action_registry():
    from klonet_agent.ops.actions import OpsActionRegistry, OpsActionSpec
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledRecipeRunner

    registry = OpsActionRegistry([OpsActionSpec("manual_checkpoint", "_manual_checkpoint")])
    result = ControlledRecipeRunner(dry_run=True, action_registry=registry)(
        OperationPlan("p1", "restart_platform", "103", "test"),
        OperationStep("precheck", "precheck", "test", recipe_id="manual_checkpoint", recipe_args={"reason": "checked"}),
    )
    assert result.status == "completed"
    assert "reason=checked" in result.output


def test_controlled_runner_blocks_action_missing_from_registry():
    from klonet_agent.ops.actions import OpsActionRegistry
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledRecipeRunner

    result = ControlledRecipeRunner(dry_run=True, action_registry=OpsActionRegistry([]))(
        OperationPlan("p1", "restart_platform", "103", "test"),
        OperationStep("x", "x", "x", recipe_id="restart_screen_component"),
    )
    assert result.status == "blocked"
    assert "action_not_allowlisted=restart_screen_component" in result.output


def test_plan_accepts_canonical_action_bindings_and_persists_action_args():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="deploy_platform",
            target="103",
            action_bindings={
                "prepare-files": {
                    "action": "write_ops_file",
                    "args": {"path": "/etc/nginx/conf.d/103.conf", "content": "server {}"},
                }
            },
        )
        loaded = store.load_plan(plan.plan_id)

    step = next(item for item in loaded.steps if item.step_id == "prepare-files")
    assert step.action == "write_ops_file"
    assert step.args == {"path": "/etc/nginx/conf.d/103.conf", "content": "server {}"}
    assert step.recipe_id == "write_ops_file"
    assert step.recipe_args == step.args


def test_old_recipe_only_plan_is_migrated_when_loaded():
    import json

    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        path = temp_dir / "legacy-plan.json"
        path.write_text(
            json.dumps(
                {
                    "plan_id": "legacy-plan",
                    "operation": "restart_platform",
                    "target": "103",
                    "objective": "restart",
                    "steps": [
                        {
                            "step_id": "restart-master",
                            "title": "restart",
                            "purpose": "restart",
                            "recipe_id": "restart_screen_component",
                            "recipe_args": {
                                "platform": "103",
                                "component": "master",
                                "screen_session": "103_m",
                                "project_root": "/home/klonet-agent/platforms/103_project",
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan = store.load_plan("legacy-plan")

    assert plan.steps[0].action == "restart_screen_component"
    assert plan.steps[0].args == plan.steps[0].recipe_args


def test_runner_uses_configured_allowed_roots(monkeypatch):
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledRecipeRunner

    monkeypatch.setenv("KLONET_AGENT_OPS_ALLOWED_ROOTS", "/home/klonet-agent/platforms")
    result = ControlledRecipeRunner(dry_run=True)(
        OperationPlan("p1", "deploy_platform", "103", "deploy", status="approved"),
        OperationStep(
            "prepare-files",
            "prepare",
            "prepare",
            action="prepare_project_files",
            args={"project_root": "/root/not-allowed"},
        ),
    )

    assert result.status == "blocked"
    assert "path_not_allowlisted=project_root" in result.output


def test_custom_plan_steps_are_not_forced_into_deploy_template():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        plan = OperationPlanStore(temp_dir).create_plan(
            operation="deploy_platform",
            target="lht",
            steps=[
                {
                    "step_id": "start-mysql",
                    "title": "启动已有 MySQL 容器",
                    "action": "start_docker_container",
                    "args": {"name": "mysql-vemu"},
                },
                {
                    "step_id": "verify-mysql",
                    "title": "验证 3306 端口",
                },
            ],
        )

    assert [step.step_id for step in plan.steps] == ["start-mysql", "verify-mysql"]
    assert plan.steps[0].action == "start_docker_container"
    assert plan.steps[0].args == {"name": "mysql-vemu"}
    assert plan.steps_source == "custom"


def test_custom_precheck_checkpoint_completes_without_action():
    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        store = OperationPlanStore(temp_dir)
        plan = store.create_plan(
            operation="deploy_platform",
            target="lht",
            steps=[
                {"step_id": "precheck", "title": "环境证据已确认"},
                {"step_id": "install-deps", "title": "安装依赖"},
            ],
        )
        plan.status = "approved"
        store.save_plan(plan)
        result = store.execute_step(plan.plan_id, "precheck")

    assert "result_status=completed" in result
    assert "readonly_or_manual_checkpoint_completed" in result


def test_custom_plan_rejects_shell_command_step():
    import pytest

    from klonet_agent.ops.operations import OperationPlanStore
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir, pytest.raises(ValueError, match=r"action \+ args"):
        OperationPlanStore(temp_dir).create_plan(
            operation="deploy_platform",
            target="lht",
            steps=[{"step_id": "bad", "title": "bad", "command": "docker start mysql"}],
        )


def test_start_docker_container_action_builds_fixed_helper_command():
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledActionRunner

    result = ControlledActionRunner(dry_run=True)(
        OperationPlan("p1", "deploy_platform", "lht", "start mysql", status="approved"),
        OperationStep(
            "start-mysql",
            "start mysql",
            "start existing container",
            action="start_docker_container",
            args={"name": "mysql-vemu"},
        ),
    )

    assert result.status == "completed"
    assert "start-docker-container --dry-run --name mysql-vemu" in result.output


def test_write_ops_file_incrementally_inserts_after_unique_anchor():
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledActionRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        target = temp_dir / "config.py"
        target.write_text(
            "class WtxConfig:\n    mysql_ip = '127.0.0.1'\n    redis_port = 6379\n",
            encoding="utf-8",
        )
        step = OperationStep(
            "patch-mysql-port",
            "patch mysql port",
            "insert mysql port",
            action="write_ops_file",
            args={
                "path": str(target),
                "mode": "insert_after",
                "anchor": "    mysql_ip = '127.0.0.1'",
                "content": "    mysql_port = 3307",
                "expected_matches": "1",
            },
        )
        runner = ControlledActionRunner(dry_run=False)
        first = runner(OperationPlan("p1", "deploy_platform", "lht", "patch"), step)
        second = runner(OperationPlan("p1", "deploy_platform", "lht", "patch"), step)
        updated = target.read_text(encoding="utf-8")

    assert first.status == "completed"
    assert "mode=insert_after" in first.output
    assert updated.count("mysql_port = 3307") == 1
    assert second.status == "completed"
    assert "already_applied=true" in second.output
    assert "environment_changed=false" in second.output


def test_write_ops_file_incremental_edit_blocks_ambiguous_anchor():
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledActionRunner
    from tests.helpers import local_temp_dir

    with local_temp_dir() as temp_dir:
        target = temp_dir / "config.py"
        target.write_text("mysql_ip = 'x'\nmysql_ip = 'x'\n", encoding="utf-8")
        result = ControlledActionRunner(dry_run=False)(
            OperationPlan("p1", "deploy_platform", "lht", "patch"),
            OperationStep(
                "patch",
                "patch",
                "patch",
                action="write_ops_file",
                args={
                    "path": str(target),
                    "mode": "insert_after",
                    "anchor": "mysql_ip = 'x'",
                    "content": "mysql_port = 3307",
                    "expected_matches": "1",
                },
            ),
        )

    assert result.status == "blocked"
    assert "anchor_match_count=2 expected=1" in result.output
