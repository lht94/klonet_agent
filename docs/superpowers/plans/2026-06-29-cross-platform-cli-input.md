# Cross-Platform CLI Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Chinese Backspace editing reliable on Ubuntu while preserving the existing Windows and English-input behavior.

**Architecture:** Add one CLI startup function that conditionally activates the system-provided `readline` editor only for interactive non-Windows terminals. Keep Windows, redirected streams, missing optional readline support, prompts, and submitted-line rendering unchanged.

**Tech Stack:** Python 3.8, standard-library `importlib`, optional system `readline`, pytest.

## Global Constraints

- Add no third-party dependency.
- Preserve native Windows console input behavior.
- Preserve single-byte English input and Backspace behavior.
- Do not change prompts, model messages, or whether submitted user input remains visible.
- Keep piped and redirected input behavior unchanged.

---

### Task 1: Configure Unicode-aware interactive input

**Files:**
- Modify: `tests/test_cli_entry.py`
- Modify: `app/cli.py`

**Interfaces:**
- Consumes: stdin and stdout objects with optional `isatty()`, plus `sys.platform`.
- Produces: `configure_interactive_input(stdin=None, stdout=None) -> None`.

- [ ] **Step 1: Write the failing platform tests**

Add `import importlib` and `import pytest`, then add these tests to `tests/test_cli_entry.py`:

```python
def test_cli_loads_readline_for_interactive_non_windows_terminal(monkeypatch):
    """Unix 交互终端应启用系统 readline 处理多字节字符退格。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)
    loaded = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(importlib, "import_module", loaded.append)

    configure(
        stdin=FakeStream(is_tty=True),
        stdout=FakeStream(is_tty=True),
    )

    assert loaded == ["readline"]


def test_cli_keeps_native_windows_interactive_input(monkeypatch):
    """Windows 应保留已经正常工作的原生控制台输入。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)
    loaded = []
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(importlib, "import_module", loaded.append)

    configure(
        stdin=FakeStream(is_tty=True),
        stdout=FakeStream(is_tty=True),
    )

    assert loaded == []


@pytest.mark.parametrize(
    ("stdin_is_tty", "stdout_is_tty"),
    [(False, True), (True, False), (False, False)],
)
def test_cli_does_not_load_readline_for_redirected_streams(
    monkeypatch,
    stdin_is_tty,
    stdout_is_tty,
):
    """管道或重定向流不能切换成交互式 readline。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)
    loaded = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(importlib, "import_module", loaded.append)

    configure(
        stdin=FakeStream(is_tty=stdin_is_tty),
        stdout=FakeStream(is_tty=stdout_is_tty),
    )

    assert loaded == []


def test_cli_allows_missing_optional_readline(monkeypatch):
    """精简 Unix Python 缺少 readline 时仍应正常启动。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)

    def missing_readline(name):
        raise ModuleNotFoundError("No module named 'readline'", name=name)

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(importlib, "import_module", missing_readline)

    configure(
        stdin=FakeStream(is_tty=True),
        stdout=FakeStream(is_tty=True),
    )


def test_cli_does_not_hide_unrelated_readline_import_errors(monkeypatch):
    """readline 内部依赖故障不能被误当成可选模块缺失。"""

    import klonet_agent.app.cli as cli

    configure = getattr(cli, "configure_interactive_input", None)
    assert callable(configure)

    def missing_dependency(name):
        raise ModuleNotFoundError("No module named '_readline'", name="_readline")

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(importlib, "import_module", missing_dependency)

    with pytest.raises(ModuleNotFoundError, match="_readline"):
        configure(
            stdin=FakeStream(is_tty=True),
            stdout=FakeStream(is_tty=True),
        )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
agent/bin/python -m pytest tests/test_cli_entry.py -q
```

Expected: the new tests fail at `assert callable(configure)` because `configure_interactive_input` does not exist; existing tests still pass.

- [ ] **Step 3: Add the minimal interactive-input configuration**

In `app/cli.py`, import `importlib` and add:

```python
def configure_interactive_input(stdin=None, stdout=None):
    """在 Unix 交互终端启用系统 readline，正确编辑多字节字符。"""

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if sys.platform == "win32":
        return
    if not (
        hasattr(stdin, "isatty")
        and stdin.isatty()
        and hasattr(stdout, "isatty")
        and stdout.isatty()
    ):
        return

    try:
        importlib.import_module("readline")
    except ModuleNotFoundError as exc:
        if exc.name != "readline":
            raise
```

Call `configure_interactive_input()` immediately after `configure_console_encoding()` in `run_chat()`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
agent/bin/python -m pytest tests/test_cli_entry.py -q
```

Expected: all tests in `tests/test_cli_entry.py` pass.

- [ ] **Step 5: Run the complete regression suite**

Run:

```bash
agent/bin/python -m pytest -q
```

Expected: the complete test suite passes with no new failures.

- [ ] **Step 6: Verify the real Ubuntu terminal behavior**

Start the CLI from an Ubuntu TTY whose `stty -a` output contains `-iutf8`. At `用户：`, type `你好`, press Backspace once, and press Enter.

Expected: the visible input and the value received by the application contain only `你`; English Backspace editing remains unchanged.

- [ ] **Step 7: Commit the implementation**

```bash
git add app/cli.py tests/test_cli_entry.py
git commit -m "fix: support Unicode CLI editing on Unix"
```
