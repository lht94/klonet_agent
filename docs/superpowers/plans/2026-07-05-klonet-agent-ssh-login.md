# Klonet Agent SSH Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing server installer with an opt-in password-based SSH account configuration so `klonet-agent` can log in and run every Agent mode through the familiar `python -m klonet_agent.agent` command.

**Architecture:** The installer keeps `nologin` as its default, but `--enable-ssh-login` selects `/bin/bash`, installs a root-owned login profile, and grants the service account group access to runtime directories. `--set-password` delegates credential entry exclusively to the system `passwd` program and never accepts password material itself.

**Tech Stack:** Bash, Linux account tools, `/etc/profile.d`, pytest with fake system commands.

## Global Constraints

- Password text must never appear in source, arguments, environment variables, fixtures, logs, or Git.
- `--set-password` is invalid without `--enable-ssh-login`.
- Default installations continue to create `/usr/sbin/nologin` accounts.
- SSH-enabled accounts use `/bin/bash`; existing enabled accounts are not downgraded on reinstall.
- The installer does not edit or reload `sshd` configuration.
- The existing root-owned helper and sudoers allowlist remain unchanged.
- Tests must not modify real accounts, passwords, `/etc`, runtime directory ownership, or SSH services.

---

### Task 1: SSH login installer behavior

**Files:**
- Create: `scripts/klonet-agent-login-profile.sh.in`
- Modify: `scripts/install-klonet-agent-service.sh`
- Modify: `tests/test_klonet_agent_service_installer.py`

**Interfaces:**
- Consumes: installer flags `--enable-ssh-login` and `--set-password`; substitutions `@VENV_BIN@`, `@ENV_FILE@`, and `@PACKAGE_PARENT@`.
- Produces: `/bin/bash` account shell, `/etc/profile.d/klonet-agent.sh`, runtime group permissions, and an optional interactive `passwd klonet-agent` call.

- [ ] **Step 1: Add failing flag-validation and profile contract tests**

```python
def test_set_password_requires_ssh_login(tmp_path):
    result, calls, _ = _run_installer(tmp_path, "--set-password")
    assert result.returncode != 0
    assert "--set-password requires --enable-ssh-login" in result.stderr
    assert "passwd klonet-agent" not in calls


def test_ssh_login_installs_profile_and_sets_password_interactively(tmp_path):
    result, calls, root = _run_installer(
        tmp_path, "--enable-ssh-login", "--set-password"
    )
    assert result.returncode == 0, result.stderr
    assert "useradd --system --user-group --create-home" in calls
    assert "--shell /bin/bash klonet-agent" in calls
    assert "passwd klonet-agent" in calls
    profile = (root / "etc/profile.d/klonet-agent.sh").read_text(encoding="utf-8")
    assert str(PROJECT_ROOT / ".venv/bin") in profile
    assert "/etc/klonet-agent/klonet-agent.env" in profile
    assert str(PROJECT_ROOT.parent) in profile


def test_installer_contains_no_password_input_option():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "--password " not in text
    assert "chpasswd" not in text
    assert "passwd \"$agent_user\"" in text
```

Extend the fake command list with `passwd`, `chgrp`, and `find`. Fake commands only record their names and arguments.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_klonet_agent_service_installer.py -q --basetemp=/tmp/klonet_agent_ssh_red`

Expected: new tests FAIL because the flags and profile do not exist.

- [ ] **Step 3: Add the login profile template**

```bash
if [[ "${USER:-}" == "klonet-agent" ]]; then
  export PATH="@VENV_BIN@:$PATH"
  if [[ -r "@ENV_FILE@" ]]; then
    set -a
    source "@ENV_FILE@"
    set +a
  fi
  cd "@PACKAGE_PARENT@"
