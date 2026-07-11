from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER = PROJECT_ROOT / "scripts" / "klonet-agent-op"


def test_run_ops_command_action_is_registered():
    from klonet_agent.ops.actions import DEFAULT_OPS_ACTION_REGISTRY

    spec = DEFAULT_OPS_ACTION_REGISTRY.get("run_ops_command")

    assert spec is not None
    assert spec.handler_name == "_run_ops_command"


def test_command_policy_classifies_make_as_controlled_without_step_confirm():
    from klonet_agent.ops.command_policy import decide_ops_command

    decision = decide_ops_command(
        {"program": "make", "argv": ["all"], "cwd": "/home/klonet-agent/workspaces/demo"}
    )

    assert decision.allowed
    assert decision.category == "workspace_build"
    assert decision.risk == "controlled"
    assert decision.requires_sudo is False
    assert decision.requires_step_confirmation is False


def test_command_policy_classifies_apt_install_as_step_confirmed_sudo():
    from klonet_agent.ops.command_policy import decide_ops_command

    decision = decide_ops_command(
        {"program": "apt", "argv": ["install", "-y", "build-essential"], "cwd": ""}
    )
    reinstall = decide_ops_command(
        {"program": "apt", "argv": ["install", "--reinstall", "-y", "python3.8-minimal"], "cwd": ""}
    )

    assert decision.allowed
    assert decision.category == "system_package_install"
    assert decision.risk == "dangerous"
    assert decision.requires_sudo is True
    assert decision.requires_step_confirmation is True
    assert reinstall.allowed
    assert reinstall.risk == "dangerous"
    assert reinstall.requires_sudo is True
    assert reinstall.requires_step_confirmation is True


def test_command_policy_allows_controlled_python_package_install():
    from klonet_agent.ops.command_policy import decide_ops_command

    python = decide_ops_command(
        {
            "program": "python3.8",
            "argv": [
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "gunicorn",
                "celery",
                "gevent-websocket",
            ],
            "cwd": "/home/klonet-agent/platforms/lht_project",
        }
    )
    pip = decide_ops_command(
        {
            "program": "pip3.8",
            "argv": ["install", "--no-cache-dir", "gunicorn"],
            "cwd": "/home/klonet-agent/platforms/lht_project",
        }
    )

    assert python.allowed
    assert python.category == "python_package_install"
    assert python.requires_step_confirmation is True
    assert python.requires_sudo is False
    assert pip.allowed
    assert pip.category == "python_package_install"


def test_command_policy_rejects_uncontrolled_python_package_install_forms():
    from klonet_agent.ops.command_policy import decide_ops_command

    requirements = decide_ops_command(
        {
            "program": "python3.8",
            "argv": ["-m", "pip", "install", "-r", "requirements.txt"],
            "cwd": "/home/klonet-agent/platforms/lht_project",
        }
    )
    url = decide_ops_command(
        {
            "program": "pip3.8",
            "argv": ["install", "https://example.com/pkg.whl"],
            "cwd": "/home/klonet-agent/platforms/lht_project",
        }
    )
    code = decide_ops_command(
        {
            "program": "python3.8",
            "argv": ["-c", "print(1)"],
            "cwd": "/home/klonet-agent/platforms/lht_project",
        }
    )

    assert not requirements.allowed
    assert requirements.reason == "pip_requirements_file_not_allowed"
    assert not url.allowed
    assert url.reason == "pip_package_not_allowed"
    assert not code.allowed
    assert code.reason == "python_args_not_allowed"


def test_command_policy_allows_selected_git_workflows():
    from klonet_agent.ops.command_policy import decide_ops_command

    cwd = "/home/klonet-agent/workspaces/demo"

    clone = decide_ops_command(
        {"program": "git", "argv": ["clone", "https://github.com/org/repo.git", "repo"], "cwd": cwd}
    )
    pull = decide_ops_command(
        {"program": "git", "argv": ["pull", "origin", "main"], "cwd": cwd}
    )
    checkout = decide_ops_command(
        {"program": "git", "argv": ["checkout", "feature/theaterq"], "cwd": cwd}
    )
    push = decide_ops_command(
        {"program": "git", "argv": ["push", "origin", "main"], "cwd": cwd}
    )

    assert clone.allowed and clone.category == "git_clone"
    assert pull.allowed and pull.category == "git_pull"
    assert checkout.allowed and checkout.category == "git_checkout"
    assert push.allowed and push.category == "git_push"
    assert push.requires_step_confirmation is True
    assert push.risk == "dangerous"


