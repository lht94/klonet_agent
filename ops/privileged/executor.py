"""只有协调器可调用的确定性高权限命令执行器。"""

from __future__ import annotations

import subprocess
import threading
from typing import Callable

from klonet_agent.ops.privileged.action_runner import (
    DirectPrivilegedActionRunner,
)
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
        action_runner: Callable | None = None,
        direct_action_runner: Callable | None = None,
    ) -> None:
        self.max_output_chars = max(1, int(max_output_chars))
        self.on_start = on_start
        self.on_output = on_output
        self._legacy_action_runner = action_runner
        self.action_runner = (
            direct_action_runner or DirectPrivilegedActionRunner()
        )

    def execute(self, step: PrivilegedStep) -> ExecutionEvidence:
        if step.action:
            return self.execute_action(step)
        return self._execute(step, step.command, shell=True)

    def execute_action(self, step: PrivilegedStep) -> ExecutionEvidence:
        """Dispatch one validated registered action without a shell boundary."""

        started_at = utc_now()
        if self.on_start:
            self.on_start("正在执行已确认步骤：%s" % step.title)
        try:
            if self._legacy_action_runner is not None:
                # Compatibility for injected test/integration runners created
                # before Ops-Privilege gained its own direct backend. The
                # production default never enters this branch.
                from klonet_agent.ops.operations import OperationPlan, OperationStep

                operation_step = OperationStep(
                    step_id=step.step_id,
                    title=step.title,
                    purpose=step.title,
                    action=step.action,
                    args=dict(step.args),
                    recipe_id=step.action,
                    recipe_args=dict(step.args),
                )
                operation_plan = OperationPlan(
                    plan_id="privileged-action",
                    operation="deploy_platform",
                    target=str(step.args.get("platform") or ""),
                    objective=step.title,
                    steps=[operation_step],
                )
                result = self._legacy_action_runner(
                    operation_plan,
                    operation_step,
                )
            else:
                result = self.action_runner(step)
        except Exception as exc:
            return ExecutionEvidence(
                return_code=1,
                stderr=str(exc),
                started_at=started_at,
                finished_at=utc_now(),
                environment_changed=False,
            )
        output = self._bounded(str(result.output or ""))
        return_code = 0 if result.status == "completed" else (
            2 if result.status == "blocked" else 1
        )
        changed = (
            step.risk != "readonly"
            and "environment unchanged" not in output.lower()
            and "environment_changed=false" not in output.lower()
            and "dry_run=true" not in output.lower()
        )
        return ExecutionEvidence(
            return_code=return_code,
            stdout=output if return_code == 0 else "",
            stderr=output if return_code != 0 else "",
            started_at=started_at,
            finished_at=utc_now(),
            timed_out=False,
            environment_changed=changed,
        )

    def execute_readonly(
        self,
        step: PrivilegedStep,
        argv: list[str],
    ) -> ExecutionEvidence:
        """Execute prevalidated arguments without shell interpretation."""

        return self._execute(step, list(argv), shell=False)

    def _execute(
        self,
        step: PrivilegedStep,
        command,
        *,
        shell: bool,
    ) -> ExecutionEvidence:
        started_at = utc_now()
        if self.on_start:
            self.on_start(step.command)
        try:
            process = subprocess.Popen(
                command,
                shell=shell,
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
