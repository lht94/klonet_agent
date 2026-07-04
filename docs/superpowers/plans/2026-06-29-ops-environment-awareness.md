# Ops Environment Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only Ops agent and environment diagnostic path for Klonet runtime troubleshooting.

**Architecture:** Reuse the existing orchestrator and tool loop. Add a dedicated `ops` profile plus structured read-only environment tools; extend turn intent with a boolean diagnostic flag so Mentor can recommend Ops without losing its teaching behavior.

**Tech Stack:** Python dataclasses, pytest, existing OpenAI-compatible tool schema, existing ToolExecutor and AgentProfile patterns.

---

## 2026-07-03 Extension: Ops Tool Expansion

User feedback from real server tests shows that Ops still lacks several
operator-grade capabilities. Add the following directions to the remaining
work:

1. **Archive and prepare-file recipes.** `prepare-files` must not be blocked by
   `no_recipe_attached`. Add allowlisted recipes for extracting a Klonet install
   bundle, validating the extracted directory, copying `mains/` entry files into
   the project root, and then running higher-risk setup scripts only through
   confirmed OperationPlan steps.
2. **Wider read-only system probes.** Questions about system Python, command
   paths, package manager records, mounts, users/groups, sudo availability, and
   service versions should use fixed read-only probes instead of `read_ops_file`.
   Reading `/usr/bin/python3` as a file is the wrong abstraction; use
   `inspect_system_environment` or a future command/path inspection tool.
3. **Controlled write tools.** Ops needs write capability for operational
   artifacts such as generated notes, config drafts, Nginx draft files, and
   planned file edits. These must be scoped by OperationPlan, use allowlisted
   target directories or explicit project roots, write backups when overwriting,
   redact secrets in previews, and require confirmation before changing runtime
   files.
4. **Runbook-derived tool backlog.** Based on the Klonet runbooks, future tools
   should cover: archive listing/extraction, checksum and file inventory,
   `mains/` entry-file copy validation, `base_requ_setup.sh` and
   `docker_service.sh` controlled execution, Docker daemon config backup/merge
   preview, Redis availability checks, Nginx syntax/reload workflow, frontend
   config validation, screen lifecycle operations, port/process ownership,
   service health checks, and safe log/screen evidence capture.

Implementation should stay incremental: every new modifying capability starts as
dry-run, then requires exact plan confirmation and step confirmation before real
execution.

## 2026-07-04 Update: Tool Backlog Status

Reading the current Klonet startup and environment setup runbooks shows two
separate phases:

- environment setup: inspect an install bundle, extract it, validate
  `vemu_install_new_gen`, run `base_requ_setup.sh NORMAL`, run
  `docker_service.sh`, then verify Docker/Redis/MySQL/RabbitMQ/OVS/KVM/libvirt;
- platform startup: inspect all existing platforms first, choose non-conflicting
  screen names and ports, copy `mains/` entry files into the project root,
  render backend/frontend/nginx config drafts, start four backend screens, then
  verify screen/process/port/Nginx health.

Implemented substrate so far:

- read-only runtime context: `inspect_ops_context`, `inspect_platform_instances`,
  `inspect_process_detail`, `inspect_nginx_routes`, `inspect_screen_session`,
  `read_ops_file`, `read_klonet_logs`;
- read-only deployment prep: `inspect_archive` lists zip/tar members and path
  traversal risk without extracting;
- read-only system probes: `inspect_system_environment` supports fixed baseline
  checks plus `system_python` and `command_paths` for command path/version
  verification;
- read-only config drafting: `render_docker_daemon_config` merges
  `insecure-registries` into an existing `daemon.json` draft while preserving
  existing mirrors, DNS, runtimes and other Docker daemon fields;
- controlled recipes: `extract_archive`, `prepare_project_files`,
  `run_install_script`, `write_ops_file`, `reload_nginx`,
  `start_platform_screens`, stop/restart screen recipes.

Remaining high-value tools:

1. Service/container health summary: deterministic Redis/MySQL/RabbitMQ/Nginx
   container/process health with "reuse existing service" guidance.
2. Script inventory and preflight: inspect extracted install scripts, list
   expected scripts, executable bits, shebangs, and risky commands before
   attaching `run_install_script`.
3. Platform health verifier: after start/restart, verify screen sessions,
   process cwd, configured ports, Nginx routes and selected HTTP health
   endpoints in one structured result.
4. Frontend config validator: compare rendered frontend config against actual
   `scripts/config.js` field names and Nginx aliases.

---

## File Structure

- `agents/profile.py`: add `ops` profile and tool allowlist.
- `prompts.py`: add `OPS_PROMPT` and Mentor guidance for recommending Ops on runtime failures.
- `agent.py`: allow `--mode ops`.
- `knowledge/intent.py`: add `requires_environment_diagnosis` to trusted intent data and schemas.
- `knowledge/turn_intent.py`: carry the environment diagnosis flag into turn decisions.
- `knowledge/intent_analyzer.py`: update prompt to output the flag for operations faults.
- `tools/environment.py`: implement fixed read-only probes, status vocabulary, safe log reading, and redaction.
- `tools/registry.py`: register `inspect_system_environment`, `inspect_klonet_runtime`, and `read_klonet_logs`.
- `tools/executor.py`: dispatch the new tools.
- `tests/test_ops_agent.py`: profile, CLI, and prompt behavior.
- `tests/test_ops_environment_tools.py`: redaction, command whitelist, status vocabulary, executor routing.
- `tests/test_turn_intent.py`: environment diagnosis flag propagation.

## Task 1: Add Ops Profile And Mode

