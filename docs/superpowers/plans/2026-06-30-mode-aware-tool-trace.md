# Mode-Aware Tool Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Mentor show only aggregate reasoning summaries while Ops shows concise, real “action → observation” tool traces.

**Architecture:** Keep phase progress, visible reasoning, and tool tracing as separate presentation concerns inside `AgentOrchestrator`. Replace the current Ops call/complete/result/next-step sequence with deterministic action formatting and bounded observation extraction; do not add model calls or change tool execution/history behavior.

**Tech Stack:** Python 3.8+, pytest, existing `AgentOrchestrator` CLI output helpers.

## Global Constraints

- Mentor keeps “正在理解你的问题”, “已识别”, and “正在组织回答” in non-brief mode.
- Mentor never prints per-tool action or observation lines, but its final reasoning summary still aggregates `tool_events`.
- Ops prints deterministic action and real observation content; it does not print hidden model reasoning.
- Ops removes the legacy “工具完成”, “工具结果摘要”, and fixed “下一步” lines.
- Observation output contains at most three non-empty result lines, with each line capped at 160 characters.
- Action arguments are restricted to `query`, `path`, `session`, and `checks`; unknown tools do not print their argument dictionaries.
- `brief` mode prints none of the phase progress, tool trace, or reasoning summary.
- Do not alter tool selection, execution, permissions, history, or loop budgets.
- Preserve the existing untracked `memory/shared/` directory.

---

### Task 1: Separate Mentor Progress from Ops Tool Tracing

**Files:**
- Modify: `tests/test_orchestrator_controls.py:1023-1104`
- Modify: `orchestrator.py:670-681`

**Interfaces:**
- Consumes: `AgentOrchestrator._show_progress_updates() -> bool` for phase milestones.
- Produces: the existing tool loop calls `_print_tool_loop_action(name, args)` and `_print_tool_loop_observation(name, result)`; both methods are Ops-only presentation hooks.

- [ ] **Step 1: Change the Mentor regression test to require phase progress but forbid per-tool output**

Replace the tool assertions in `test_default_mode_prints_progress_milestones` with:

```python
    assert "正在理解你的问题" in output
    assert "已识别：" in output
    assert "正在组织回答" in output
    assert "正在检索知识库" not in output
    assert "正在调用工具" not in output
    assert "工具完成" not in output
    assert "观察：" not in output
    assert "思考摘要" in output
    assert "已调用 search_knowledge" in output
```

- [ ] **Step 2: Run the Mentor test and verify RED**

Run:

```bash
pytest -q tests/test_orchestrator_controls.py::test_default_mode_prints_progress_milestones
```

Expected: FAIL because current Mentor output contains `正在调用工具：search_knowledge` and `工具完成：search_knowledge`.

- [ ] **Step 3: Route tool-loop output through Ops-only hooks**

Replace the three presentation calls around `self.use_tool(...)` in `orchestrator.py` with:

```python
                    self._print_tool_loop_action(tool_name, tool_args)
                    result = self.use_tool(tool_name, tool_args)
                    self._print_tool_loop_observation(tool_name, result)
```

Do not change `tool_events.append(...)`, tool history messages, or memory writes.

- [ ] **Step 4: Add the minimal Ops-only action hook**

Add this method immediately before `_print_tool_loop_observation`:

```python
    def _print_tool_loop_action(self, tool_name: str, tool_args: dict) -> None:
        """Print one safe Ops action before a tool executes."""

        if self.profile.name != "ops" or self.answer_style == "brief":
            return
        print(f"Klonet Agent：正在执行工具：{tool_name}")
```

Keep `_print_tool_loop_observation` temporarily unchanged so this task changes only mode separation.

- [ ] **Step 5: Run the Mentor test and verify GREEN**

Run:

```bash
pytest -q tests/test_orchestrator_controls.py::test_default_mode_prints_progress_milestones
```

Expected: PASS.

- [ ] **Step 6: Commit the mode separation**

```bash
git add orchestrator.py tests/test_orchestrator_controls.py
git commit -m "fix: hide per-tool traces in mentor mode"
```

---

### Task 2: Render Ops Actions with Safe, Useful Parameters

**Files:**
- Modify: `tests/test_orchestrator_controls.py:1067-1104`
- Modify: `orchestrator.py:810-824`

**Interfaces:**
- Consumes: `_print_tool_loop_action(tool_name: str, tool_args: dict) -> None` from Task 1.
- Produces: `_format_tool_action(tool_name: str, tool_args: dict) -> str` returning one bounded, user-facing action description.

- [ ] **Step 1: Update the Ops integration test with the desired action and legacy-output assertions**

In `test_ops_mode_prints_tool_loop_trace_without_reasoning_summary`, replace the current tool trace assertions with:

```python
    assert "已识别：" in output
    assert "正在检索知识库：Klonet 启动" in output
    assert "观察：unexpected tool result" in output
    assert "正在调用工具" not in output
    assert "工具完成" not in output
    assert "工具结果摘要" not in output
    assert "下一步：" not in output
    assert "思考摘要" not in output
```

- [ ] **Step 2: Add a unit-level test for argument allowlisting and fallback behavior**

Add the following test after the Ops integration test:

```python
def test_ops_tool_action_uses_safe_arguments_only():
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator.answer_style = "default"

    assert orchestrator._format_tool_action(
        "search_code",
        {"query": "vemu_frontend", "token": "secret"},
    ) == "正在搜索源码：vemu_frontend"
    assert orchestrator._format_tool_action(
        "inspect_screen_session",
        {"session": "102_m", "password": "secret"},
    ) == "正在检查 screen 会话：102_m"
    assert orchestrator._format_tool_action(
        "unknown_tool",
        {"password": "secret"},
    ) == "正在执行工具：unknown_tool"
```

- [ ] **Step 3: Run both Ops tests and verify RED**

Run:

```bash
pytest -q \
  tests/test_orchestrator_controls.py::test_ops_mode_prints_tool_loop_trace_without_reasoning_summary \
  tests/test_orchestrator_controls.py::test_ops_tool_action_uses_safe_arguments_only
```

Expected: FAIL because `_format_tool_action` does not exist and the integration output still uses the temporary generic action plus legacy observation text.

- [ ] **Step 4: Implement bounded action formatting**

Add these methods to `AgentOrchestrator` and update `_print_tool_loop_action` to print the formatter result:

```python
    def _print_tool_loop_action(self, tool_name: str, tool_args: dict) -> None:
        """Print one safe Ops action before a tool executes."""

        if self.profile.name != "ops" or self.answer_style == "brief":
            return
        print(f"Klonet Agent：{self._format_tool_action(tool_name, tool_args)}")

    def _format_tool_action(self, tool_name: str, tool_args: dict) -> str:
        """Return a deterministic action using only allowlisted arguments."""

        actions = {
            "search_knowledge": ("正在检索知识库", "query"),
            "search_shared_ops_memory": ("正在检索历史诊断", "query"),
            "search_code": ("正在搜索源码", "query"),
            "list_source_files": ("正在查看源码目录", "path"),
            "list_files": ("正在查看目录", "path"),
            "read_source_file": ("正在读取源码", "path"),
            "read_file": ("正在读取文件", "path"),
            "read_ops_file": ("正在读取运维文件", "path"),
            "read_klonet_logs": ("正在读取 Klonet 日志", "path"),
            "inspect_screen_session": ("正在检查 screen 会话", "session"),
            "inspect_system_environment": ("正在检查系统环境", "checks"),
            "inspect_ops_context": ("正在检查运维环境", "checks"),
            "inspect_klonet_runtime": ("正在检查 Klonet 运行状态", "checks"),
        }
        action, key = actions.get(tool_name, (f"正在执行工具：{tool_name}", ""))
        if not key or key not in tool_args:
            return action
        raw_value = tool_args[key]
        if isinstance(raw_value, list):
            raw_value = "、".join(str(item) for item in raw_value)
        value = " ".join(str(raw_value).split())
        if len(value) > 120:
            value = value[:117] + "..."
        return f"{action}：{value}" if value else action
```

- [ ] **Step 5: Run the safe-action unit test and verify GREEN for action formatting**

Run:

```bash
pytest -q tests/test_orchestrator_controls.py::test_ops_tool_action_uses_safe_arguments_only
```

Expected: PASS.

Do not expect the integration test to pass until Task 3 replaces the legacy observation output.

- [ ] **Step 6: Commit the action formatter**

```bash
git add orchestrator.py tests/test_orchestrator_controls.py
git commit -m "feat: describe ops tool actions safely"
```

---

### Task 3: Show Bounded Real Ops Observations

**Files:**
- Modify: `tests/test_orchestrator_controls.py` after `test_ops_tool_action_uses_safe_arguments_only`
- Modify: `orchestrator.py` in `_print_tool_loop_observation`

**Interfaces:**
- Consumes: raw `result: str` returned by the existing tool executor.
- Produces: `_tool_observation_lines(tool_name: str, result: str) -> tuple[list[str], bool]`, where the list contains at most three display lines and the boolean indicates omitted content.

- [ ] **Step 1: Add observation extraction tests**

