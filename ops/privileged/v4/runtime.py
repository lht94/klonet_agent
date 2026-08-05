"""Runtime adapters shared by V4 production and evaluation assembly."""

from __future__ import annotations

from typing import Any

from klonet_agent.ops.privileged.contracts import PrivilegedStep
from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy


class ValidatedReadonlyCommandRunner:
    """Translate classifier argv text into the executor's validated boundary."""

    def __init__(self, executor: Any) -> None:
        self.executor = executor
        self.policy = PrivilegedRiskPolicy()

    def __call__(self, command: str) -> str:
        argv, reason = self.policy.readonly_argv(command)
        if argv is None:
            raise PermissionError(
                "command is not deterministically read-only: %s" % reason
            )
        step = PrivilegedStep(
            step_id="v4-readonly-command",
            title="deterministic read-only inspection",
            command=command,
            risk="readonly",
            status="approved",
        )
        evidence = self.executor.execute_readonly(step, argv)
        output = evidence.stdout if evidence.return_code == 0 else evidence.stderr
        return str(output or "")
