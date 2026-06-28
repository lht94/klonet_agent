# CLI Admin Data Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add CLI-only commands for reading and deleting local users, conversations, and projects.

**Architecture:** Add `app/admin.py` as the focused filesystem management layer, then extend `agent.py` with an `admin` subcommand group. Tests use temporary runtime roots and subprocess CLI checks where useful.

**Tech Stack:** Python, argparse, pathlib, shutil, pytest.

---

### Task 1: Admin Store

**Files:**
- Create: `app/admin.py`
- Test: `tests/test_admin_cli.py`

- [ ] **Step 1: Write failing tests**

Add tests for listing users/projects, reading user and project data, dry-run deletion, and confirmed deletion against temp directories.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_admin_cli.py -q`
Expected: fail because `klonet_agent.app.admin` does not exist.

- [ ] **Step 3: Implement minimal admin store**

Create `AdminDataStore` with methods for `list_users`, `list_projects`, `read_user`, `read_conversation`, `read_project`, `delete_user`, `delete_conversation`, and `delete_project`.

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_admin_cli.py -q`
Expected: pass.

### Task 2: Root CLI Subcommands

**Files:**
- Modify: `agent.py`
- Test: `tests/test_admin_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Add subprocess tests for `admin list-users` and dry-run `admin delete-project`.

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_admin_cli.py -q`
Expected: fail because `agent.py` does not parse `admin`.

- [ ] **Step 3: Implement argparse subcommands**

Keep existing chat flags working, and dispatch `admin` commands to `AdminDataStore`.

- [ ] **Step 4: Run targeted tests**

Run: `python -m pytest tests/test_admin_cli.py tests/test_cli_entry.py -q`
Expected: pass.

### Task 3: Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Run broader relevant tests**

Run: `python -m pytest tests/test_admin_cli.py tests/test_cli_entry.py tests/test_session.py -q`
Expected: pass.

- [ ] **Step 2: Inspect git diff**

Run: `git diff -- agent.py app/admin.py tests/test_admin_cli.py docs/superpowers/specs/2026-06-28-cli-admin-data-management-design.md docs/superpowers/plans/2026-06-28-cli-admin-data-management.md`
Expected: only the planned CLI admin changes.
