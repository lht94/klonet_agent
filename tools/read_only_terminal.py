"""Structured read-only terminal execution for Ops diagnostics."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


MAX_COMMANDS = 4
MAX_ARGS = 40
MAX_OUTPUT_CHARS = 12000
SAFE_PROGRAMS = {
    "which",
    "ls",
    "find",
    "stat",
    "grep",
    "rg",
    "head",
    "tail",
    "ps",
    "ss",
    "systemctl",
}


def run_readonly_command(args: dict | None = None) -> str:
    values = args or {}
    raw_pipeline = values.get("pipeline")
    if raw_pipeline is None:
        raw_pipeline = [{"program": values.get("program"), "argv": values.get("argv", [])}]
    if not isinstance(raw_pipeline, list) or not raw_pipeline:
        return "Error: pipeline must be a non-empty array"
    if len(raw_pipeline) > MAX_COMMANDS:
        return f"Error: pipeline exceeds maximum of {MAX_COMMANDS} commands"
    commands = []
    for raw in raw_pipeline:
        problem, command = _validated_command(raw)
        if problem:
            return f"Error: {problem}"
        commands.append(command)
    cwd = str(values.get("cwd") or "").strip() or None
    if cwd:
        path = Path(cwd).expanduser()
        if not path.is_absolute() or not path.is_dir():
            return f"Error: invalid cwd: {cwd}"
        cwd = str(path.resolve())
    stderr_mode = str(values.get("stderr") or "capture").strip().lower()
    if stderr_mode not in {"capture", "discard"}:
        return "Error: stderr must be capture or discard"
    try:
        timeout = max(1, min(int(values.get("timeout_seconds", 10)), 30))
    except (TypeError, ValueError):
        return "Error: invalid timeout_seconds"
    return _run_pipeline(commands, cwd=cwd, stderr_mode=stderr_mode, timeout=timeout)


def _validated_command(raw) -> tuple[str, list[str]]:
    if not isinstance(raw, dict):
        return "each pipeline command must be an object", []
    program = str(raw.get("program") or "").strip()
    argv = raw.get("argv", [])
    if not program or not isinstance(argv, list):
        return "program and argv are required", []
    if len(argv) > MAX_ARGS:
        return f"argv exceeds maximum of {MAX_ARGS}", []
    argv = [str(item) for item in argv]
    if any("\x00" in item or "\n" in item or "\r" in item or len(item) > 500 for item in argv):
        return "argv contains an invalid value", []
    basename = Path(program).name
    problem = _validate_program_args(basename, argv)
    if problem:
        return problem, []
    executable = _resolve_program(program)
    if not executable:
        return f"program_not_allowlisted={program}", []
    return "", [executable, *argv]


def _resolve_program(program: str) -> str:
    basename = Path(program).name
    allowed = basename in SAFE_PROGRAMS or _is_python(basename) or _is_pip(basename)
    if not allowed:
        return ""
    if os.path.isabs(program):
        path = Path(program)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else ""
    if os.name == "nt" and basename == "which":
        return shutil.which("where.exe") or shutil.which("where") or ""
    if os.name == "nt" and basename == "grep":
        return shutil.which("findstr.exe") or shutil.which("findstr") or ""
    return shutil.which(program) or ""


def _validate_program_args(program: str, argv: list[str]) -> str:
    if _is_python(program):
        allowed = (
            argv in [["--version"], ["-V"]]
            or len(argv) >= 3
            and argv[:2] == ["-m", "pip"]
            and argv[2] in {"list", "show", "freeze", "--version"}
        )
        return "" if allowed else "python only allows --version or -m pip list/show/freeze"
    if _is_pip(program):
        return "" if argv and argv[0] in {"list", "show", "freeze", "--version"} else "pip only allows list/show/freeze"
    if program == "find" and any(item in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"} for item in argv):
        return "find mutating/executing predicates are not allowed"
    if program == "tail" and any(item == "-f" or item.startswith("--follow") for item in argv):
        return "tail follow mode is not allowed"
    if program == "rg" and any(item == "--pre" or item.startswith("--pre=") for item in argv):
        return "rg --pre is not allowed"
    if program == "ss" and any(item in {"-K", "--kill"} for item in argv):
        return "ss socket-kill mode is not allowed"
    if program == "systemctl":
        if not argv or argv[0] not in {"status", "is-active", "is-enabled", "show", "list-units"}:
            return "systemctl only allows read-only status operations"
    return ""


def _is_python(program: str) -> bool:
    return bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", program))


def _is_pip(program: str) -> bool:
    return bool(re.fullmatch(r"pip(?:\d+(?:\.\d+)*)?", program))


def _run_pipeline(commands: list[list[str]], *, cwd: str | None, stderr_mode: str, timeout: int) -> str:
    processes = []
    previous_stdout = None
    try:
        for index, command in enumerate(commands):
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=previous_stdout,
                stdout=subprocess.PIPE,
                stderr=(subprocess.DEVNULL if stderr_mode == "discard" else subprocess.PIPE),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if previous_stdout is not None:
                previous_stdout.close()
            previous_stdout = process.stdout
            processes.append(process)
        stdout, stderr = processes[-1].communicate(timeout=timeout)
        for process in processes[:-1]:
            process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        for process in processes:
            process.kill()
        return f"Error: readonly command timed out after {timeout}s"
    except OSError as exc:
        for process in processes:
            process.kill()
        return f"Error: readonly command failed: {exc}"
    returncodes = [process.returncode for process in processes]
    accepted = all(code == 0 or (Path(command[0]).name in {"grep", "rg"} and code == 1) for command, code in zip(commands, returncodes))
    output = (stdout or "").strip()
    error = (stderr or "").strip()
    parts = ["readonly_command", f"returncodes={','.join(str(code) for code in returncodes)}"]
    if output:
        parts.append("stdout:\n" + output[:MAX_OUTPUT_CHARS])
    if error and stderr_mode == "capture":
        parts.append("stderr:\n" + error[:2000])
    if not accepted:
        parts.insert(0, "Error: command returned non-zero status")
    return "\n".join(parts)
