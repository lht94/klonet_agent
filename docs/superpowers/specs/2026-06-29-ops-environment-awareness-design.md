# Ops Environment Awareness Design

## Goal

Add phase 2 read-only environment awareness for Klonet fault diagnosis by introducing an `ops` agent profile and allowing Mentor to recommend or reuse the same diagnostic path for operations-style troubleshooting.

## Scope

This phase is read-only. It may inspect local machine state, service status, logs, ports, and safe configuration summaries. It must not install packages, restart services, edit files, clean resources, or run arbitrary shell commands.

The first version only checks the local machine where the agent runs. It does not SSH to other hosts.

## Approach

Use the third option approved by the user:

1. Add an explicit `ops` mode for operations diagnosis.
2. Keep Mentor's teaching behavior intact.
3. Let Mentor recognize Klonet operations troubleshooting and recommend switching to `ops`; when useful, Mentor can expose the same read-only diagnostic tools in that turn.

The ops profile reuses the existing orchestrator, RAG, intent analyzer, tool executor, tracing, memory, and answer policy. New logic is limited to intent classification, profile configuration, read-only diagnostic tools, and prompt constraints.

## Intent Behavior

Operations diagnosis is a more specific form of troubleshooting. It applies when the user asks about Klonet runtime failures, errors, service startup problems, port conflicts, logs, screen sessions, nginx, Redis, RabbitMQ, MySQL, Docker, OVS, KVM, libvirt, worker registration, topology progress stalls, or web-terminal runtime errors.

The existing `QueryIntent.task_type="troubleshooting"` remains valid. A new trusted field identifies whether the turn needs environment diagnosis, so retrieval and tool visibility can differ from ordinary conceptual troubleshooting.

Mentor behavior:

- If the turn is clearly an operations diagnosis, tell the user this is suitable for `ops` mode.
- Still answer with evidence when enough knowledge-base evidence exists.
- If environment state is needed, use only read-only environment tools.

Ops behavior:

- Prefer knowledge-base runbooks first.
- Then call read-only environment tools to gather concrete local evidence.
- Continue the tool loop until the likely cause is found, the available safe checks are exhausted, or the configured tool-round limit is reached.
- Final answer must distinguish detected facts, missing facts, and unchecked items.

## Read-Only Tools

Add structured tools instead of exposing arbitrary shell:

- `inspect_system_environment`: OS, kernel, architecture, CPU, memory, disk, virtualization hints.
- `inspect_klonet_runtime`: Klonet process hints, relevant ports, screen sessions, nginx status, Docker/container summary, and common dependency service status.
- `read_klonet_logs`: safe tail/read from whitelisted log locations or user-provided whitelisted paths; redact secrets before returning.

All tool output uses a consistent status vocabulary:

- `detected`: check ran and found evidence.
- `missing`: check ran and did not find evidence.
- `unchecked`: check could not run or was not supported.

## Safety

Environment tools must:

- Execute only a fixed whitelist of read-only commands.
- Never accept arbitrary shell text.
- Redact API keys, tokens, passwords, private keys, cookies, bearer tokens, and dotenv-style secrets.
- Avoid reading `.env`, private keys, token files, or credential files.
- Limit file reads to safe workspace paths and explicit log/config allowlists.
- Treat command failure as `unchecked`, not as absence.

## Files

- Modify `agents/profile.py` to add `ops` profile and allowed tools.
- Modify `prompts.py` to add `OPS_PROMPT`.
- Modify `agent.py` CLI choices to include `ops`.
- Modify `knowledge/intent.py` and `knowledge/turn_intent.py` to represent environment diagnosis.
- Modify `knowledge/intent_analyzer.py` prompt to classify ops diagnostics.
- Create `tools/environment.py` for read-only environment inspection and redaction.
- Modify `tools/registry.py` and `tools/executor.py` to register and run the new tools.
- Add tests for profile mode, intent classification, tool schemas, executor routing, redaction, and read-only command boundaries.

## Acceptance

- `python agent.py --mode ops` starts an ops profile.
- Mentor can recognize Klonet runtime troubleshooting as an ops diagnostic and include a switch recommendation.
- Ops tools are available only through structured read-only functions.
- Tool outputs redact secrets and mark failed checks as `unchecked`.
- Existing Mentor and Coding tests still pass.
