# Ops-Privilege Supervisor Single-Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ops Supervisor the single entry for every Ops-Privilege turn, with exact plan control, raw-goal hard denial, model-based intent classification, deterministic read-only execution, mutation PEV, and fail-safe clarification.

**Architecture:** `AgentOrchestrator` sends every Ops-Privilege input to a new `PrivilegedOpsSupervisor`. The Supervisor handles exact control commands, rejects raw hard-denied goals, invokes a tool-less structured intent classifier, delegates conversations to the existing Answerer loop, executes validated read-only actions through Executor plus deterministic Checker, and sends mutations to the existing Planner/Risk/Approval/Executor/Verifier workflow. Existing mutation risk rules remain unchanged.

**Tech Stack:** Python 3.8-compatible Python, OpenAI-compatible chat completions, dataclasses, pytest.

---

### Task 1: Structured intent classifier

**Files:**
- Create: `ops/privileged/intent.py`
- Create: `tests/test_privileged_supervisor.py`

- [ ] **Step 1: Write failing tests**

Cover valid `conversation`, `readonly_action`, `mutating_action`, and `ambiguous` JSON; assert the model receives no tools. Cover invalid JSON followed by one repair and fail-safe `ambiguous` after a second invalid response.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
python -m pytest -q tests/test_privileged_supervisor.py
```

Expected: import failure because `ops.privileged.intent` does not exist.

- [ ] **Step 3: Implement the classifier**

Create:

```python
@dataclass(frozen=True)
class PrivilegedIntentDecision:
    intent: str
    requires_execution: bool
    command: str = ""
    confidence: float = 0.0
    reason: str = ""