**Files:**
- Modify: `agents/profile.py`
- Modify: `prompts.py`
- Modify: `agent.py`
- Test: `tests/test_ops_agent.py`

- [ ] **Step 1: Write failing profile and CLI tests**

```python
from klonet_agent.agents import get_profile
from klonet_agent.agent import main


def test_ops_profile_uses_read_only_environment_tools():
    profile = get_profile("ops")
    assert profile.name == "ops"
    assert "search_knowledge" in profile.allowed_tools
    assert "inspect_system_environment" in profile.allowed_tools
    assert "inspect_klonet_runtime" in profile.allowed_tools
    assert "read_klonet_logs" in profile.allowed_tools
    assert "run_command" not in profile.allowed_tools
    assert "write_file" not in profile.allowed_tools


def test_agent_cli_accepts_ops_mode(monkeypatch):
    captured = {}

    def fake_run_chat(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("klonet_agent.app.run_chat", fake_run_chat)
    monkeypatch.setattr("sys.argv", ["agent.py", "--mode", "ops"])
    main()
    assert captured["mode"] == "ops"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_ops_agent.py -q`
Expected: FAIL because `ops` mode and environment tools do not exist.

- [ ] **Step 3: Implement minimal profile, prompt, and CLI support**

Add `OPS_PROMPT`, extend `MENTOR_PROMPT` with a short recommendation rule, add `OPS_TOOLS`, and include `ops` in argparse choices.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_ops_agent.py -q`
Expected: PASS.

## Task 2: Add Environment Diagnosis Intent Flag

**Files:**
- Modify: `knowledge/intent.py`
- Modify: `knowledge/turn_intent.py`
- Modify: `knowledge/intent_analyzer.py`
- Test: `tests/test_turn_intent.py`

- [ ] **Step 1: Write failing intent propagation test**

```python
from klonet_agent.knowledge.intent import QueryIntent
from klonet_agent.knowledge.turn_intent import TurnIntentBuilder


def test_troubleshooting_can_request_environment_diagnosis():
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "troubleshooting",
            "operation": "platform_start",
            "target": "nginx",
            "symptom": "port_conflict",
            "requires_environment_diagnosis": True,
            "confidence": 0.91,
        }
    )
    turn_intent = TurnIntentBuilder().build("Klonet nginx 启动报端口占用", intent=intent)

    assert intent.requires_environment_diagnosis is True
    assert turn_intent.requires_environment_diagnosis is True
    assert turn_intent.to_query_intent().requires_environment_diagnosis is True
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_turn_intent.py::test_troubleshooting_can_request_environment_diagnosis -q`
Expected: FAIL because the flag is not defined.

- [ ] **Step 3: Implement flag in intent and turn intent**

Add a boolean field to `QueryIntent` and `TurnIntent`, copy it through `from_mapping`, `build`, `_inherit_for_continue`, and `to_query_intent`. Update the intent prompt and search tool schema to mention the field.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_turn_intent.py::test_troubleshooting_can_request_environment_diagnosis -q`
Expected: PASS.

## Task 3: Implement Read-Only Environment Tools

**Files:**
- Create: `tools/environment.py`
- Modify: `tools/registry.py`
- Modify: `tools/executor.py`
- Test: `tests/test_ops_environment_tools.py`

- [ ] **Step 1: Write failing redaction and read-only tests**

```python
from klonet_agent.tools.environment import (
    redact_sensitive_text,
    read_klonet_logs,
    run_read_only_probe,
)


def test_redacts_common_secret_shapes():
    text = "PASSWORD=abc123\nAuthorization: Bearer token-value\napi_key = sk-test"
    redacted = redact_sensitive_text(text)
    assert "abc123" not in redacted
    assert "token-value" not in redacted
    assert "sk-test" not in redacted
    assert "[REDACTED]" in redacted


def test_read_only_probe_rejects_unregistered_command():
    result = run_read_only_probe("rm -rf /")
    assert result.status == "unchecked"
    assert "not allowlisted" in result.detail


def test_log_reader_refuses_env_files(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PASSWORD=abc123", encoding="utf-8")
    result = read_klonet_logs({"path": str(env_file)})
    assert result.startswith("Error:")
    assert "refused" in result.lower()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_ops_environment_tools.py -q`
Expected: FAIL because `tools.environment` does not exist.

- [ ] **Step 3: Implement minimal environment module**

Create a `ProbeResult` dataclass, `redact_sensitive_text`, `run_read_only_probe`, `inspect_system_environment`, `inspect_klonet_runtime`, and `read_klonet_logs`. Use fixed allowlist keys mapped to commands such as `uname -a`, `python --version`, `docker ps --format ...`, `screen -ls`, `ss -ltnp`, `systemctl is-active nginx`, and safe fallbacks. Use `subprocess.run(..., timeout=5, text=True, capture_output=True)` and convert failures to `unchecked`.

- [ ] **Step 4: Register and dispatch tools**

Add tool schemas for the three environment tools and executor branches that call the new module functions.

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_ops_environment_tools.py -q`
Expected: PASS.

## Task 4: Verify Integration And Existing Behavior

**Files:**
- Modify as needed from prior tasks only.
- Test: existing targeted suites.

- [ ] **Step 1: Run targeted suites**

Run: `pytest tests/test_ops_agent.py tests/test_ops_environment_tools.py tests/test_turn_intent.py tests/test_intent_routing.py -q`
Expected: PASS.

- [ ] **Step 2: Run full suite**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 3: Inspect diff**

Run: `git diff --stat`
Expected: only planned files changed.