```python
def test_ops_observation_shows_three_real_lines_and_omission():
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator.answer_style = "default"
    lines, omitted = orchestrator._tool_observation_lines(
        "inspect_klonet_runtime",
        "\n".join(
            [
                "inspect_klonet_runtime",
                "- redis: detected - active",
                "- docker: detected - redis_102 Up 6 days",
                "- ports: detected - 0.0.0.0:12000",
                "- screen: detected - 102_m",
            ]
        ),
    )

    assert lines == [
        "- redis: detected - active",
        "- docker: detected - redis_102 Up 6 days",
        "- ports: detected - 0.0.0.0:12000",
    ]
    assert omitted is True


def test_ops_observation_handles_empty_error_and_long_lines():
    from klonet_agent.agents import get_profile
    from klonet_agent.orchestrator import AgentOrchestrator

    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.profile = get_profile("ops")
    orchestrator.answer_style = "default"

    assert orchestrator._tool_observation_lines("search_code", "") == (
        ["工具未返回可展示结果。"],
        False,
    )
    assert orchestrator._tool_observation_lines(
        "search_code",
        "Error: source index unavailable",
    ) == (["失败：source index unavailable"], False)
    lines, omitted = orchestrator._tool_observation_lines(
        "search_code",
        "x" * 200,
    )
    assert lines == ["x" * 157 + "..."]
    assert omitted is False
```

- [ ] **Step 2: Run observation tests and verify RED**

Run:

```bash
pytest -q \
  tests/test_orchestrator_controls.py::test_ops_observation_shows_three_real_lines_and_omission \
  tests/test_orchestrator_controls.py::test_ops_observation_handles_empty_error_and_long_lines
```

Expected: FAIL because `_tool_observation_lines` does not exist.

- [ ] **Step 3: Implement observation extraction and printing**

Replace `_print_tool_loop_observation` and add `_tool_observation_lines`:

```python
    def _print_tool_loop_observation(self, tool_name: str, result: str) -> None:
        """Print bounded real tool output as an Ops observation."""

        if self.profile.name != "ops" or self.answer_style == "brief":
            return
        lines, omitted = self._tool_observation_lines(tool_name, result)
        if len(lines) == 1 and not omitted:
            print(f"Klonet Agent：观察：{lines[0]}")
            return
        print("Klonet Agent：观察：")
        for line in lines:
            print(f"  {line}")
        if omitted:
            print("  - 其余内容已省略")

    def _tool_observation_lines(
        self,
        tool_name: str,
        result: str,
    ) -> tuple[list[str], bool]:
        """Extract at most three meaningful, bounded lines from a tool result."""

        candidates = []
        for raw_line in (result or "").splitlines():
            line = raw_line.strip()
            if not line or line == tool_name or line in {"```", "```text"}:
                continue
            if line.startswith("Error:"):
                line = "失败：" + line[len("Error:") :].strip()
            if len(line) > 160:
                line = line[:157] + "..."
            candidates.append(line)
        if not candidates:
            return ["工具未返回可展示结果。"], False
        return candidates[:3], len(candidates) > 3
```

- [ ] **Step 4: Run all new Ops trace tests and verify GREEN**

Run:

```bash
pytest -q \
  tests/test_orchestrator_controls.py::test_ops_mode_prints_tool_loop_trace_without_reasoning_summary \
  tests/test_orchestrator_controls.py::test_ops_tool_action_uses_safe_arguments_only \
  tests/test_orchestrator_controls.py::test_ops_observation_shows_three_real_lines_and_omission \
  tests/test_orchestrator_controls.py::test_ops_observation_handles_empty_error_and_long_lines
```

Expected: 4 passed.

- [ ] **Step 5: Commit bounded observations**

```bash
git add orchestrator.py tests/test_orchestrator_controls.py
git commit -m "feat: show real ops tool observations"
```

---

### Task 4: Verify Presentation Contracts and Full Regression Suite

**Files:**
- Modify only if a regression exposes a requirement violation: `orchestrator.py`, `tests/test_orchestrator_controls.py`

**Interfaces:**
- Consumes: all presentation helpers implemented in Tasks 1–3.
- Produces: verified Mentor, Ops, and brief output contracts without changing orchestration behavior.

- [ ] **Step 1: Run the focused orchestrator presentation tests**

```bash
pytest -q tests/test_orchestrator_controls.py -k "progress_milestones or visible_reasoning_trace or ops_mode_prints_tool_loop_trace or ops_tool_action or ops_observation or brief_mode"
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the complete orchestrator control suite**

```bash
pytest -q tests/test_orchestrator_controls.py
```

Expected: all tests pass.

- [ ] **Step 3: Run the complete project suite**

```bash
pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 4: Check formatting and scope**

```bash
git diff --check
git status --short
```

Expected: `git diff --check` prints nothing; status contains only intended source/test changes plus the pre-existing untracked `memory/shared/` directory.

- [ ] **Step 5: Commit any verification-only correction if Step 1–4 required one**

If no correction was needed, do not create an empty commit. If a correction was needed:

```bash
git add orchestrator.py tests/test_orchestrator_controls.py
git commit -m "test: tighten mode-aware trace regressions"
```