def test_command_policy_allows_workspace_directory_creation(tmp_path):
    from klonet_agent.ops.command_policy import decide_ops_command

    mkdir_decision = decide_ops_command(
        {
            "program": "mkdir",
            "argv": ["-p", str(tmp_path / "platforms" / "lht_project")],
            "cwd": str(tmp_path),
        }
    )
    install_decision = decide_ops_command(
        {
            "program": "install",
            "argv": ["-d", str(tmp_path / "platforms" / "lht_project" / "vemu_frontend")],
            "cwd": str(tmp_path),
        }
    )
    unsafe = decide_ops_command(
        {"program": "mkdir", "argv": ["-p", "/etc/klonet-test"], "cwd": "/"}
    )

    assert mkdir_decision.allowed
    assert mkdir_decision.category == "workspace_directory_create"
    assert mkdir_decision.requires_sudo is False
    assert install_decision.allowed
    assert install_decision.category == "workspace_directory_create"
    assert install_decision.requires_sudo is False
    assert not unsafe.allowed
    assert unsafe.reason == "destination_not_allowlisted"


def test_command_policy_allows_workspace_multi_copy_and_symlink(tmp_path):
    from klonet_agent.ops.command_policy import decide_ops_command

    work = tmp_path / "lht_project"
    mains = work / "vemu_uestc" / "mains"
    mains.mkdir(parents=True)
    copy_decision = decide_ops_command(
        {
            "program": "cp",
            "argv": ["vemu_uestc/mains/gun.py", "vemu_uestc/mains/master_main.py", "."],
            "cwd": str(work),
        }
    )
    symlink_decision = decide_ops_command(
        {"program": "ln", "argv": ["-s", ".", "vemu_uestc"], "cwd": str(work)}
    )
    unsafe_symlink = decide_ops_command(
        {"program": "ln", "argv": ["-s", "/etc", "vemu_uestc"], "cwd": str(work)}
    )

    assert copy_decision.allowed
    assert copy_decision.category == "workspace_file_copy"
    assert copy_decision.requires_sudo is False
    assert symlink_decision.allowed
    assert symlink_decision.category == "workspace_symlink_create"
    assert not unsafe_symlink.allowed
    assert unsafe_symlink.reason == "ln_path_not_allowlisted"


def test_command_policy_allows_git_clone_dot_with_cwd_and_reports_missing_cwd():
    from klonet_agent.ops.command_policy import decide_ops_command

    allowed = decide_ops_command(
        {
            "program": "git",
            "argv": ["clone", "gitee:uestc-minenet/vemu_uestc.git", "."],
            "cwd": "/home/klonet-agent/platforms/lht_project",
        }
    )
    missing_cwd = decide_ops_command(
        {
            "program": "git",
            "argv": ["clone", "gitee:uestc-minenet/vemu_uestc.git", "/home/klonet-agent/platforms/lht_project"],
        }
    )

    assert allowed.allowed
    assert allowed.category == "git_clone"
    assert not missing_cwd.allowed
    assert missing_cwd.reason == "git_clone_requires_cwd"


def test_command_policy_accepts_legacy_string_argv_and_gitee_alias_url():
    from klonet_agent.ops.command_policy import decide_ops_command

    decision = decide_ops_command(
        {
            "program": "git",
            "argv": "['clone', 'gitee:uestc-minenet/vemu_uestc.git', '/home/klonet-agent/102']",
            "cwd": "/home/klonet-agent",
        }
    )

    assert decision.allowed
    assert decision.argv == (
        "clone",
        "gitee:uestc-minenet/vemu_uestc.git",
        "/home/klonet-agent/102",
    )
    assert decision.category == "git_clone"


