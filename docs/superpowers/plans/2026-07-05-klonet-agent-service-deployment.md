# Klonet Agent Service Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and document an idempotent Ubuntu installer that runs Klonet Agent under the dedicated `klonet-agent` account with a constrained sudo helper and an optional systemd start.

**Architecture:** A checked-in systemd template describes the service boundary, while a Bash installer validates explicit paths, creates the fixed account/group, installs security-sensitive files with root ownership, renders the unit, and performs non-destructive verification. Pytest contract tests exercise the installer through a temporary fake command environment so tests never mutate the host.

**Tech Stack:** Bash, systemd unit files, sudoers/visudo, Python 3.8-compatible pytest.

## Global Constraints

- Account is fixed to `klonet-agent`; privileged group is fixed to `klonet-ops`.
- Helper path is `/usr/local/bin/klonet-agent-op`; sudoers path is `/etc/sudoers.d/klonet-agent-op`.
- The installer must not copy `.env`, secrets, tokens, passwords, or private keys.
- The installer must not set `KLONET_AGENT_OPS_REAL_EXECUTION=1`.
- The installer must never invoke a helper `--execute` operation.
- Existing environment-file content must survive repeat installations.
- Current CLI cannot remain interactive under `StandardInput=null`; installation enables the unit but starting is opt-in through `--start`.
- Tests must not modify real users, groups, `/etc`, sudoers, or systemd.

---

### Task 1: Systemd service contract

**Files:**
- Create: `scripts/klonet-agent.service.in`
- Create: `tests/test_klonet_agent_service_installer.py`

**Interfaces:**
- Consumes: template substitutions `@PYTHON@`, `@PACKAGE_PARENT@`, `@ENV_FILE@`, `@MODE@`, `@USER_ID@`, and `@PROJECT_ID@`.
- Produces: a unit template consumed by `scripts/install-klonet-agent-service.sh`.

- [ ] **Step 1: Write the failing template contract tests**

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "klonet-agent.service.in"


def test_service_template_runs_as_dedicated_account():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "User=klonet-agent" in text
    assert "Group=klonet-agent" in text
    assert "WorkingDirectory=@PACKAGE_PARENT@" in text
    assert "EnvironmentFile=-@ENV_FILE@" in text
    assert "ExecStart=@PYTHON@ -m klonet_agent.agent --mode @MODE@" in text
    assert "StandardInput=null" in text
    assert "Restart=on-failure" in text
```

- [ ] **Step 2: Run the test and verify the missing template fails**

Run: `python -m pytest tests/test_klonet_agent_service_installer.py -q`

Expected: FAIL with `FileNotFoundError` for `scripts/klonet-agent.service.in`.

- [ ] **Step 3: Add the minimal unit template**

```ini
[Unit]
Description=Klonet Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=klonet-agent
Group=klonet-agent
WorkingDirectory=@PACKAGE_PARENT@
EnvironmentFile=-@ENV_FILE@
ExecStart=@PYTHON@ -m klonet_agent.agent --mode @MODE@ --user-id @USER_ID@ --project-id @PROJECT_ID@
StandardInput=null
StandardOutput=journal
StandardError=journal
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Run the template test**

Run: `python -m pytest tests/test_klonet_agent_service_installer.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the template contract**

```bash
git add scripts/klonet-agent.service.in tests/test_klonet_agent_service_installer.py
git commit -m "test: define dedicated agent service contract"
```

### Task 2: Idempotent installer

**Files:**
- Create: `scripts/install-klonet-agent-service.sh`
- Modify: `tests/test_klonet_agent_service_installer.py`

**Interfaces:**
- Consumes: `--project-root`, `--python`, `--mode`, `--user-id`, `--project-id`, `--service-name`, `--env-file`, `--start`; optional test-only destination prefixes supplied through `KLONET_INSTALL_ROOT` and command lookup through `PATH`.
- Produces: installed helper, validated sudoers, environment file, rendered systemd unit, enabled service, and optional restart.

- [ ] **Step 1: Add failing installer contract and sandbox-execution tests**

```python
def test_installer_keeps_real_execution_disabled():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "KLONET_AGENT_OPS_REAL_EXECUTION=1" not in text
    assert "--execute" not in text
    assert "reload-nginx --dry-run" in text


