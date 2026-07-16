"""Read-only Docker inspection through the root-owned Ops helper."""

from __future__ import annotations

import re
import subprocess


_SAFE_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def inspect_docker_containers(args: dict | None = None, command_runner=None) -> str:
    values = args or {}
    name = str(values.get("name") or "").strip()
    if name and not _SAFE_CONTAINER_NAME.fullmatch(name):
        return f"Error: invalid_container_name={name}"
    command = [
        "sudo",
        "-n",
        "/usr/local/bin/klonet-agent-op",
        "inspect-docker-containers",
        "--execute",
    ]
    if name:
        command.extend(["--name", name])
    runner = command_runner or _run_command
    try:
        output = runner(command)
    except subprocess.CalledProcessError as exc:
        stderr = " ".join(str(exc.stderr or "").split())
        return f"Error: docker inspection failed: {stderr or f'returncode={exc.returncode}'}"
    except OSError as exc:
        return f"Error: docker inspection unavailable: {exc}"
    return str(output)


def _run_command(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=10,
    )
    return completed.stdout.strip()