def test_command_policy_rejects_unapproved_git_commands():
    from klonet_agent.ops.command_policy import decide_ops_command

    reset = decide_ops_command(
        {"program": "git", "argv": ["reset", "--hard"], "cwd": "/home/klonet-agent/workspaces/demo"}
    )
    unsafe_clone = decide_ops_command(
        {"program": "git", "argv": ["clone", "https://github.com/org/repo.git", "/etc/repo"], "cwd": "/home/klonet-agent/workspaces/demo"}
    )

    assert not reset.allowed
    assert reset.reason == "git_args_not_allowed"
    assert not unsafe_clone.allowed
    assert unsafe_clone.reason == "git_destination_not_within_cwd"


def test_operation_plan_preserves_run_ops_command_argv_and_sets_risk(tmp_path):
    from klonet_agent.ops.operations import OperationPlanStore

    plan = OperationPlanStore(tmp_path).create_plan(
        operation="deploy_platform",
        target="demo",
        steps=[
            {
                "step_id": "apt-update",
                "title": "刷新 apt 索引",
                "action": "run_ops_command",
                "args": {"program": "apt", "argv": ["update"], "cwd": ""},
            }
        ],
    )

    step = plan.steps[0]
    assert step.args["argv"] == ["update"]
    assert step.risk == "dangerous"
    assert step.requires_step_confirmation is True


def test_operation_plan_forces_apt_reinstall_step_confirmation(tmp_path):
    from klonet_agent.ops.operations import OperationPlanStore

    plan = OperationPlanStore(tmp_path).create_plan(
        operation="deploy_platform",
        target="demo",
        steps=[
            {
                "step_id": "reinstall-python",
                "title": "恢复 python",
                "risk": "controlled",
                "requires_step_confirmation": False,
                "action": "run_ops_command",
                "args": {
                    "program": "apt",
                    "argv": ["install", "--reinstall", "-y", "python3.8-minimal"],
                    "cwd": "",
                },
            }
        ],
    )

    step = plan.steps[0]
    assert step.risk == "dangerous"
    assert step.requires_step_confirmation is True
    assert step.permission == "step_confirm_required"


def test_operation_plan_rejects_disallowed_run_ops_command(tmp_path):
    from klonet_agent.ops.operations import OperationPlanStore

    try:
        OperationPlanStore(tmp_path).create_plan(
            operation="deploy_platform",
            target="demo",
            steps=[
                {
                    "step_id": "unsafe-apt",
                    "title": "unsafe apt",
                    "action": "run_ops_command",
                    "args": {
                        "program": "apt",
                        "argv": ["install", "--allow-unauthenticated", "-y", "python3"],
                    },
                }
            ],
        )
    except ValueError as exc:
        assert "apt_install_option_not_allowed" in str(exc)
    else:
        raise AssertionError("expected disallowed run_ops_command to be rejected")


def test_run_ops_command_make_executes_after_plan_confirm(tmp_path):
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledActionRunner

    calls = []

    def runner(command):
        calls.append(command)
        return "built"

    work = tmp_path / "work"
    work.mkdir()
    store = OperationPlanStore(
        tmp_path / "plans",
        action_runner=ControlledActionRunner(dry_run=False, command_runner=runner),
    )
    plan = store.create_plan(
        operation="deploy_platform",
        target="demo",
        steps=[
            {
                "step_id": "build",
                "title": "编译",
                "action": "run_ops_command",
                "args": {"program": "make", "argv": ["all"], "cwd": str(work)},
            }
        ],
    )
    store.approve_plan(plan.plan_id)
    result = store.execute_step(plan.plan_id, "build")

    assert calls == [["make", "all"]]
    assert "category=workspace_build" in result
    assert "command_output=built" in result


def test_run_ops_command_apt_requires_confirm_step(tmp_path):
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledActionRunner

    store = OperationPlanStore(
        tmp_path / "plans",
        action_runner=ControlledActionRunner(dry_run=False, command_runner=lambda command: "ok"),
    )
    plan = store.create_plan(
        operation="deploy_platform",
        target="demo",
        steps=[
            {
                "step_id": "apt-update",
                "title": "刷新 apt",
                "action": "run_ops_command",
                "args": {"program": "apt", "argv": ["update"], "cwd": ""},
            }
        ],
    )
    store.approve_plan(plan.plan_id)
    result = store.execute_step(plan.plan_id, "apt-update")

    assert "requires explicit confirm-step" in result
    assert f"confirm-step {plan.plan_id} apt-update" in result