def test_installer_requires_explicit_start(tmp_path):
    result, calls = run_installer(tmp_path)
    assert result.returncode == 0
    assert "systemctl enable klonet-agent.service" in calls
    assert "systemctl restart klonet-agent.service" not in calls

    result, calls = run_installer(tmp_path, "--start")
    assert result.returncode == 0
    assert "systemctl restart klonet-agent.service" in calls


def test_reinstall_preserves_environment_file(tmp_path):
    result, _ = run_installer(tmp_path)
    assert result.returncode == 0
    env_file = tmp_path / "etc/klonet-agent/klonet-agent.env"
    env_file.write_text("OPENAI_API_KEY=server-secret\n", encoding="utf-8")
    result, _ = run_installer(tmp_path)
    assert result.returncode == 0
    assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=server-secret\n"
```

The test helper creates fake `id`, `getent`, `groupadd`, `useradd`, `usermod`, `install`, `visudo`, `systemctl`, `sudo`, and `stat` commands under `tmp_path/bin`, prepends that directory to `PATH`, sets `KLONET_INSTALL_ROOT=tmp_path`, and records calls without changing the host.

- [ ] **Step 2: Run focused tests and verify installer failures**

Run: `python -m pytest tests/test_klonet_agent_service_installer.py -q`

Expected: FAIL because `scripts/install-klonet-agent-service.sh` does not exist.

- [ ] **Step 3: Implement strict argument parsing and validation**

Use `#!/usr/bin/env bash` and `set -Eeuo pipefail`. Require effective UID 0, validate the package root, Python executable, fixed source files, safe service name, and required system commands. Resolve the package parent with `dirname "$project_root"`. Default to no start; only `--start` sets `start_service=1`.

- [ ] **Step 4: Implement idempotent account and security-file installation**

Use these operations:

```bash
getent group klonet-ops >/dev/null || groupadd --system klonet-ops
if ! id klonet-agent >/dev/null 2>&1; then
  useradd --system --user-group --create-home \
    --home-dir /var/lib/klonet-agent --shell /usr/sbin/nologin klonet-agent
fi
usermod -aG klonet-ops klonet-agent
install -o root -g root -m 0755 "$project_root/scripts/klonet-agent-op" "$install_root/usr/local/bin/klonet-agent-op"
install -o root -g root -m 0440 "$project_root/scripts/klonet-agent-op.sudoers" "$sudoers_tmp"
visudo -cf "$sudoers_tmp"
mv "$sudoers_tmp" "$install_root/etc/sudoers.d/klonet-agent-op"
```

Before modifying an existing account, use `getent passwd klonet-agent` to require UID below the platform's regular-user threshold and shell `/usr/sbin/nologin` or `/bin/false`.

- [ ] **Step 5: Render the unit, preserve environment, enable, and optionally start**

Create `/etc/klonet-agent` as `root:klonet-agent 0750`. Create the environment file only when absent as `root:klonet-agent 0640`, containing comments but no values. Escape replacement values before applying template substitutions. Install the unit as `root:root 0644`, then call:

```bash
systemctl daemon-reload
systemctl enable "${service_name}.service"
if (( start_service )); then
  systemctl restart "${service_name}.service"
fi
```

Finish with sudoers validation, `sudo -l -U klonet-agent`, and `sudo -u klonet-agent /usr/local/bin/klonet-agent-op reload-nginx --dry-run` on real installations. Under `KLONET_INSTALL_ROOT`, use the prefixed helper path so tests remain isolated.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_klonet_agent_service_installer.py tests/test_ops_helper_install_contract.py tests/test_ops_helper_script.py -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit the installer**

