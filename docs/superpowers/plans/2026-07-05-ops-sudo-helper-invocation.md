# Ops Sudo Helper Invocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every helper-backed real Ops recipe through non-interactive sudo while leaving dry-run commands unprivileged.

**Architecture:** Add one `ControlledRecipeRunner._helper_command()` boundary that builds the helper invocation and conditionally prefixes `sudo -n` only for real execution. Replace all helper command construction with this boundary, preserve local non-helper recipes, and verify exact command arrays without running live server operations.

**Tech Stack:** Python 3.8, existing Ops recipe runner, pytest.

## Global Constraints

- Never request, accept, store, trace, or pipe a sudo password.
- Real helper commands must begin with `sudo -n /usr/local/bin/klonet-agent-op`.
- Dry-run helper commands must begin directly with `/usr/local/bin/klonet-agent-op` and contain no sudo prefix.
- Preserve OperationPlan and per-step confirmation checks.
- Do not add systemd deployment or run live `--execute` operations.
- Preserve unrelated existing worktree changes.

---

### Task 1: Add a failing privilege-boundary regression test

**Files:**
- Modify: `tests/test_ops_operations.py`

**Interfaces:**
- Consumes: `ControlledRecipeRunner(plan, step)` and existing `OperationPlan`/`OperationStep` data classes.
- Produces: a behavior test covering restart, stop, platform stop/start, and Nginx helper commands in dry-run and real modes.

- [ ] **Step 1: Add the failing test**

Append this test near the existing helper command tests:

```python
def test_helper_backed_recipes_use_sudo_only_for_real_execution():
    from klonet_agent.ops.operations import OperationPlan, OperationStep
    from klonet_agent.ops.recipes import ControlledRecipeRunner

    plan = OperationPlan(
        plan_id="plan-sudo-boundary",
        operation="restart_platform",
        target="102",
        objective="verify helper privilege boundary",
    )
    cases = [
        (
            "restart_screen_component",
            {
                "platform": "102",
                "component": "master",
                "screen_session": "102_m",
                "project_root": "/home/adminis/lht/102_project",
            },
            "restart-screen-component",
        ),
        (
            "stop_screen_component",
            {
                "platform": "102",
                "component": "master",
                "screen_session": "102_m",
            },
            "stop-screen-component",
        ),
        ("stop_platform_screens", {"platform": "102"}, "stop-platform-screens"),
        (
            "start_platform_screens",
            {
                "platform": "102",
                "project_root": "/home/adminis/lht/102_project",
            },
            "start-platform-screens",
        ),
        ("reload_nginx", {}, "reload-nginx"),
    ]

    for recipe_id, recipe_args, action in cases:
        calls = []
        step = OperationStep(
            step_id=f"test-{recipe_id}",
            title="test helper command",
            purpose="verify sudo boundary",
            recipe_id=recipe_id,
            recipe_args=recipe_args,
        )
        real_result = ControlledRecipeRunner(
            dry_run=False,
            command_runner=lambda command: calls.append(command) or "helper ok",
        )(plan, step)

        assert real_result.status == "completed"
        assert calls[0][:4] == [
            "sudo",
            "-n",
            "/usr/local/bin/klonet-agent-op",
            action,
        ]
        assert "--execute" in calls[0]

        dry_result = ControlledRecipeRunner(dry_run=True)(plan, step)
        assert dry_result.status == "completed"
        assert "command_preview=/usr/local/bin/klonet-agent-op" in dry_result.output
        assert "command_preview=sudo " not in dry_result.output
        assert "--dry-run" in dry_result.output
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_ops_operations.py::test_helper_backed_recipes_use_sudo_only_for_real_execution -q
```

Expected: FAIL because current real restart/stop/start/Nginx commands start directly with the helper path instead of `sudo -n`.

### Task 2: Centralize helper command construction

**Files:**
- Modify: `ops/recipes.py`
- Modify: `tests/test_ops_operations.py`

