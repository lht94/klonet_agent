# Ops-Privilege V4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate the V4 staged Ops-Privilege workflow, then replace V3 after a real deployment exercise.

**Architecture:** V4 uses a new coordinator and high-level contracts while reusing the proven low-level safety substrate. Read-only requests terminate after bounded discovery and response synthesis; mutation plans contain only host changes and retain confirmation, authorization hashing, execution recovery, and independent verification.

**Tech Stack:** Python 3.8-compatible dataclasses, OpenAI-compatible chat completions, pytest, existing Ops-Privilege probes/actions/checkers.

---

### Task 1: Baseline and isolated branch

- [x] Audit and commit tracked V3 safety fixes; exclude the untracked installer.
- [x] Create `codex/ops-privilege-v4` in an ignored worktree.
- [x] Run the full baseline suite.

### Task 2: V4 contracts and read-only workflow

- [ ] Add failing contract tests for evidence references, budgets, conclusions, and change-step invariants.
- [ ] Implement V4 contracts and rerun focused tests.
- [ ] Add failing coordinator tests proving readonly requests cannot reach Planner/Binder/Store.
- [ ] Implement Discovery, Synthesis, Response, and the readonly coordinator path.

### Task 3: V4 mutation workflow

- [ ] Add failing tests for mutation-only planning, bounded evidence gaps, Action/Shell-only binding, confirmation hashes, and recovery.
- [ ] Implement Change Planner, Binder, Store, Workflow, and verification orchestration.
- [ ] Reuse the existing Action Runner, Executor, Shell review, Checker, and policy boundaries through adapters.

### Task 4: Runtime selection and regression

- [ ] Add failing factory/integration tests for explicit `v3|v4` selection and no automatic fallback.
- [ ] Wire V4 into the orchestrator and Eval runner.
- [ ] Run focused security tests, full pytest, compile, and diff checks.

### Task 5: Real V4 deployment acceptance

- [ ] Snapshot existing project, process, screen, port, and Nginx state.
- [ ] Run the exact V4 deployment prompt for `v4e2e`, inspect the plan, then confirm it.
- [ ] Verify Git origin/branch, frozen resources, config, processes, ports, Nginx, health, and non-interference.
- [ ] Leave the verified instance running for user inspection.

### Task 6: Final V4 cutover

- [ ] Make V4 the only runtime after all gates pass.
- [ ] Validate and hard-delete all V3 plan JSON files without backup.
- [ ] Remove V3-only planner/workflow/binder/summarizer/store code and tests while retaining shared safety modules.
- [ ] Run full automated and live read-only verification before completion.
