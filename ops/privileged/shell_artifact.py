"""Frozen, single-use shell artifacts for unregistered privileged capabilities."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from klonet_agent.ops.privileged.contracts import ShellArtifact


MAX_SCRIPT_BYTES = 16 * 1024
MAX_SCRIPT_LINES = 120
ALLOWED_ENV_KEYS = {"PATH", "LANG", "LC_ALL", "PYTHONNOUSERSITE"}
FIXED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FORBIDDEN_COMMANDS = {
    "awk",
    "bash",
    "command",
    "curl",
    "dash",
    "env",
    "eval",
    "exec",
    "find",
    "ftp",
    "ksh",
    "nc",
    "netcat",
    "rsync",
    "scp",
    "sftp",
    "sh",
    "socat",
    "source",
    ".",
    "su",
    "sudo",
    "sudoedit",
    "timeout",
    "wget",
    "xargs",
    "zsh",
}
HARD_DENIED_PATTERNS = (
    re.compile(r"(?im)^\s*(?:sudo\s+)?rm\s+-[^\n]*r[^\n]*f[^\n]*\s+/(?:\s|$)"),
    re.compile(r"(?i)\b(?:mkfs(?:\.\w+)?|fdisk|parted)\b"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),
    re.compile(r"(?is)\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash)\b"),
    re.compile(r"(?i)\b(?:visudo|update-alternatives)\b"),
    re.compile(r"(?i)(?:/etc/sudoers|/root/\.ssh/authorized_keys|/etc/ssh/)"),
    re.compile(r"(?i)\b(?:scp|rsync|curl|wget)\b[^\n]*(?:/etc/shadow|/etc/passwd|\.ssh/)"),
    re.compile(r"(?i)\bfind\s+/\s+[^\n]*-delete\b"),
    re.compile(r"(?i)\bgit\s+(?:[^;\n]+\s+)?push\b"),
    re.compile(
        r"(?i)(?:/etc/shadow|/proc/(?:self|\d+)/environ|"
        r"(?:^|/)\.ssh/|(?:^|/)\.gnupg/|(?:^|/)\.aws/credentials)"
    ),
    re.compile(
        r"(?i)(?:/home/klonet-agent/klonet_agent/)?ops/privileged/"
    ),
    re.compile(r"(?i)(?:/home/klonet-agent/)?\.codex/"),
    re.compile(r"(?i)privileged_ops_plans_v4/"),
    re.compile(
        r"(?i)\b(?:password|passwd|token|secret|api[_-]?key|private[_-]?key)"
        r"\s*=\s*[^\s'\"]+"
    ),
)
DESTRUCTIVE_TARGET = re.compile(
    r"(?im)^\s*(?:sudo\s+)?(?:rm|unlink|rmdir)\b([^\n]*)$"
)


class ShellArtifactPolicy:
    """Validate syntax and immutable execution scope before user confirmation."""

    def validate(
        self,
        artifact: ShellArtifact,
        *,
        allowed_future_cwds: tuple[Path, ...] = (),
    ) -> str:
        script = str(artifact.script or "")
        if not script.strip():
            return "shell_artifact_empty"
        executable_lines = [
            line.strip()
            for line in script.splitlines()
            if line.strip() and line.strip() != "set -euo pipefail"
        ]
        if not executable_lines:
            return "shell_artifact_empty"
        script_bytes = len(script.encode("utf-8"))
        if script_bytes > MAX_SCRIPT_BYTES:
            return "shell_artifact_too_large=%s>%s" % (
                script_bytes,
                MAX_SCRIPT_BYTES,
            )
        # The normalization guard is injected by us, so it must not consume
        # one of the model's contract lines. Blank lines do not add execution
        # complexity either.
        script_lines = len(executable_lines)
        if script_lines > MAX_SCRIPT_LINES:
            return "shell_artifact_too_many_lines=%s>%s" % (
                script_lines,
                MAX_SCRIPT_LINES,
            )
        if artifact.interpreter != "/bin/bash":
            return "shell_interpreter_not_allowed"
        cwd = Path(str(artifact.cwd or "")).expanduser()
        if not cwd.is_absolute():
            return "shell_cwd_not_existing_absolute_directory"
        if not cwd.is_dir() and not _is_allowed_future_cwd(
            cwd,
            allowed_future_cwds,
        ):
            return "shell_cwd_not_existing_absolute_directory"
        if artifact.run_as and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.-]{0,63}",
            artifact.run_as,
        ):
            return "shell_run_as_invalid"
        if any(key not in ALLOWED_ENV_KEYS for key in artifact.env_allowlist):
            return "shell_environment_key_not_allowed"
        if artifact.env_allowlist.get("PATH", FIXED_PATH) != FIXED_PATH:
            return "shell_path_must_be_fixed"
        actual_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if artifact.sha256 != actual_hash:
            return "shell_artifact_sha256_mismatch"
        for pattern in HARD_DENIED_PATTERNS:
            if pattern.search(script):
                return "shell_artifact_hard_denied"
        for match in DESTRUCTIVE_TARGET.finditer(script):
            target_text = match.group(1)
            if re.search(r"[$*?\[\]`]", target_text):
                return "shell_destructive_target_must_be_literal"
        syntax_problem = self._bash_syntax_problem(script)
        if syntax_problem:
            return syntax_problem
        ast_problem = self._ast_problem(script)
        if ast_problem:
            return ast_problem
        return ""

    @staticmethod
    def _bash_syntax_problem(script: str) -> str:
        bash = shutil.which("bash")
        if not bash:
            return "bash_not_available"
        result = subprocess.run(
            [bash, "-n"],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
        return "" if result.returncode == 0 else "shell_syntax_invalid"

    @staticmethod
    def _ast_problem(script: str) -> str:
        try:
            import bashlex
        except ModuleNotFoundError:
            return "shell_ast_parser_unavailable"
        try:
            nodes = bashlex.parse(script)
        except Exception:
            return "shell_ast_parse_failed"
        for node in _walk_bash_nodes(nodes):
            kind = getattr(node, "kind", "")
            if kind in {
                "commandsubstitution",
                "processsubstitution",
                "function",
                "parameter",
            }:
                return "shell_dynamic_construct_not_allowed=%s" % kind
            if kind == "operator" and getattr(node, "op", "") == "&":
                return "shell_background_execution_not_allowed"
            if kind == "command":
                words = [
                    str(getattr(part, "word", ""))
                    for part in getattr(node, "parts", [])
                    if getattr(part, "kind", "") == "word"
                ]
                command = _command_basename(words)
                if command in FORBIDDEN_COMMANDS:
                    return "shell_command_not_allowed=%s" % command
                if command in {"python", "python3", "perl", "ruby", "node"}:
                    if any(flag in words[1:] for flag in ("-c", "-e", "--eval")):
                        return "shell_inline_interpreter_not_allowed=%s" % command
        return ""


def normalize_shell_script(script: str) -> str:
    body = str(script or "").replace("\r\n", "\n").strip()
    prefix = "set -euo pipefail"
    if not body.startswith(prefix):
        body = prefix + "\n" + body
    return body + "\n"


def create_shell_artifact(
    *,
    artifact_id: str,
    script: str,
    cwd: str,
    run_as: str,
    timeout: int,
    environment_fingerprint: str,
    declared_changes: list[str],
    rollback: str,
    nonce: str,
    lifetime_minutes: int = 30,
) -> ShellArtifact:
    normalized = normalize_shell_script(script)
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=max(1, min(int(lifetime_minutes), 120))
    )
    return ShellArtifact(
        artifact_id=artifact_id,
        script=normalized,
        sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        interpreter="/bin/bash",
        cwd=str(Path(cwd).expanduser().resolve()),
        run_as=run_as,
        env_allowlist={"PATH": FIXED_PATH, "LANG": "C.UTF-8"},
        timeout=timeout,
        environment_fingerprint=environment_fingerprint,
        declared_changes=list(declared_changes),
        rollback=rollback,
        single_use_nonce=nonce,
        expires_at=expires.isoformat(timespec="seconds"),
        status="awaiting_confirmation",
    )


def artifact_is_expired(artifact: ShellArtifact) -> bool:
    try:
        expires = datetime.fromisoformat(artifact.expires_at)
    except (TypeError, ValueError):
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


def build_shell_environment(artifact: ShellArtifact) -> dict[str, str]:
    environment = {
        key: value
        for key, value in artifact.env_allowlist.items()
        if key in ALLOWED_ENV_KEYS
    }
    environment["PATH"] = FIXED_PATH
    return environment


def _walk_bash_nodes(nodes: Any):
    stack = list(nodes if isinstance(nodes, list) else [nodes])
    while stack:
        node = stack.pop()
        yield node
        for value in vars(node).values():
            if isinstance(value, list):
                stack.extend(
                    item for item in value if hasattr(item, "kind")
                )
            elif hasattr(value, "kind"):
                stack.append(value)


def _command_basename(words: list[str]) -> str:
    for word in words:
        if "=" in word and not word.startswith(("/", "./", "../")):
            key = word.split("=", 1)[0]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
        return os.path.basename(word)
    return ""


def _is_allowed_future_cwd(cwd: Path, allowed: tuple[Path, ...]) -> bool:
    """Permit a dependency-produced cwd during compilation, never execution."""

    try:
        resolved = cwd.resolve(strict=False)
    except OSError:
        return False
    matches = []
    for candidate in allowed:
        try:
            planned = Path(candidate).expanduser().resolve(strict=False)
            resolved.relative_to(planned)
            matches.append(planned)
        except (OSError, ValueError):
            continue
    if not matches:
        return False
    parent = resolved
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir()
