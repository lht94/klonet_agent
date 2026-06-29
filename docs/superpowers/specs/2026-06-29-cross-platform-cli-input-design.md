# Cross-Platform CLI Input Design

## Goal

Make interactive Chinese text editing behave consistently on Windows and Ubuntu. In particular, pressing Backspace after a Chinese character must remove the complete character without leaving terminal artifacts or passing damaged UTF-8 bytes to the agent.

## Root Cause

The CLI currently calls Python's built-in `input()` without loading an interactive line editor. On the affected Ubuntu terminal, the `IUTF8` terminal flag is disabled, so canonical-mode Backspace erases one UTF-8 byte instead of one Unicode character. Windows uses a different console input implementation and does not reproduce the problem.

This is separate from whether a submitted user line remains visible in terminal history. Submitted input must continue to remain visible.

## Approach

Use the operating system's existing input support without adding a package dependency:

1. Add `configure_interactive_input()` to `app/cli.py`.
2. Call it from `run_chat()` after the existing stream encoding configuration and before the first `input()` call.
3. When stdin and stdout are interactive TTY streams on a non-Windows platform, load the system-provided `readline` module. Loading it installs Python's Unicode-aware interactive line editor.
4. On Windows, for redirected input/output, or when `readline` is unavailable, leave the current input mechanism unchanged.

The function will not modify prompts, agent behavior, model messages, terminal history, or submitted-line rendering.

## Platform Behavior

### Ubuntu and other Unix-like systems

Interactive TTY sessions use the installed GNU readline support. Chinese Backspace handling becomes character-aware even when the parent terminal starts with `-iutf8`. Arrow-key navigation and normal readline history behavior are acceptable side effects.

### Windows

The configuration is a no-op. Python continues using the native Windows console input path, preserving the behavior that already works.

### Pipes and redirected streams

The configuration is a no-op. Existing UTF-8 pipe decoding and multiline prompt behavior remain unchanged.

## Error Handling

`readline` is optional in some minimal Unix Python builds. If it cannot be imported, startup must continue rather than failing the agent. The fallback is the existing `input()` behavior.

Only an unavailable `readline` module is treated as an optional-platform condition. Unrelated startup errors must not be hidden.

## Testing

Add tests in `tests/test_cli_entry.py` that verify:

- an interactive non-Windows TTY loads `readline`;
- Windows does not try to load `readline`;
- redirected stdin or stdout does not load `readline`;
- a missing optional `readline` module does not prevent startup configuration;
- existing encoding, piped-input, and submitted-line-preservation tests continue to pass.

The implementation follows test-driven development: add the failing platform-branch tests first, observe the expected failure, then add the smallest production change that passes them.

## Acceptance Criteria

- On Ubuntu, typing `你好` and pressing Backspace once leaves `你`, with no residual character or damaged UTF-8 input.
- Existing single-byte English input and Backspace behavior remain unchanged.
- On Windows, interactive input behavior remains unchanged.
- No new third-party dependency is added.
- Submitted user input remains visible after Enter.
- The CLI test suite passes on the supported Python 3.8 runtime.
