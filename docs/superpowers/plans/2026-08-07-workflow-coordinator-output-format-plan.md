# Workflow Coordinator Output Formatting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve readable model-produced line breaks in every final Workflow Coordinator response without changing answer content or adding another model call.

**Architecture:** Keep the existing `V4ResponseAgent` as the single response-generation boundary. Add presentation guidance to its current system prompt and replace destructive whitespace flattening with leading/trailing trimming only.

**Tech Stack:** Python 3.8, existing OpenAI-compatible LLM client, pytest.

---

### Task 1: Prove final response layout is preserved

**Files:**
- Create: `tests/test_privileged_v4_response.py`

- [ ] **Step 1: Write the failing test**

```python
from types import SimpleNamespace

from klonet_agent.ops.privileged.v4.contracts import EvidenceConclusion
from klonet_agent.ops.privileged.v4.response import V4ResponseAgent


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content)
                )
            ]
        )


def test_readonly_response_preserves_model_layout_with_one_call():
    llm = FakeLLM("\n结论：已发现平台。\n\n1. 平台 A\n2. 平台 B\n")

    result = V4ResponseAgent(llm).render_readonly(
        "检查平台",
        EvidenceConclusion(),
    )

    assert result == "结论：已发现平台。\n\n1. 平台 A\n2. 平台 B"
    assert len(llm.calls) == 1
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "合理分段" in system_prompt
    assert "列表项分别换行" in system_prompt
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
/home/lzl/miniconda3/envs/klonet_agent/bin/python -m pytest -q tests/test_privileged_v4_response.py
```

Expected: FAIL because the existing implementation flattens all line breaks.

### Task 2: Preserve the model's formatting

**Files:**
- Modify: `ops/privileged/v4/response.py:22-39`
- Test: `tests/test_privileged_v4_response.py`

- [ ] **Step 1: Strengthen the existing prompt and remove whitespace flattening**

Update the existing system message to include:

```python
"保持事实、顺序和含义不变；使用合理分段，列表项分别换行，"
"不要输出多余空行。"
```

Replace:

```python
text = " ".join((response.choices[0].message.content or "").split())
```

with:

```python
text = (response.choices[0].message.content or "").strip()
```

- [ ] **Step 2: Run focused tests and verify GREEN**

Run:

```bash
/home/lzl/miniconda3/envs/klonet_agent/bin/python -m pytest -q \
  tests/test_privileged_v4_response.py \
  tests/test_privileged_v4_coordinator.py \
  tests/test_privileged_v4_discovery.py
```

Expected: all selected tests pass.

- [ ] **Step 3: Commit the implementation**

```bash
git add ops/privileged/v4/response.py tests/test_privileged_v4_response.py
git commit -m "fix: preserve workflow coordinator response layout"
```

### Task 3: Validate and publish

**Files:**
- Verify: repository-wide Python sources and tests

- [ ] **Step 1: Run Python 3.8 compilation**

```bash
/home/lzl/miniconda3/envs/klonet-py38/bin/python3.8 -m compileall -q \
  agent.py app config.py evals ops orchestrator.py tools
```

Expected: exit code 0.

- [ ] **Step 2: Run the full suite**

```bash
/home/lzl/miniconda3/envs/klonet_agent/bin/python -m pytest -q \
  --basetemp=/tmp/lzl_workflow_format_pytest
```

Expected: all tests pass.

- [ ] **Step 3: Run diff and repository checks**

```bash
git diff --check
git status --short
```

Expected: only the pre-existing untracked Miniconda installer remains.

- [ ] **Step 4: Push master**

```bash
git push origin master
```

Expected: `origin/master` advances to the implementation commit.
