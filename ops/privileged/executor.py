"""只有协调器可调用的确定性高权限命令执行器。"""

from __future__ import annotations

import subprocess
import threading
from typing import Callable

from klonet_agent.ops.privileged.contracts import (
    ExecutionEvidence,
    PrivilegedStep,
    utc_now,
)


class PrivilegedCommandExecutor:
    """执行一个已授权 Step；不做规划、审批、重试或结果判定。"""

    def __init__(
        self,
        *,
        max_output_chars: int = 12000,
        on_start: Callable[[str], None] | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        self.max_output_chars = max(1, int(max_output_chars))
        self.on_start = on_start
        self.on_output = on_output

    def execute(self, step: PrivilegedStep) -> ExecutionEvidence:
        started_at = utc_now()
        if self.on_start:
            self.on_start(step.command)
        try:
            process = subprocess.Popen(
                step.command,
                shell=True,
                cwd=step.cwd or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            return ExecutionEvidence(
                return_code=127,
                stderr=str(exc),
                started_at=started_at,
                finished_at=utc_now(),
                timed_out=False,
                environment_changed=False,
            )
        timed_out = False
        stdout = ""
        stderr = ""

        if not hasattr(process, "wait"):
            stdout, stderr = process.communicate(timeout=step.timeout)
        else:
            buffers = {"stdout": [], "stderr": []}
            threads = [
                threading.Thread(
                    target=self._drain,
                    args=(process.stdout, "stdout", buffers["stdout"]),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._drain,
                    args=(process.stderr, "stderr", buffers["stderr"]),
                    daemon=True,
                ),
            ]
            for thread in threads:
                thread.start()
            try:
                process.wait(timeout=step.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()
            for thread in threads:
                thread.join(timeout=1)
            stdout = "".join(buffers["stdout"])
            stderr = "".join(buffers["stderr"])

        return ExecutionEvidence(
            return_code=None if timed_out else process.returncode,
            stdout=self._bounded(stdout or ""),
            stderr=self._bounded(stderr or ""),
            started_at=started_at,
            finished_at=utc_now(),
            timed_out=timed_out,
            environment_changed=step.risk != "readonly",
        )

    def _drain(self, stream, channel: str, output: list[str]) -> None:
        if stream is None:
            return
        for chunk in iter(stream.readline, ""):
            output.append(chunk)
            if self.on_output:
                self.on_output(channel, chunk)
        stream.close()

    def _bounded(self, value: str) -> str:
        if len(value) <= self.max_output_chars:
            return value
        marker = "\n... output truncated ...\n"
        keep = max(0, self.max_output_chars - len(marker))
        return value[:keep] + marker