```bash
git add scripts/install-klonet-agent-service.sh scripts/klonet-agent.service.in tests/test_klonet_agent_service_installer.py
git commit -m "feat: install agent service with dedicated account"
```

### Task 3: Deployment documentation and repository handoff

**Files:**
- Modify: `docs/ops/klonet-agent-op-install.md`
- Modify: `README.md`
- Test: `tests/test_ops_helper_install_contract.py`

**Interfaces:**
- Consumes: installer CLI and unit behavior from Tasks 1-2.
- Produces: copy-paste deployment, configuration, manual-run, service-run, update, diagnostics, and safety instructions.

- [ ] **Step 1: Add failing documentation assertions**

```python
def test_install_doc_covers_dedicated_service_deployment():
    text = INSTALL_DOC.read_text(encoding="utf-8")
    assert "install-klonet-agent-service.sh" in text
    assert "sudo -u klonet-agent" in text
    assert "systemctl start klonet-agent" in text
    assert "journalctl -u klonet-agent" in text
    assert "/etc/klonet-agent/klonet-agent.env" in text
    assert "--start" in text
```

- [ ] **Step 2: Run the documentation contract and verify failure**

Run: `python -m pytest tests/test_ops_helper_install_contract.py -q`

Expected: FAIL because the new deployment workflow is absent.

- [ ] **Step 3: Document installation and dedicated-account operation**

Add a concise quick-start such as:

```bash
sudo ./scripts/install-klonet-agent-service.sh \
  --project-root "$PWD" \
  --python "$PWD/.venv/bin/python" \
  --mode ops \
  --user-id lht \
  --project-id test1

sudoedit /etc/klonet-agent/klonet-agent.env
sudo systemctl start klonet-agent
sudo systemctl status klonet-agent
sudo journalctl -u klonet-agent -n 100 --no-pager
```

Explain that the current interactive CLI exits under systemd without an input/service transport, so manual execution remains:

```bash
cd /home/adminis/lht/agent
sudo -u klonet-agent --preserve-env=OPENAI_API_KEY \
  /home/adminis/lht/agent/klonet_agent/.venv/bin/python \
  -m klonet_agent.agent --mode ops --user-id lht --project-id test1
```

Prefer the root-owned environment file for systemd and warn against putting secrets in shell history or Git.

- [ ] **Step 4: Run focused and full tests**

Run: `python -m pytest tests/test_klonet_agent_service_installer.py tests/test_ops_helper_install_contract.py tests/test_ops_helper_script.py tests/test_ops_operations.py -q`

Expected: all focused tests PASS, except any previously documented unrelated platform-specific baseline failure must be reported explicitly rather than hidden.

Run: `python -m pytest -q --basetemp=/tmp/klonet_agent_pytest_tmp`

Expected: full suite PASS or only the already-known unrelated quote expectation failure; record exact output.

- [ ] **Step 5: Audit all pending files for secrets and accidental environment artifacts**

Run:

```bash
git status --short
git diff --check
git ls-files --others --exclude-standard
rg -l '(OPENAI_API_KEY|api[_-]?key|token|password|BEGIN .*PRIVATE KEY)' \
  README.md docs memory/shared scripts tests
```

Inspect matches without printing secret values. Do not add `.env`, virtual environments, private keys, or generated runtime state.

- [ ] **Step 6: Commit all safe pending project changes**

```bash
git add README.md docs memory/shared scripts tests
git status --short
git commit -m "docs: record ops deployment and regression evidence"
```

- [ ] **Step 7: Verify and push**

```bash
git status --short --branch
git log --oneline origin/master..HEAD
git push origin master
git status --short --branch
```

Expected: push succeeds and local `master` is aligned with `origin/master` with no safe pending project changes.