**Interfaces:**
- Produces: `ControlledRecipeRunner._helper_command(action: str, *args: str) -> list`.
- Consumes: `self.dry_run`, `self.helper_path`, action name, and validated string arguments.

- [ ] **Step 1: Add the minimal command constructor**

Add this method after `ControlledRecipeRunner.__init__`:

```python
    def _helper_command(self, action: str, *args: str) -> list:
        command = [
            self.helper_path,
            action,
            "--dry-run" if self.dry_run else "--execute",
            *args,
        ]
        if self.dry_run:
            return command
        return ["sudo", "-n", *command]
```

- [ ] **Step 2: Route all helper-backed recipes through the constructor**

Replace hand-built helper arrays for restart, stop, stop-platform, start-platform, reload-Nginx, root archive extraction, and root install-script execution. For example:

```python
command = self._helper_command(
    "restart-screen-component",
    "--platform",
    platform,
    "--component",
    component,
    "--screen",
    screen_session,
    "--project-root",
    project_root,
)
```

Use the same shape for every covered action. Do not change local archive extraction, local install scripts, or file-writing recipes.

- [ ] **Step 3: Update outdated exact-command expectations**

Update existing real-execution assertions for restart and Nginx so their expected arrays begin with:

```python
["sudo", "-n", "/usr/local/bin/klonet-agent-op", ...]
```

Keep existing root archive and root install-script expectations unchanged because they already assert that prefix.

- [ ] **Step 4: Run the focused test and verify GREEN**

```bash
.venv/bin/python -m pytest \
  tests/test_ops_operations.py::test_helper_backed_recipes_use_sudo_only_for_real_execution \
  tests/test_ops_operations.py::test_restart_screen_component_recipe_execute_calls_fixed_helper_command \
  tests/test_ops_operations.py::test_reload_nginx_recipe_execute_tests_config_before_reload \
  tests/test_ops_operations.py::test_extract_archive_recipe_execute_uses_sudo_helper_for_root_destination \
  tests/test_ops_operations.py::test_run_install_script_recipe_execute_uses_sudo_helper_for_root_script_dir \
  -q
```

Expected: 5 tests pass.

### Task 3: Document and verify the security contract

**Files:**
- Modify: `docs/ops/klonet-agent-op-install.md`
- Test: `tests/test_ops_operations.py`
- Test: `tests/test_ops_helper_install_contract.py`
- Test: `tests/test_ops_helper_script.py`

**Interfaces:**
- Consumes: the completed centralized command boundary.
- Produces: an installation document that matches actual `sudo -n` behavior.

- [ ] **Step 1: Update the installation contract**

Add a short section explaining:

```text
Dry-run executes the helper directly as the Agent user. Real execution always
uses `sudo -n /usr/local/bin/klonet-agent-op ... --execute`; `-n` forbids a
password prompt and causes immediate failure if sudoers is missing. Only the
dedicated `klonet-agent` service account should belong to `klonet-ops`.
```

- [ ] **Step 2: Run focused Ops and security suites**

```bash
.venv/bin/python -m pytest \
  tests/test_ops_operations.py \
  tests/test_ops_helper_install_contract.py \
  tests/test_ops_helper_script.py \
  -q
```

Expected: all selected tests pass; no test executes live server changes.

- [ ] **Step 3: Run the complete test suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: complete suite passes, or only previously documented unrelated baseline failures remain with no new Ops failures.

- [ ] **Step 4: Inspect the final diff and commit only task files**

```bash
git diff --check
git diff -- ops/recipes.py tests/test_ops_operations.py docs/ops/klonet-agent-op-install.md
git add ops/recipes.py tests/test_ops_operations.py docs/ops/klonet-agent-op-install.md
git commit -m "fix: route ops helper execution through sudo"
```

Do not stage the pre-existing `README.md`, test reports, or `memory/shared/` changes.