fi
```

- [ ] **Step 4: Implement argument validation and account shell selection**

Parse both flags as booleans. Fail before mutations when password setup lacks SSH login. Require `/bin/bash` and `passwd` only for SSH mode. Set `account_shell=/bin/bash` for new SSH-enabled accounts and `/usr/sbin/nologin` otherwise. For an existing system account, allow `/usr/sbin/nologin`, `/bin/false`, or `/bin/bash`; run `usermod --shell /bin/bash klonet-agent` only when SSH login is requested.

- [ ] **Step 5: Render profile and grant runtime access**

Render the template with the existing safe `escape_sed` helper and install it as root-owned mode `0644`. For each of `memory`, `journals`, `workspaces`, and `tracing`, create the directory if absent, set group `klonet-agent`, add group read/write/execute as appropriate, and set setgid on directories. Under `KLONET_INSTALL_ROOT`, use fake `chgrp`, `chmod`, and `find` calls so tests cannot alter repository metadata.

- [ ] **Step 6: Invoke system password setup last**

After helper, sudoers, unit, profile, and verification finish:

```bash
if ((set_password)); then
  passwd "$agent_user"
fi
```

Print a reminder that account login also depends on server-side `sshd` password-authentication policy.

- [ ] **Step 7: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/test_klonet_agent_service_installer.py tests/test_ops_helper_install_contract.py tests/test_ops_helper_script.py -q --basetemp=/tmp/klonet_agent_ssh_green`

Expected: all tests PASS.

```bash
git add scripts/install-klonet-agent-service.sh scripts/klonet-agent-login-profile.sh.in tests/test_klonet_agent_service_installer.py
git commit -m "feat: support dedicated agent ssh login"
```

### Task 2: Deployment documentation and final verification

**Files:**
- Modify: `docs/ops/klonet-agent-op-install.md`
- Modify: `README.md`
- Modify: `tests/test_ops_helper_install_contract.py`

**Interfaces:**
- Consumes: the installer flags and login behavior from Task 1.
- Produces: copy-paste setup, SSH policy checks, login, all-mode startup, and password safety guidance.

- [ ] **Step 1: Add failing documentation assertions**

```python
def test_install_doc_covers_ssh_login_account():
    text = INSTALL_DOC.read_text(encoding="utf-8")
    assert "--enable-ssh-login" in text
    assert "--set-password" in text
    assert "ssh klonet-agent@" in text
    assert "python -m klonet_agent.agent --mode mentor" in text
    assert "python -m klonet_agent.agent --mode coding" in text
    assert "python -m klonet_agent.agent --mode ops" in text
    assert "sshd -T" in text
```

- [ ] **Step 2: Run the documentation test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_ops_helper_install_contract.py::test_install_doc_covers_ssh_login_account -q --basetemp=/tmp/klonet_agent_ssh_docs_red`

Expected: FAIL because SSH instructions are absent.

- [ ] **Step 3: Add deployment and usage instructions**

Document the combined installer command, the interactive `passwd` prompt, `sshd -T` verification, `ssh klonet-agent@SERVER_ADDRESS`, and all three mode commands. State that the installer does not modify sshd policy and never stores passwords. Replace the earlier recommendation that interactive use must start through `sudo -u` with the simpler dedicated SSH workflow while retaining it as a diagnostic fallback.

- [ ] **Step 4: Run focused and full verification**

Run: `.venv/bin/python -m pytest tests/test_klonet_agent_service_installer.py tests/test_ops_helper_install_contract.py tests/test_ops_helper_script.py tests/test_ops_operations.py -q --basetemp=/tmp/klonet_agent_ssh_focused`

Expected: all focused tests PASS.

Run: `.venv/bin/python -m pytest -q --basetemp=/tmp/klonet_agent_ssh_full`

Expected: all tests PASS.

- [ ] **Step 5: Audit secrets and commit**

Run:

```bash
git diff --check
git status --short
rg -l -i '(password\s*[:=]\s*[^[:space:]]+|BEGIN [A-Z ]*PRIVATE KEY)' scripts docs README.md tests
```

Inspect filenames without printing credential values. Then commit safe changes:

```bash
git add README.md docs/ops/klonet-agent-op-install.md tests/test_ops_helper_install_contract.py
git commit -m "docs: explain dedicated ssh agent workflow"
```

- [ ] **Step 6: Push and verify remote alignment**

```bash
git push origin master
git fetch origin master
git status --short --branch
git rev-parse HEAD
git rev-parse origin/master
```

Expected: local and remote hashes match and the worktree is clean.
