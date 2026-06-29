# Virtual Environment Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recreate the current `agent/` Python 3.8 environment as `.venv/` with exactly matching installed package versions, then remove the old environment only after verification.

**Architecture:** Treat `agent/` as the rollback environment while building `.venv/` from an exact temporary package snapshot. Compare normalized old and new package sets, exercise the CLI with the new interpreter, and make deletion of `agent/` the final gated action.

**Tech Stack:** Python 3.8 `venv`, pip 25.0.1, GNU core utilities, pytest.

## Global Constraints

- Preserve Python 3.8.0 and every package version reported by `pip freeze --all`.
- Keep `agent/` untouched until `.venv/` passes every required check.
- Store exact-version snapshots only under `/tmp`, not in Git.
- Do not modify application source, `requirements.txt`, or `.gitignore`.
- Stop without deleting `agent/` if any creation, installation, comparison, or verification step fails.

---

### Task 1: Snapshot the old environment and create `.venv`

**Files:**
- Create temporarily: `/tmp/klonet_agent_venv_old.txt`
- Create ignored runtime directory: `.venv/`

**Interfaces:**
- Consumes: `agent/bin/python`, `/usr/local/python3/bin/python3.8`.
- Produces: a sorted exact-version snapshot and a fresh `.venv/bin/python`.

- [ ] **Step 1: Export and validate the old package snapshot**

```bash
agent/bin/python -m pip freeze --all | LC_ALL=C sort > /tmp/klonet_agent_venv_old.txt
test -s /tmp/klonet_agent_venv_old.txt
wc -l /tmp/klonet_agent_venv_old.txt
```

Expected: the file is non-empty and contains the complete old package list.

- [ ] **Step 2: Confirm `.venv` is absent and ignored**

```bash
test ! -e .venv
git check-ignore -q .venv
```

Expected: both commands exit 0.

- [ ] **Step 3: Create the new environment with the same base interpreter**

```bash
/usr/local/python3/bin/python3.8 -m venv .venv
.venv/bin/python --version
readlink -f .venv/bin/python
```

Expected: Python 3.8.0 and `/usr/local/python3/bin/python3.8`.

### Task 2: Install and compare exact package versions

**Files:**
- Read: `/tmp/klonet_agent_venv_old.txt`
- Create temporarily: `/tmp/klonet_agent_venv_new.txt`
- Modify ignored runtime directory: `.venv/`

**Interfaces:**
- Consumes: the exact old snapshot and `.venv/bin/python`.
- Produces: an installed `.venv` and a normalized comparison snapshot.

- [ ] **Step 1: Install every pinned distribution**

```bash
.venv/bin/python -m pip install -r /tmp/klonet_agent_venv_old.txt
```

Expected: pip exits 0 with every requested pinned version installed.

- [ ] **Step 2: Export and compare the new package snapshot**

```bash
.venv/bin/python -m pip freeze --all | LC_ALL=C sort > /tmp/klonet_agent_venv_new.txt
diff -u /tmp/klonet_agent_venv_old.txt /tmp/klonet_agent_venv_new.txt
```

Expected: `diff` exits 0 and prints no differences.

### Task 3: Verify the recreated environment

**Files:**
- Read: project source and `tests/test_cli_entry.py`.
- Use: ignored runtime directory `.venv/`.

**Interfaces:**
- Consumes: verified package-equivalent `.venv`.
- Produces: evidence that the project starts and the CLI regression suite passes.

- [ ] **Step 1: Verify the project CLI starts**

```bash
.venv/bin/python -m klonet_agent.agent --help
```

Expected: exit 0 and help text containing `--mode`, `--user-id`, and `--project-id`.

- [ ] **Step 2: Run focused CLI tests**

```bash
.venv/bin/python -m pytest tests/test_cli_entry.py -q
```

Expected: 14 tests pass with no failures.

- [ ] **Step 3: Confirm `.venv` remains ignored**

```bash
git check-ignore -v .venv/bin/python
git status --short
```

Expected: `.gitignore` identifies `.venv/`; Git does not list `.venv/`.

### Task 4: Remove the old environment after the verification gate

**Files:**
- Delete: `agent/`
- Keep: `.venv/`

**Interfaces:**
- Consumes: successful package comparison, CLI startup, and CLI test evidence from Tasks 2 and 3.
- Produces: one working ignored environment at `.venv/`.

- [ ] **Step 1: Check that no Python process is using the old environment**

```bash
pgrep -af '/home/adminis/lht/agent/klonet_agent/agent/bin/python' || true
```

Expected: no active project Python process uses the old interpreter.

- [ ] **Step 2: Delete only the old virtual environment**

```bash
rm -rf agent
test ! -e agent
```

Expected: `agent/` no longer exists and `.venv/` remains intact.

- [ ] **Step 3: Re-run the final checks from `.venv`**

```bash
.venv/bin/python --version
.venv/bin/python -m pytest tests/test_cli_entry.py -q
git status --short
```

Expected: Python 3.8.0, 14 CLI tests pass, and neither virtual environment appears in Git status.
