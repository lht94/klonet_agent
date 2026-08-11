from __future__ import annotations

import pytest


class FakeReadonlyExecutor:
    def __init__(self):
        self.calls = []

    def execute_readonly(self, step, argv):
        from klonet_agent.ops.privileged.contracts import ExecutionEvidence

        self.calls.append((step, argv))
        return ExecutionEvidence(return_code=0, stdout="nginx is active")


def test_validated_readonly_runner_converts_safe_command_to_argv():
    from klonet_agent.ops.privileged.workflow.readonly_runtime import ValidatedReadonlyCommandRunner

    executor = FakeReadonlyExecutor()
    output = ValidatedReadonlyCommandRunner(executor)("systemctl status nginx")

    assert output == "nginx is active"
    assert executor.calls[0][1] == ["systemctl", "status", "nginx"]
    assert executor.calls[0][0].risk == "readonly"


def test_validated_readonly_runner_refuses_mutation_before_executor():
    from klonet_agent.ops.privileged.workflow.readonly_runtime import ValidatedReadonlyCommandRunner

    executor = FakeReadonlyExecutor()

    with pytest.raises(PermissionError, match="deterministically read-only"):
        ValidatedReadonlyCommandRunner(executor)("sudo systemctl restart nginx")

    assert executor.calls == []