```

Implement `PrivilegedIntentClassifier.classify(text)` with a tool-less system prompt, JSON parsing, one repair attempt, allowed intents `{conversation, readonly_action, mutating_action, ambiguous}`, and fail-safe `ambiguous`.

- [ ] **Step 4: Run tests and confirm GREEN**

```bash
python -m pytest -q tests/test_privileged_supervisor.py
```

### Task 2: Goal Safety Guard and exact Plan Control

**Files:**
- Create: `ops/privileged/goal_guard.py`
- Modify: `ops/privileged/workflow.py`
- Test: `tests/test_privileged_supervisor.py`
- Test: `tests/test_privileged_workflow.py`

- [ ] **Step 1: Write failing tests**

Assert raw `rm -rf /` and `curl ... | bash` goals are denied before the classifier. Assert only these exact grammars are control commands:

```text
list-priv
show-priv <plan_id>
confirm-priv <plan_id>
confirm-priv-step <plan_id> <step_id>
resume-priv <plan_id>
abort-priv <plan_id>
```

Malformed or natural-language variants must not be Plan Control.

- [ ] **Step 2: Run tests and confirm RED**

Run the two targeted test files and confirm the new expectations fail.

- [ ] **Step 3: Implement minimal safety and control parsing**

Add `GoalSafetyGuard.check(goal)` using the existing `PrivilegedRiskPolicy.classify_command()` destructive result as the current hard-deny standard. Replace the permissive control regex with exact full-match alternatives and arities.

- [ ] **Step 4: Run tests and confirm GREEN**

Run the targeted test files.

### Task 3: Read-only Executor → Checker flow

**Files:**
- Modify: `ops/privileged/verifier.py`
- Modify: `ops/privileged/workflow.py`
- Test: `tests/test_privileged_supervisor.py`
- Test: `tests/test_privileged_workflow.py`

- [ ] **Step 1: Write failing tests**

Assert a classifier-provided `python3 -V` command:

- is deterministically required to be readonly;
- executes without plan persistence or confirmation;
- completes from execution evidence and Checkers without calling the Verifier LLM;
- is rejected safely if the command is not classified readonly.

- [ ] **Step 2: Run tests and confirm RED**

Run the targeted tests.

- [ ] **Step 3: Implement deterministic read-only submission**

Add `PrivilegedVerifierAgent.verify_deterministic_step()` and
`PrivilegedOpsWorkflow.submit_readonly(goal, command)`. Construct one ephemeral readonly step, infer postconditions, execute it, and use only deterministic Checker results. Do not change mutation risk rules.

- [ ] **Step 4: Run tests and confirm GREEN**

Run the targeted tests.

### Task 4: Supervisor single entry

**Files:**
- Create: `ops/privileged/supervisor.py`
- Modify: `ops/privileged/__init__.py`
- Modify: `orchestrator.py`
- Test: `tests/test_privileged_supervisor.py`
- Test: `tests/test_privileged_integration.py`

- [ ] **Step 1: Write failing branch tests**

Using stub classifier/workflow objects, assert:

- control command → Plan Control without classifier;
- destructive raw goal → denied without classifier or Planner;
- conversation → `handled=False`, allowing the existing Answerer;
- readonly action → `submit_readonly`;
- mutating action → existing `submit`;
- ambiguous → clarification and no execution.

- [ ] **Step 2: Write failing Orchestrator integration tests**

Assert every Ops-Privilege turn calls Supervisor first. Assert `handled=False` continues to the existing LLM Answerer, while handled results return before the main tool loop. Remove assertions tied to `_is_privileged_execution_request`.

- [ ] **Step 3: Run tests and confirm RED**

Run supervisor and integration test files.

- [ ] **Step 4: Implement Supervisor and Orchestrator wiring**

Create `SupervisorResult(handled, kind, message, workflow_result)` and
`PrivilegedOpsSupervisor.handle(text)`. Instantiate it for the Ops-Privilege profile. Replace `_handle_privileged_request()` and delete `_is_privileged_execution_request()`.

- [ ] **Step 5: Run tests and confirm GREEN**

Run supervisor and integration test files.

### Task 5: Prompt, profile, and Eval alignment

**Files:**
- Modify: `prompts.py`
- Modify: `agents/profile.py`
- Modify: `evals/ops_privilege_history_20260726/run_eval.py`
- Modify: `tests/test_prompt_style.py`
- Test: `tests/test_privileged_supervisor.py`

- [ ] **Step 1: Add failing assertions**

Assert the Ops-Privilege prompt describes Supervisor single entry and the four intent outcomes, without instructing the Answerer or Planner to bypass Supervisor.

- [ ] **Step 2: Implement prompt/profile wording**

Set the profile workflow to:

```text
supervisor -> exact plan control -> goal safety -> model intent ->
answer | readonly execute/check | mutation PEV | clarify
```

Update the Eval runner so deterministic mode tests exact control and Goal Guard locally, while model-route results are produced by the live Supervisor path instead of the removed keyword helper.

- [ ] **Step 3: Run prompt and Eval tests**

Run prompt tests and the 60-case deterministic Eval. Record the new baseline without changing mutation risk expectations.

### Task 6: Regression verification and delivery

**Files:**
- Modify only files required by failing regression tests.

- [ ] **Step 1: Run the complete privileged suite**

```bash
python -m pytest -q \
  tests/test_llm_client.py \
  tests/test_python38_compat.py \
  tests/test_privileged_supervisor.py \
  tests/test_privileged_workflow.py \
  tests/test_privileged_integration.py \
  tests/test_privileged_execution.py \
  tests/test_privileged_contracts.py \
  tests/test_privileged_agents.py \
  tests/test_prompt_style.py \
  tests/test_ops_agent.py
```

- [ ] **Step 2: Run repository regression tests**

Run the full test suite and compare failures with the pre-existing baseline. Do not attribute unrelated baseline failures to this change.

- [ ] **Step 3: Audit requirements**

Verify all six branches in the approved diagram have direct test evidence and confirm mutation risk classification logic was not redesigned.

- [ ] **Step 4: Commit and push**

Commit the plan and implementation to `master`, push `origin/master`, and report the exact test and Eval results.
