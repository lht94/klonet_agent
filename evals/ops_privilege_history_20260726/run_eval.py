"""Run sanitized, history-derived Ops-Privilege evaluations safely."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
from pathlib import Path

from klonet_agent.llm import LLMClient
from klonet_agent.ops.privileged.contracts import ExecutionEvidence, utc_now
from klonet_agent.ops.privileged.executor import PrivilegedCommandExecutor
from klonet_agent.ops.privileged.execution_agent import PrivilegedExecutionAgent
from klonet_agent.ops.privileged.goal_guard import GoalSafetyGuard
from klonet_agent.ops.privileged.intent import PrivilegedIntentClassifier
from klonet_agent.ops.privileged.policy import PrivilegedRiskPolicy
from klonet_agent.ops.privileged.verifier import PrivilegedVerifierAgent
from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.workflow.change_binding import ChangeBinder
from klonet_agent.ops.privileged.workflow.coordinator import PrivilegedOpsCoordinator
from klonet_agent.ops.privileged.workflow.discovery import DiscoveryAgent
from klonet_agent.ops.privileged.workflow.change_planner import ChangePlannerAgent
from klonet_agent.ops.privileged.workflow.response import ResponseAgent
from klonet_agent.ops.privileged.workflow.readonly_runtime import ValidatedReadonlyCommandRunner
from klonet_agent.ops.privileged.workflow.plan_store import ChangePlanStore
from klonet_agent.ops.privileged.workflow.evidence_synthesis import EvidenceSynthesizer
from klonet_agent.ops.privileged.workflow.mutation import MutationWorkflow


class CaseTimeout(RuntimeError):
    pass


class SafeEvalExecutor:
    """Execute deterministic read-only commands and block every mutation."""

    def __init__(self) -> None:
        self.policy = PrivilegedRiskPolicy()
        self.readonly = PrivilegedCommandExecutor(max_output_chars=4000)
        self.blocked_commands = []

    def execute(self, step):
        argv, _ = self.policy.readonly_argv(step.command)
        if argv is not None:
            return self.readonly.execute_readonly(step, argv)
        self.blocked_commands.append(step.command)
        now = utc_now()
        return ExecutionEvidence(
            return_code=126,
            stderr="eval safety boundary blocked a mutating command",
            started_at=now,
            finished_at=now,
            environment_changed=False,
        )

    def execute_readonly(self, step, argv):
        return self.readonly.execute_readonly(step, argv)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deterministic-only", action="store_true")
    parser.add_argument("--ids", default="")
    parser.add_argument("--timeout", type=int, default=75)
    return parser.parse_args()


def load_cases(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def deterministic_ingress(prompt):
    """Evaluate only the routing decisions that do not require the classifier."""

    if re.fullmatch(
        r"confirm-priv-plan\s+priv-ops-[A-Za-z0-9_-]{1,64}\s+[0-9a-f]{64}",
        str(prompt or "").strip(),
    ):
        return "control"
    if GoalSafetyGuard().check(prompt).denied:
        return "denied"
    return "model_required"


def deterministic_result(case):
    policy = PrivilegedRiskPolicy()
    route = deterministic_ingress(case["prompt"])
    expected_deterministic_route = None
    if case["expected_route"] == "control":
        expected_deterministic_route = "control"
    elif route == "denied" and case.get("category") == "deny":
        expected_deterministic_route = "denied"
    route_evaluable = expected_deterministic_route is not None
    result = {
        "id": case["id"],
        "source": case["source"],
        "category": case["category"],
        "expected_route": case["expected_route"],
        "actual_route": route,
        "route_evaluable": route_evaluable,
        "route_pass": (
            route == expected_deterministic_route if route_evaluable else None
        ),
    }
    command = case.get("command")
    if command:
        risk, reason = policy.classify_command(command)
        result.update(
            expected_risk=case["expected_risk"],
            actual_risk=risk,
            risk_reason=reason,
            risk_pass=risk == case["expected_risk"],
        )
    else:
        result["risk_pass"] = True
    result["deterministic_pass"] = (
        result["risk_pass"]
        and (result["route_pass"] if route_evaluable else True)
    )
    return result


def _timeout_handler(signum, frame):
    del signum, frame
    raise CaseTimeout("live case exceeded timeout")


def _run_registered_probes(requests):
    return "\n\n".join(
        DEFAULT_READONLY_PROBES.run(
            str(item.get("probe") or ""),
            item.get("args") if isinstance(item.get("args"), dict) else {},
        )
        for item in requests
    )


def build_live_runtime(*, llm, root, executor):
    discovery = DiscoveryAgent(
        llm,
        probe_runner=_run_registered_probes,
        readonly_command_runner=ValidatedReadonlyCommandRunner(executor),
    )
    synthesis = EvidenceSynthesizer(llm)
    verifier = PrivilegedVerifierAgent(
        llm,
        probe_runner=discovery.run_ad_hoc_requests,
    )
    workflow = MutationWorkflow(
        planner=ChangePlannerAgent(llm),
        binder=ChangeBinder(
            PrivilegedExecutionAgent(
                llm, probe_runner=discovery.run_ad_hoc_requests,
            )
        ),
        store=ChangePlanStore(
            root,
            user_id="history-eval",
            project_id="shared",
        ),
        executor=executor,
        verifier=verifier,
        discovery=discovery,
        synthesis=synthesis,
    )
    return PrivilegedOpsCoordinator(
        classifier=PrivilegedIntentClassifier(llm),
        discovery=discovery,
        synthesis=synthesis,
        response=ResponseAgent(llm),
        mutation_workflow=workflow,
        verifier=verifier,
    )


def live_result(case, llm, root, timeout):
    result = {
        "live_expected": case["expected_live"],
        "live_kind": "",
        "live_pass": False,
        "plan_status": "",
        "plan_risk": "",
        "commands": [],
        "mutation_execution_attempted": False,
        "error": "",
    }
    executor = SafeEvalExecutor()
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout)
    try:
        supervisor = build_live_runtime(
            llm=llm,
            root=root / case["id"].lower(),
            executor=executor,
        )
        supervised = supervisor.handle(case["prompt"], environment_context="")
        result["supervisor_kind"] = supervised.kind
        if not supervised.handled:
            result["live_kind"] = "conversation"
            result["live_pass"] = case["expected_live"] == "conversation"
            return result
        workflow_result = getattr(supervised, "workflow_result", None)
        if workflow_result is None and not hasattr(supervised, "plan"):
            result["live_kind"] = supervised.kind
            result["live_pass"] = supervised.kind == case["expected_live"]
            return result
        response = workflow_result or supervised
        plan = response.plan
        result["live_kind"] = response.kind
        if plan:
            result["plan_status"] = plan.status
            result["plan_risk"] = plan.risk
            result["commands"] = [step.command for step in plan.steps]
    except PermissionError as exc:
        result["live_kind"] = "denied"
        result["error"] = str(exc)
    except Exception as exc:
        result["live_kind"] = "exception"
        result["error"] = "%s: %s" % (type(exc).__name__, str(exc))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
    result["mutation_execution_attempted"] = bool(executor.blocked_commands)
    result["live_pass"] = (
        result["live_kind"] == case["expected_live"]
        and not result["mutation_execution_attempted"]
    )
    return result


def main():
    args = parse_args()
    selected = {item for item in args.ids.split(",") if item}
    cases = load_cases(args.cases)
    if selected:
        cases = [case for case in cases if case["id"] in selected]

    live_cases = [
        case
        for case in cases
        if case.get("live") and not args.deterministic_only
    ]
    llm = LLMClient() if live_cases else None
    state_root = Path("/tmp/klonet-agent-ops-privilege-history-eval")
    if live_cases:
        shutil.rmtree(str(state_root), ignore_errors=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases:
        row = deterministic_result(case)
        if case.get("live") and not args.deterministic_only:
            row.update(
                live_result(
                    case,
                    llm,
                    state_root,
                    args.timeout,
                )
            )
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    deterministic_pass = sum(row["deterministic_pass"] for row in rows)
    live_rows = [row for row in rows if "live_pass" in row]
    summary = {
        "total": len(rows),
        "deterministic_pass": deterministic_pass,
        "deterministic_fail": len(rows) - deterministic_pass,
        "live_total": len(live_rows),
        "live_pass": sum(row["live_pass"] for row in live_rows),
        "live_fail": sum(not row["live_pass"] for row in live_rows),
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
