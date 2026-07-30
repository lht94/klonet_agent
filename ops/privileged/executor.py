"""只有协调器可调用的确定性高权限命令执行器。"""

from __future__ import annotations

import subprocess
import threading
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Callable

from klonet_agent.ops.privileged.action_runner import (
    DirectPrivilegedActionRunner,
)
from klonet_agent.ops.privileged.contracts import (
    ExecutionEvidence,
    PrivilegedStep,
    utc_now,
)
from klonet_agent.ops.privileged.shell_artifact import (
    ShellArtifactPolicy,
    artifact_is_expired,
    build_shell_environment,
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
        shell_policy: ShellArtifactPolicy | None = None,
        environment_fingerprint_provider: Callable[[], str] | None = None,
    ) -> None:
        self.max_output_chars = max(1, int(max_output_chars))
        self.on_start = on_start
        self.on_output = on_output
        self._legacy_action_runner = action_runner
        self.action_runner = (
            direct_action_runner or DirectPrivilegedActionRunner()
        )
        self.shell_policy = shell_policy or ShellArtifactPolicy()
        self.environment_fingerprint_provider = (
            environment_fingerprint_provider or (lambda: "")
        )

    def execute(self, step: PrivilegedStep) -> ExecutionEvidence:
        binding = step.execution_binding
        if binding is None:
            return ExecutionEvidence(
                return_code=2,
                stderr="execution_binding_missing",
                started_at=utc_now(),
                finished_at=utc_now(),
                environment_changed=False,
            )
        if binding.kind == "registered_action":
            bound_step = PrivilegedStep.from_dict(step.to_dict())
            bound_step.action = binding.action
            bound_step.args = dict(binding.args)
            bound_step.preconditions = list(binding.preconditions)
            bound_step.postconditions = list(binding.postconditions)
            return self.execute_action(bound_step)
        if binding.kind == "shell_artifact":
            return self.execute_shell_artifact(step)
        return ExecutionEvidence(
            return_code=2,
            stderr="legacy_or_unknown_execution_binding_refused",
            started_at=utc_now(),
            finished_at=utc_now(),
            environment_changed=False,
        )

    def execute_shell_artifact(
        self,
        step: PrivilegedStep,
    ) -> ExecutionEvidence:
        binding = step.execution_binding
        artifact = binding.shell_artifact if binding is not None else None
        started_at = utc_now()
        if artifact is None:
            return ExecutionEvidence(
                return_code=2,
                stderr="shell_artifact_missing",
                started_at=started_at,
                finished_at=utc_now(),
                environment_changed=False,
            )
        if artifact.status != "approved":
            return ExecutionEvidence(
                return_code=2,
                stderr="shell_artifact_not_exactly_approved",
                started_at=started_at,
                finished_at=utc_now(),
                environment_changed=False,
            )
        if (
            not artifact.approved_contract_hash
            or artifact.approved_contract_hash != artifact.contract_hash
        ):
            return ExecutionEvidence(
                return_code=2,
                stderr="shell_artifact_contract_not_exactly_approved",
                started_at=started_at,
                finished_at=utc_now(),
                environment_changed=False,
            )
        if artifact_is_expired(artifact):
            artifact.status = "expired"
            return ExecutionEvidence(
                return_code=2,
                stderr="shell_artifact_expired",
                started_at=started_at,
                finished_at=utc_now(),
                environment_changed=False,
            )
        problem = self.shell_policy.validate(artifact)
        if problem:
            return ExecutionEvidence(
                return_code=2,
                stderr=problem,
                started_at=started_at,
                finished_at=utc_now(),
                environment_changed=False,
            )
        current_fingerprint = self.environment_fingerprint_provider()
        if (
            artifact.environment_fingerprint
            and current_fingerprint != artifact.environment_fingerprint
        ):
            return ExecutionEvidence(
                return_code=2,
                stderr="shell_artifact_environment_fingerprint_changed",
                started_at=started_at,
                finished_at=utc_now(),
                environment_changed=False,
            )
        if hashlib.sha256(
            artifact.script.encode("utf-8")
        ).hexdigest() != artifact.sha256:
            return ExecutionEvidence(
                return_code=2,
                stderr="shell_artifact_changed_after_confirmation",
                started_at=started_at,
                finished_at=utc_now(),
                environment_changed=False,
            )
        with tempfile.TemporaryDirectory(
            prefix="klonet-shell-artifact-"
        ) as temp_dir:
            script_path = Path(temp_dir) / (
                artifact.artifact_id + ".sh"
            )
            script_path.write_text(artifact.script, encoding="utf-8")
            script_path.chmod(0o700)
            argv = [
                artifact.interpreter,
                "--noprofile",
                "--norc",
                str(script_path),
            ]
            current_user = ""
            try:
                import pwd

                current_user = pwd.getpwuid(os.geteuid()).pw_name
            except (ImportError, KeyError, OSError):
                current_user = ""
            if artifact.run_as and artifact.run_as != current_user:
                # A different, explicitly reviewed execution identity needs to
                # traverse and read this short-lived artifact. Scripts cannot
                # contain secrets by policy and the directory disappears
                # immediately after execution.
                Path(temp_dir).chmod(0o711)
                script_path.chmod(0o555)
                argv = ["sudo", "-n", "-u", artifact.run_as, *argv]
            evidence = self._execute(
                step,
                argv,
                shell=False,
                cwd=artifact.cwd,
                env=build_shell_environment(artifact),
                timeout=artifact.timeout,
            )
        artifact.status = (
            "executed"
            if evidence.return_code == 0 and not evidence.timed_out
            else "failed"
        )
        artifact.executed_at = utc_now()
        evidence.environment_changed = True
        return evidence

    def current_environment_fingerprint(self) -> str:
        return str(self.environment_fingerprint_provider() or "")

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
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecutionEvidence:
        started_at = utc_now()
        if self.on_start:
            self.on_start(step.command)
        try:
            process = subprocess.Popen(
                command,
                shell=shell,
                cwd=cwd or step.cwd or None,
                env=env,
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
                process.wait(timeout=timeout or step.timeout)
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