def test_run_ops_command_git_push_requires_confirm_step(tmp_path):
    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledActionRunner

    repo = tmp_path / "repo"
    repo.mkdir()
    store = OperationPlanStore(
        tmp_path / "plans",
        action_runner=ControlledActionRunner(dry_run=False, command_runner=lambda command: "ok"),
    )
    plan = store.create_plan(
        operation="deploy_platform",
        target="demo",
        steps=[
            {
                "step_id": "push",
                "title": "上传分支",
                "action": "run_ops_command",
                "args": {"program": "git", "argv": ["push", "origin", "main"], "cwd": str(repo)},
            }
        ],
    )
    store.approve_plan(plan.plan_id)
    result = store.execute_step(plan.plan_id, "push")

    assert "requires explicit confirm-step" in result
    assert f"confirm-step {plan.plan_id} push" in result


def test_run_ops_command_git_uses_noninteractive_env_and_timeout(tmp_path, monkeypatch):
    import subprocess

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledActionRunner

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")

    monkeypatch.setattr("klonet_agent.ops.recipes.subprocess.run", fake_run)
    work = tmp_path / "repo"
    work.mkdir()
    store = OperationPlanStore(
        tmp_path / "plans",
        action_runner=ControlledActionRunner(dry_run=False),
    )
    plan = store.create_plan(
        operation="deploy_platform",
        target="demo",
        steps=[
            {
                "step_id": "clone",
                "title": "clone",
                "action": "run_ops_command",
                "args": {
                    "program": "git",
                    "argv": ["clone", "https://github.com/org/repo.git", "."],
                    "cwd": str(work),
                },
            }
        ],
    )
    store.approve_plan(plan.plan_id)
    result = store.execute_step(plan.plan_id, "clone")

    assert "command_output=cloned" in result
    assert calls
    _command, kwargs = calls[0]
    assert kwargs["timeout"] == 120
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in kwargs["env"]["GIT_SSH_COMMAND"]


def test_run_ops_command_timeout_blocks_instead_of_hanging(tmp_path, monkeypatch):
    import subprocess

    from klonet_agent.ops.operations import OperationPlanStore
    from klonet_agent.ops.recipes import ControlledActionRunner

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("klonet_agent.ops.recipes.subprocess.run", fake_run)
    work = tmp_path / "repo"
    work.mkdir()
    store = OperationPlanStore(
        tmp_path / "plans",
        action_runner=ControlledActionRunner(dry_run=False),
    )
    plan = store.create_plan(
        operation="deploy_platform",
        target="demo",
        steps=[
            {
                "step_id": "clone",
                "title": "clone",
                "action": "run_ops_command",
                "args": {
                    "program": "git",
                    "argv": ["clone", "https://github.com/org/repo.git", "."],
                    "cwd": str(work),
                },
            }
        ],
    )
    store.approve_plan(plan.plan_id)
    result = store.execute_step(plan.plan_id, "clone")

    assert "result_status=blocked" in result
    assert "command_timed_out" in result
    assert "next_required_action=inspect_runtime" in result


def test_helper_run_ops_command_dry_run_contract(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "run-ops-command",
            "--dry-run",
            "--program",
            "apt",
            "--argv-json",
            '["update"]',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "action=run-ops-command" in result.stdout
    assert "program=apt" in result.stdout
    assert "environment_changed=false" in result.stdout


def test_helper_run_ops_command_allows_apt_reinstall_dry_run():
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "run-ops-command",
            "--dry-run",
            "--program",
            "apt",
            "--argv-json",
            '["install","--reinstall","-y","python3.8-minimal"]',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "program=apt" in result.stdout
    assert "python3.8-minimal" in result.stdout
    assert "environment_changed=false" in result.stdout
