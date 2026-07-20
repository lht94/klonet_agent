# Klonet Agent Manual Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the existing installer, stop it from enabling or starting the systemd service, and preserve the server-specific Python path supplied through `--python` so the selected environment can import `rank_bm25`.

**Architecture:** The installer remains responsible for the dedicated account, SSH login profile, environment file, Ops helper, sudoers, runtime permissions, and compatible unit rendering. Service lifecycle management is removed. The caller must supply an executable Python path; the installer converts only its directory to an absolute path, preserves the final executable name/symlink, and performs a dependency preflight before any account or filesystem mutation.

**Tech Stack:** Bash, systemd unit templates, Python 3.8+, pytest

---

## File map

- Modify `scripts/install-klonet-agent-service.sh`: remove `--start` and service enable/restart behavior; preserve the selected Python launcher and validate `rank_bm25`.
- Modify `tests/test_klonet_agent_service_installer.py`: add regression coverage for manual lifecycle, Python-path preservation, and dependency preflight.
- Modify `docs/ops/klonet-agent-op-install.md`: document manual login/run behavior, explicit Python selection, dependency repair, and migration from an enabled unit.
- Modify `README.md`: align the quick-start instructions with manual execution and explicit Python selection.
- Modify `tests/test_ops_helper_install_contract.py`: enforce the new documentation contract.

### Task 1: Remove automatic service lifecycle management

**Files:**
- Modify: `tests/test_klonet_agent_service_installer.py`
- Modify: `scripts/install-klonet-agent-service.sh`

- [ ] **Step 1: Replace the service-start test with failing manual-lifecycle tests**

Replace `test_installer_requires_explicit_start` with:

```python
def test_installer_does_not_enable_or_start_service(tmp_path):
    result, calls, _ = _run_installer(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "systemctl daemon-reload" in calls
    assert "systemctl enable" not in calls
    assert "systemctl restart" not in calls


def test_installer_rejects_removed_start_option(tmp_path):
    result, calls, _ = _run_installer(tmp_path, "--start")

    assert result.returncode != 0
    assert "unknown argument: --start" in result.stderr
    assert "systemctl enable" not in calls
    assert "systemctl restart" not in calls
```

- [ ] **Step 2: Run the focused tests and verify the old behavior fails**

Run:

```bash
pytest -q tests/test_klonet_agent_service_installer.py \
  -k 'does_not_enable_or_start_service or rejects_removed_start_option'
```

Expected: both tests fail because the current installer calls `systemctl enable`, and `--start` is still accepted.

- [ ] **Step 3: Remove lifecycle flags and calls from the installer**

In `scripts/install-klonet-agent-service.sh`:

1. Remove `start_service=0`.
2. Remove the usage line for `--start`.
3. Remove the parser branch:

```bash
--start) start_service=1; shift ;;
```

4. Replace the lifecycle block:

```bash
systemctl daemon-reload
systemctl enable "${service_name}.service"
if ((start_service)); then
  systemctl restart "${service_name}.service"
fi
```

with:

```bash
systemctl daemon-reload
```

5. Replace the final start instructions with:

```bash
printf 'The Agent is configured for manual execution after login.\n'
printf 'Run: python -m klonet_agent.agent --mode %s --user-id %s --project-id %s\n' \
  "$mode" "$user_id" "$project_id"
```

- [ ] **Step 4: Run the installer tests**

Run:

```bash
pytest -q tests/test_klonet_agent_service_installer.py
```

Expected: all tests in the file pass except any new Python preflight tests introduced in Task 2, which have not yet been added.

- [ ] **Step 5: Commit the lifecycle change**

```bash
git add scripts/install-klonet-agent-service.sh tests/test_klonet_agent_service_installer.py
git commit -m "fix: keep klonet agent manually managed"
```

### Task 2: Preserve the selected Python environment and preflight BM25

**Files:**
- Modify: `tests/test_klonet_agent_service_installer.py`
- Modify: `scripts/install-klonet-agent-service.sh`

- [ ] **Step 1: Extend the installer test helper with a controllable Python launcher**

Change `_run_installer` to accept a keyword-only dependency result and create a test launcher under a venv-shaped path:

```python
def _run_installer(
    tmp_path: Path,
    *extra_args: str,
    rank_bm25_available: bool = True,
):
    install_root = tmp_path / "root"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")

    python_path = tmp_path / "selected-venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    import_status = 0 if rank_bm25_available else 1
    python_path.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"-c\" "
        "&& \"${2:-}\" == \"import rank_bm25\" ]]; then\n"
        f"  exit {import_status}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    python_path.chmod(0o755)

    _write_fake_command(
        bin_dir,
        "id",
        'if [[ "${1:-}" == "-u" ]]; then echo 0; exit 0; fi; exit 1',
    )
    _write_fake_command(bin_dir, "getent", "exit 2")
    for name in (
        "groupadd", "useradd", "usermod", "visudo", "systemctl", "sudo",
        "passwd", "chgrp", "chmod", "find", "chown",
    ):
        _write_fake_command(bin_dir, name)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "KLONET_INSTALL_ROOT": str(install_root),
            "KLONET_TEST_CALLS": str(calls),
        }
    )
    command = [
        "bash",
        str(INSTALLER),
        "--project-root",
        str(PROJECT_ROOT),
        "--python",
        str(python_path),
        *extra_args,
    ]
    result = subprocess.run(command, text=True, capture_output=True, env=env)
    return result, calls.read_text(encoding="utf-8"), install_root
```

- [ ] **Step 2: Add failing path and dependency tests**

Add:

```python
def test_installer_preserves_selected_venv_python_path(tmp_path):
    result, _, install_root = _run_installer(
        tmp_path,
        "--enable-ssh-login",
    )

    assert result.returncode == 0, result.stderr
    installer_text = INSTALLER.read_text(encoding="utf-8")
    assert 'readlink -f "$python_path"' not in installer_text
    profile = (install_root / "etc/profile.d/klonet-agent.sh").read_text(
        encoding="utf-8"
    )
    assert str(tmp_path / "selected-venv" / "bin") in profile


def test_installer_fails_before_mutation_when_rank_bm25_is_missing(tmp_path):
    result, calls, install_root = _run_installer(
        tmp_path,
        rank_bm25_available=False,
    )

    assert result.returncode != 0
    assert "cannot import rank_bm25" in result.stderr
    assert "-m pip install -r" in result.stderr
    assert "groupadd" not in calls
    assert "useradd" not in calls
    assert "systemctl" not in calls
    assert not install_root.exists()
```

- [ ] **Step 3: Run the new tests and verify they fail**

Run:

```bash
pytest -q tests/test_klonet_agent_service_installer.py \
  -k 'preserves_selected_venv_python_path or fails_before_mutation_when_rank_bm25_is_missing'
```

Expected: the path test fails because `readlink -f` remains, and the missing-dependency test fails because installation currently proceeds.

- [ ] **Step 4: Implement safe path normalization and dependency preflight**

Replace:

```bash
python_path="$(readlink -f "$python_path")"
```

with:

```bash
python_dir="$(cd -- "$(dirname -- "$python_path")" && pwd -P)" \
  || die "Python directory does not exist: $(dirname -- "$python_path")"
python_path="$python_dir/$(basename -- "$python_path")"
```

Immediately after the executable check, add:

```bash
if ! "$python_path" -c 'import rank_bm25' >/dev/null 2>&1; then
  die "Python environment cannot import rank_bm25: $python_path. Install project dependencies with: $python_path -m pip install -r $project_root/requirements.txt"
fi
```

Keep `--python` required; do not add a default value or interpreter discovery.

- [ ] **Step 5: Run the complete installer test file**

Run:

```bash
pytest -q tests/test_klonet_agent_service_installer.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit the Python-path fix**

```bash
git add scripts/install-klonet-agent-service.sh tests/test_klonet_agent_service_installer.py
git commit -m "fix: preserve klonet agent virtualenv"
```

### Task 3: Align user documentation with manual execution

**Files:**
- Modify: `tests/test_ops_helper_install_contract.py`
- Modify: `docs/ops/klonet-agent-op-install.md`
- Modify: `README.md`

- [ ] **Step 1: Change the documentation contract test first**

Replace `test_install_doc_covers_dedicated_service_deployment` with:

```python
def test_install_doc_covers_manual_agent_execution():
    text = INSTALL_DOC.read_text(encoding="utf-8")

    assert "install-klonet-agent-service.sh" in text
    assert '--python "$PWD/.venv/bin/python"' in text
    assert "ssh klonet-agent@" in text
    assert "python -m klonet_agent.agent --mode ops" in text
    assert "systemctl disable --now klonet-agent.service" in text
    assert "systemctl start klonet-agent" not in text
    assert "systemctl restart klonet-agent" not in text
    assert "--start" not in text
    assert "/etc/klonet-agent/klonet-agent.env" in text
```

- [ ] **Step 2: Run the contract test and verify the old documentation fails**

Run:

```bash
pytest -q tests/test_ops_helper_install_contract.py \
  -k manual_agent_execution
```

Expected: FAIL because the current document still describes `--start` and systemd start/restart commands.

- [ ] **Step 3: Replace service lifecycle guidance in the Ops install document**

In `docs/ops/klonet-agent-op-install.md`:

- Change the heading `一键部署专用账户与服务` to `一键部署专用账户与运行环境`.
- Replace the bullet `安装并 enable klonet-agent.service` with `生成兼容的 unit 文件，但不 enable 或启动服务`.
- Delete the `--start` example and the `systemd 状态与日志` start/restart section.
- Insert this migration and usage block:

````markdown
### 手动运行与旧服务迁移

安装器不会 enable、start 或 restart `klonet-agent.service`。如果服务器曾使用旧版
安装器启用服务，管理员需要执行一次：

```bash
sudo systemctl disable --now klonet-agent.service
```

之后登录专用账户并手动运行：

```bash
ssh klonet-agent@SERVER_ADDRESS
python -m klonet_agent.agent --mode ops --user-id lht --project-id test
```

`--python` 没有默认值，必须指定当前服务器上已安装项目依赖的解释器。安装器会
验证该解释器可以导入 `rank_bm25`。如果验证失败，使用同一个解释器安装依赖：

```bash
/实际路径/.venv/bin/python -m pip install -r requirements.txt
```
````

- [ ] **Step 4: Update the README quick-start section**

Keep the existing explicit `--python "$PWD/.venv/bin/python"` example. Add a sentence that the option is mandatory and server-specific, remove any implication that the installer enables or starts systemd, and add:

```bash
# 旧版本曾启用服务时，只需迁移一次
sudo systemctl disable --now klonet-agent.service

# 登录后手动运行
ssh klonet-agent@服务器地址
python -m klonet_agent.agent --mode ops --user-id lht --project-id test
```

- [ ] **Step 5: Run documentation contract tests**

Run:

```bash
pytest -q tests/test_ops_helper_install_contract.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit documentation and its contract**

```bash
git add README.md docs/ops/klonet-agent-op-install.md tests/test_ops_helper_install_contract.py
git commit -m "docs: document manual klonet agent execution"
```

### Task 4: Run regression verification

**Files:**
- Verify only; no planned source changes.

- [ ] **Step 1: Run focused deployment tests**

```bash
pytest -q \
  tests/test_klonet_agent_service_installer.py \
  tests/test_ops_helper_install_contract.py
```

Expected: all tests pass with zero failures.

- [ ] **Step 2: Run the complete test suite**

```bash
pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Check formatting and scope**

```bash
git diff --check HEAD~3..HEAD
git status --short
```

Expected: `git diff --check` exits 0. `git status --short` contains only pre-existing user changes, including `doc/07_klonet_agent_knowledge_notes.md`, and no uncommitted files from this implementation.

- [ ] **Step 4: Inspect the final installer contract**

```bash
rg -n 'readlink -f|--start|systemctl (enable|restart)' \
  scripts/install-klonet-agent-service.sh
rg -n 'python_dir|import rank_bm25|manual execution' \
  scripts/install-klonet-agent-service.sh
```

Expected: the first command finds no matches. The second finds path normalization, dependency preflight, and the manual-execution completion message.
