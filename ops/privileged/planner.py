"""Agentic semantic Planner for Ops-Privilege."""

from __future__ import annotations

import ast
import json
import re
import textwrap
import uuid
from pathlib import Path
from typing import Any, Callable

from klonet_agent.ops.privileged.context import GroundedPlanContext
from klonet_agent.ops.privileged.contracts import (
    PlanResource,
    PrivilegedPlan,
    PrivilegedStep,
    RISK_LEVELS,
)
from klonet_agent.ops.privileged.planner_schema import (
    PROCESS_TERMINATING_SIGNALS,
    normalize_process_signal,
    normalize_semantic_risk,
)
from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES


MAX_PLANNER_INVALID_REPAIRS = 3


PLANNER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Planner.
You plan operations for the Klonet project. An unqualified "平台" means the
Klonet platform unless the user or conversation explicitly names another
product. Never replace an unknown target with a generic Nginx/demo web stack.
You own the operational route. Runbooks are experience, not mandatory workflows.
Do not select registered Action names and do not emit commands or scripts.

Return one JSON object with status exactly "need_evidence", "ready", or
"blocked".

When facts are missing but discoverable, return:
{"status":"need_evidence","probe_requests":[
 {"probe":"registered probe name","args":{},"purpose":"why this fact matters"}
]}
Use at most 4 probes in one round. Probe arguments must come from existing
evidence. Never request mutation.
Planning feedback may contain evidence_already_collected from an earlier
execution-binding attempt. Treat it as authoritative. Do not request the same
probe with the same arguments again unless the feedback explicitly reports
that the earlier probe was unavailable or the relevant state has changed.

When ready, return:
{
 "status":"ready",
 "goal":"the actual user goal",
 "assumptions":[],
 "resources":[{
   "name":"stable_name such as instance_root or master_port",
   "kind":"path|port|url|identifier|string",
   "status":"frozen|deferred",
   "role":"semantic role such as platform_runtime_root or source_repo_root",
   "value":"exact value for frozen; null for deferred",
   "source":"user_input|environment_evidence|derived",
   "reason":"why this value is deferred, otherwise empty",
   "resolve_before":"semantic step id that needs a deferred value, otherwise empty",
   "consumers":["semantic_step_id.argument_name"]
 }],
 "steps":[{
   "step_id":"stable id",
   "title":"concise Chinese title",
   "objective":"state this step must achieve",
   "reason":"why this route follows from evidence",
   "evidence_refs":[],
   "depends_on":[],
   "expected_effects":[],
   "success_criteria":[],
   "risk_suggestion":"readonly|low|medium|high|destructive"
 }]
}
Normally return 2-6 steps and never more than 8. Every step needs an objective,
reason, and at least one observable success criterion. Dependencies may reference
only earlier steps. Do not invent paths, platform names, services, hosts or
ports. If a material choice belongs to the user and cannot be discovered, use
status="blocked" with a concise reason and missing_decisions array.

Freeze shared implementation resources once at plan creation. Paths, instance
names, ports, session prefixes, and routes that are known or safely derived
must be status=frozen. A value that is genuinely unavailable, such as an
unspecified Git remote URL, may be status=deferred with a concrete reason and
the semantic step id before which it must be resolved. Never substitute an old
instance's value for a deferred resource. consumers lock a resource to an
argument in the later implementation, for example copy.destination and
start.project_root. Do not add consumers for parameters that do not directly
carry that exact value.
Every path resource must have an unambiguous role. In particular,
platform_runtime_root, source_repo_root, config_file and runtime_cwd are
different roles even when two values happen to share a parent directory.
For a new Klonet instance beside an existing one, freeze a distinct
instance_root (for example platform_runtime_root/instance_name), prepare/copy
the source into it before configuration or startup, and bind runtime startup
through the platform root plus instance name. Do not mutate the existing
source_repo_root's active config as the new instance's isolated config.
Nginx syntax may be configured before startup, but HTTP/proxy health must only
be verified after the backend-start semantic step on which it depends.

Never include passwords. You have no mutation tools. An independent Execution
Agent will map semantic steps to registered Actions or frozen one-time shell
artifacts after planning.
Treat Structured environment facts as the authoritative resource model. Never
use source_repo_root as project_root when the model gives a different
platform_root. runtime_cwd is the only valid start project_root. Secret facts
contain metadata only and must never be expanded, requested, copied, or echoed.
""".strip()

MAX_PRIVILEGED_PLAN_STEPS = 8
MAX_PLANNING_PROBE_ROUNDS = 3


class PlanningBlocked(Exception):
    """The Planner needs a material user decision that probes cannot resolve."""


class PrivilegedPlannerAgent:
    def __init__(
        self,
        llm: Any,
        policy: Any | None = None,
        action_registry: Any | None = None,
        probe_runner: Any | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.llm = llm
        # Retained only as constructor compatibility. The semantic Planner
        # deliberately cannot inspect execution implementation names.
        del policy, action_registry
        self.probe_runner = probe_runner
        self.on_progress = on_progress

    def plan(
        self,
        goal: str,
        *,
        environment_context: str = "",
        grounded_context: GroundedPlanContext | None = None,
        conversation_context: str = "",
        planning_feedback: str = "",
        prior_probe_history: list[dict[str, Any]] | None = None,
    ) -> PrivilegedPlan:
        planning_context = (
            grounded_context.render()
            if grounded_context is not None
            else (
                "## Read-only server evidence\n%s\n\n## Registered probes\n%s"
                % (
                    environment_context or "(none)",
                    DEFAULT_READONLY_PROBES.render(),
                )
            )
        )
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Recent conversation (use only to resolve references and"
                    " continuations; the current request has priority):\n%s\n\n"
                    "Current request:\n%s\n\n"
                    "Feedback from a previous execution-binding attempt"
                    " (if any):\n%s\n\n"
                    "Grounded planning context:\n%s"
                    % (
                        conversation_context or "(none)",
                        goal,
                        planning_feedback or "(none)",
                        planning_context,
                    )
                ),
            },
        ]
        data = None
        last_error = ""
        probe_history: list[dict[str, Any]] = []
        probe_rounds = 0
        invalid_repairs = 0
        duplicate_probe_repairs = 0
        known_probe_keys = _probe_history_keys(prior_probe_history or [])
        while True:
            self._progress(
                "规划节点：Planner 正在综合目标与证据%s…"
                % (
                    "（只读补证第 %s 轮后）" % probe_rounds
                    if probe_rounds
                    else ""
                )
            )
            response = self._complete(messages)
            content = response.choices[0].message.content or ""
            try:
                data = _parse_json_object(content)
                status = str(data.get("status") or "").strip().lower()
                if status == "need_evidence":
                    if probe_rounds >= MAX_PLANNING_PROBE_ROUNDS:
                        raise ValueError(
                            "planning probe round limit reached without a plan"
                        )
                    requests = self._validate_probe_requests(
                        data.get("probe_requests")
                    )
                    duplicate_names = [
                        item["probe"]
                        for item in requests
                        if _probe_request_key(item) in known_probe_keys
                    ]
                    requests = [
                        item
                        for item in requests
                        if _probe_request_key(item) not in known_probe_keys
                    ]
                    if duplicate_names:
                        self._progress(
                            "规划结论：已拒绝重复只读检查（%s），将复用证据账本。"
                            % "、".join(dict.fromkeys(duplicate_names))
                        )
                    if not requests:
                        if duplicate_probe_repairs >= 1:
                            raise ValueError(
                                "Planner repeatedly requested probes already"
                                " present in the evidence ledger"
                            )
                        duplicate_probe_repairs += 1
                        messages.append({"role": "assistant", "content": content})
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "All requested probes duplicate authoritative"
                                    " evidence_already_collected. Do not request"
                                    " them again. Use that evidence and return ready"
                                    " or blocked, or request only materially different"
                                    " probe arguments."
                                ),
                            }
                        )
                        continue
                    self._progress(
                        "规划结论：还缺少可探测事实，准备执行 %s 个只读检查（%s）。"
                        % (
                            len(requests),
                            "、".join(item["probe"] for item in requests),
                        )
                    )
                    if self.probe_runner is None:
                        raise ValueError(
                            "planner requested evidence but probe runner is unavailable"
                        )
                    evidence = self.probe_runner(requests)
                    known_probe_keys.update(
                        _probe_request_key(item) for item in requests
                    )
                    probe_history.append(
                        {
                            "round": probe_rounds + 1,
                            "requests": requests,
                            "evidence": str(evidence)[:18000],
                        }
                    )
                    probe_rounds += 1
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Read-only probe evidence for round %s:\n%s\n\n"
                                "Continue planning. Request more evidence only if"
                                " materially necessary."
                                % (probe_rounds, str(evidence)[:18000])
                            ),
                        }
                    )
                    continue
                if status == "blocked":
                    self._progress(
                        "规划结论：存在无法由服务器探测替代的用户决定。"
                    )
                    reason = str(data.get("reason") or "缺少必要决策").strip()
                    missing = _string_list(data.get("missing_decisions"))
                    raise PlanningBlocked(
                        "%s%s"
                        % (
                            reason,
                            "；需要确认：" + "；".join(missing)
                            if missing
                            else "",
                        )
                    )
                if status != "ready":
                    raise ValueError("planner status must be need_evidence, ready, or blocked")
                steps = self._build_semantic_steps(data)
                resources = self._build_plan_resources(data, steps)
                if _requires_plan_resource_manifest(
                    str(data.get("goal") or goal),
                    steps,
                ):
                    if not any(
                        resource.kind == "path" for resource in resources
                    ):
                        raise ValueError(
                            "mutating deployment plan requires a path resource manifest"
                        )
                    resource_text = " ".join(
                        "%s %s" % (step.title, step.objective)
                        for step in steps
                    ).lower()
                    if re.search(r"端口|\bports?\b", resource_text) and not any(
                        resource.kind == "port" for resource in resources
                    ):
                        raise ValueError(
                            "deployment plan that assigns ports requires frozen port resources"
                        )
                    _validate_deployment_plan_shape(
                        str(data.get("goal") or goal),
                        steps,
                        resources,
                        grounded_context=grounded_context,
                    )
                self._progress(
                    "规划结论：已形成 %s 个语义步骤，开始匹配安全执行能力。"
                    % len(steps)
                )
                break
            except (KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
                if invalid_repairs >= MAX_PLANNER_INVALID_REPAIRS:
                    raise ValueError(
                        "Planner did not return a valid semantic plan: %s"
                        % last_error
                    )
                invalid_repairs += 1
                self._progress(
                    "规划节点：Planner 返回格式不完整（%s），正在请求结构化修复…"
                    % str(last_error)[:240]
                )
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Repair only the invalid JSON/schema response. Do not"
                            " switch to a canned workflow. Error: %s" % last_error
                        ),
                    }
                )
            except PlanningBlocked:
                raise

        if data is None:
            raise ValueError(
                "Planner did not return a semantic plan: %s" % last_error
            )

        risk = _effective_declared_risk(
            _highest_risk(steps),
            data.get("risk"),
        )
        plan = PrivilegedPlan(
            plan_id="priv-" + uuid.uuid4().hex[:10],
            goal=str(data.get("goal") or goal).strip(),
            risk=risk,
            steps=steps,
            resources=resources,
            verification_level="semantic",
            status="draft",
            grounding=_grounding_summary(
                grounded_context,
                "llm_agentic_v3",
            ),
            assumptions=_string_list(data.get("assumptions")),
            probe_history=probe_history,
        )
        return plan

    @staticmethod
    def _build_plan_resources(
        data: dict[str, Any],
        steps: list[PrivilegedStep],
    ) -> list[PlanResource]:
        raw = data.get("resources", [])
        if raw is None:
            raw = []
        if not isinstance(raw, list) or len(raw) > 40:
            raise ValueError("resources must be an array with at most 40 items")
        step_ids = {step.step_id for step in steps}
        resources = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("plan resource must be an object")
            consumers = _string_list(item.get("consumers"))
            for consumer in consumers:
                semantic_id = consumer.rsplit(".", 1)[0]
                if semantic_id not in step_ids:
                    raise ValueError(
                        "plan resource consumer references unknown step: %s"
                        % consumer
                    )
            resource = PlanResource(
                name=str(item.get("name") or "").strip(),
                kind=str(item.get("kind") or "string").strip().lower(),
                status=str(item.get("status") or "").strip().lower(),
                role=_resource_role(item),
                value=item.get("value"),
                source=str(item.get("source") or "").strip()[:200],
                reason=str(item.get("reason") or "").strip()[:1000],
                resolve_before=str(
                    item.get("resolve_before") or ""
                ).strip()[:100],
                consumers=consumers,
            )
            for step in steps:
                text = "%s %s" % (step.title, step.objective)
                copy_like = bool(
                    re.search(
                        r"复制|克隆|git|\bcopy\b|\bclone\b",
                        text.lower(),
                    )
                )
                create_like = bool(
                    re.search(
                        r"创建.{0,8}目录|新建.{0,8}目录|\bcreate.{0,8}director",
                        text.lower(),
                    )
                )
                implicit_args: tuple[str, ...] = ()
                resource_role = resource.role or resource.name
                if resource.name == "instance_name" or resource_role == "instance_name":
                    implicit_args = ("platform",)
                elif resource.name == "instance_root" or resource_role == "instance_root":
                    implicit_args = ("project_root",)
                    if copy_like:
                        implicit_args += ("destination", "repository")
                    if create_like:
                        implicit_args += ("path",)
                elif (
                    resource.name == "source_root"
                    or resource_role == "source_repo_root"
                ) and copy_like:
                    implicit_args = ("source",)
                elif (
                    resource.name in {"git_remote", "repository_url"}
                    and copy_like
                ):
                    implicit_args = ("url",)
                elif resource.kind == "port":
                    implicit_args = (resource.name,)
                for arg_name in implicit_args:
                    step_id = step.step_id
                    consumer = "%s.%s" % (step_id, arg_name)
                    if consumer not in resource.consumers:
                        resource.consumers.append(consumer)
            if resource.name == "instance_root" or resource.role == "instance_root":
                for step in steps:
                    text = "%s %s" % (step.title, step.objective)
                    if re.search(
                        r"启动|复制|克隆|创建.{0,8}目录|\bstart\b|\bcopy\b|\bclone\b|\bcreate.{0,8}director",
                        text.lower(),
                    ):
                        consumer = "%s.script" % step.step_id
                        if consumer not in resource.consumers:
                            resource.consumers.append(consumer)
            if resource.name in {"git_remote", "repository_url"}:
                for step in steps:
                    text = "%s %s" % (step.title, step.objective)
                    if re.search(
                        r"克隆|复制|git|\bclone\b|\bcopy\b",
                        text.lower(),
                    ):
                        consumer = "%s.script" % step.step_id
                        if consumer not in resource.consumers:
                            resource.consumers.append(consumer)
            if (
                resource.status == "deferred"
                and resource.resolve_before not in step_ids
            ):
                raise ValueError(
                    "deferred resource resolve_before must reference a semantic step"
                )
            resources.append(resource)
        names = [item.name for item in resources]
        if len(set(names)) != len(names):
            raise ValueError("duplicate plan resource name")
        # destination is a semantic role, not an LLM naming choice. Prefer the
        # frozen instance root and discard aliases that incorrectly claim the
        # same destination consumer.
        instance_consumers = {
            consumer
            for resource in resources
            for consumer in resource.consumers
            if consumer.endswith((".destination", ".project_root"))
        }
        for consumer in instance_consumers:
            claimants = [
                resource for resource in resources
                if consumer in resource.consumers
            ]
            preferred = next(
                (
                    resource for resource in claimants
                    if resource.role == "instance_root"
                    or resource.name == "instance_root"
                ),
                None,
            )
            if preferred is not None:
                for resource in claimants:
                    if resource is not preferred:
                        resource.consumers.remove(consumer)
        owners: dict[str, PlanResource] = {}
        for resource in resources:
            retained_consumers = []
            for consumer in resource.consumers:
                previous = owners.setdefault(consumer, resource)
                if previous.name == resource.name:
                    retained_consumers.append(consumer)
                    continue
                same_value = (
                    str(Path(str(previous.value)).expanduser())
                    == str(Path(str(resource.value)).expanduser())
                    if previous.kind == resource.kind == "path"
                    else previous.value == resource.value
                )
                if not same_value:
                    if (
                        consumer.endswith(".source")
                        and previous.kind == resource.kind == "path"
                    ):
                        try:
                            previous_exists = Path(
                                str(previous.value)
                            ).is_dir()
                        except OSError:
                            previous_exists = False
                        try:
                            current_exists = Path(str(resource.value)).is_dir()
                        except OSError:
                            current_exists = False
                        if previous_exists != current_exists:
                            if current_exists:
                                previous.consumers = [
                                    item for item in previous.consumers
                                    if item != consumer
                                ]
                                owners[consumer] = resource
                                retained_consumers.append(consumer)
                            continue
                    raise ValueError(
                        "plan resource consumer has conflicting owners: %s "
                        "%s=%s vs %s=%s"
                        % (
                            consumer,
                            previous.name,
                            previous.value,
                            resource.name,
                            resource.value,
                        )
                    )
                # Equal-value aliases are harmless, but the persisted contract
                # keeps a single canonical owner for deterministic injection.
                if previous is resource:
                    retained_consumers.append(consumer)
            resource.consumers = retained_consumers
        return resources

    def _progress(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def _build_semantic_steps(
        self,
        data: dict[str, Any],
    ) -> list[PrivilegedStep]:
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("steps must be a non-empty array")
        if len(raw_steps) > MAX_PRIVILEGED_PLAN_STEPS:
            raise ValueError(
                "steps exceeds maximum of %s" % MAX_PRIVILEGED_PLAN_STEPS
            )
        steps: list[PrivilegedStep] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_steps, start=1):
            if not isinstance(item, dict):
                raise ValueError("semantic step must be an object")
            if any(
                key in item
                for key in (
                    "action", "args", "command", "shell", "script",
                    "executable",
                )
            ):
                raise ValueError(
                    "semantic Planner must not choose execution implementation"
                )
            step_id = str(
                item.get("step_id") or "step-%s" % index
            ).strip()
            if not step_id or step_id in seen:
                raise ValueError("duplicate or empty semantic step_id")
            objective = str(item.get("objective") or "").strip()
            reason = str(item.get("reason") or "").strip()
            criteria = _string_list(item.get("success_criteria"))
            if not objective or not reason or not criteria:
                raise ValueError(
                    "semantic step requires objective, reason, and success_criteria"
                )
            dependencies = _string_list(item.get("depends_on"))
            unknown = [item for item in dependencies if item not in seen]
            if unknown:
                raise ValueError(
                    "semantic dependency must reference an earlier step: %s"
                    % ",".join(unknown)
                )
            risk = normalize_semantic_risk(
                item.get("risk_suggestion"),
                "medium",
            )
            if risk not in RISK_LEVELS:
                raise ValueError("invalid semantic risk suggestion: %s" % risk)
            seen.add(step_id)
            steps.append(
                PrivilegedStep(
                    step_id=step_id,
                    title=str(item.get("title") or objective).strip(),
                    objective=objective,
                    reason=reason,
                    evidence_refs=_string_list(item.get("evidence_refs")),
                    depends_on=dependencies,
                    success_criteria=criteria,
                    risk=risk,
                    expected_changes=_string_list(
                        item.get("expected_effects")
                    ),
                    status="pending",
                )
            )
        return steps

    @staticmethod
    def _validate_probe_requests(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise ValueError("need_evidence requires probe_requests")
        known = {
            spec.name for spec in DEFAULT_READONLY_PROBES.describe()
        }
        result = []
        for item in value[:4]:
            if not isinstance(item, dict):
                raise ValueError("probe request must be an object")
            name = str(item.get("probe") or "").strip()
            if name not in known:
                raise ValueError("planning probe is not registered: %s" % name)
            args = item.get("args")
            if not isinstance(args, dict):
                args = {}
            result.append(
                {
                    "probe": name,
                    "args": args,
                    "purpose": str(item.get("purpose") or "").strip()[:500],
                }
            )
        return result

    def _complete(self, messages: list[dict[str, str]]) -> Any:
        try:
            return self.llm.complete(
                messages=messages,
                tools=None,
                reasoning_effort="high",
                temperature=0,
                response_format={"type": "json_object"},
            )
        except TypeError:
            return self.llm.complete(messages=messages, tools=None)

def _resource_role(item: dict[str, Any]) -> str:
    explicit = str(item.get("role") or "").strip()
    if explicit:
        return explicit[:100]
    name = str(item.get("name") or "").strip()
    known = {
        "project_root": "platform_runtime_root",
        "runtime_cwd": "runtime_cwd",
        "source_repo_root": "source_repo_root",
        "config_path": "config_file",
        "nginx_config_path": "nginx_config_file",
        "instance_root": "platform_instance_root",
    }
    return known.get(name, name)[:100]


def _default_action_postconditions(
    action: str,
    args: dict[str, Any],
) -> list[dict[str, Any]]:
    if action == "write_ops_file":
        return [
            {
                "checker": "file_contains",
                "args": {
                    "path": args.get("path"),
                    "text": args.get("content"),
                },
            }
        ]
    if action == "replace_text_in_file":
        return [
            {
                "checker": "file_contains",
                "args": {
                    "path": args.get("path"),
                    "text": args.get("new_text"),
                },
            }
        ]
    if action == "insert_text_before_anchor":
        return [
            {
                "checker": "file_contains",
                "args": {
                    "path": args.get("path"),
                    "text": args.get("content"),
                },
            }
        ]
    if action == "edit_text_file":
        return [
            {
                "checker": "file_contains",
                "args": {
                    "path": args.get("path"),
                    "text": args.get("content"),
                },
            }
        ]
    if action == "upsert_python_class":
        return [
            {
                "checker": "file_contains",
                "args": {
                    "path": args.get("path"),
                    "text": "class %s(" % args.get("class_name"),
                },
            }
        ]
    if action == "merge_json_file":
        return [
            {
                "checker": "json_file_valid",
                "args": {"path": args.get("path")},
            }
        ]
    if action == "start_redis_instance":
        return [
            {
                "checker": "port_listening",
                "args": {
                    "host": "127.0.0.1",
                    "port": args.get("expected_port"),
                },
            }
        ]
    if action in {"create_directory", "sync_directory", "extract_archive"}:
        path = (
            args.get("path")
            or args.get("destination")
            or args.get("destination_dir")
        )
        return [{"checker": "file_exists", "args": {"path": path}}]
    if action == "prepare_project_files":
        root = Path(str(args.get("project_root") or ""))
        return [
            {
                "checker": "file_exists",
                "args": {"path": str(root / name)},
            }
            for name in (
                "gun.py",
                "master_main.py",
                "celery_worker.py",
                "web_terminal_main.py",
                "worker_gun.py",
                "worker_main.py",
            )
        ]
    if action == "copy_files":
        destination = Path(str(args.get("destination") or ""))
        sources = args.get("sources") if isinstance(args.get("sources"), list) else []
        return [
            {
                "checker": "file_exists",
                "args": {"path": str(destination / Path(source).name)},
            }
            for source in sources
        ]
    if action == "move_path":
        return [
            {
                "checker": "file_exists",
                "args": {"path": args.get("destination")},
            },
            {
                "checker": "file_absent",
                "args": {"path": args.get("source")},
            },
        ]
    if action == "remove_path":
        return [
            {
                "checker": "file_absent",
                "args": {"path": args.get("path")},
            }
        ]
    if action == "manage_service":
        operation = str(args.get("operation") or "")
        checker = "service_inactive" if operation == "stop" else "service_active"
        return [
            {
                "checker": checker,
                "args": {"service": args.get("service")},
            }
        ] if operation not in {"enable", "disable"} else []
    if action == "install_nginx_config":
        return [
            {
                "checker": "file_exists",
                "args": {
                    "path": "/etc/nginx/sites-available/%s"
                    % args.get("config_name"),
                },
            }
        ]
    if action == "reload_nginx":
        return [
            {"checker": "nginx_config_valid", "args": {}},
            {
                "checker": "service_active",
                "args": {"service": "nginx"},
            },
        ]
    if action in {"start_docker_container", "manage_container"}:
        operation = str(args.get("operation") or "start")
        if operation == "set_restart_policy":
            return [
                {
                    "checker": "container_restart_policy",
                    "args": {
                        "container": args.get("name"),
                        "policy": args.get("restart_policy"),
                    },
                }
            ]
        checker = "container_absent" if operation == "remove" else "container_running"
        return [
            {
                "checker": checker,
                "args": {
                    "container": args.get("name"),
                },
            }
        ]
    if action == "manage_docker_network":
        operation = str(args.get("operation") or "")
        if operation in {"connect", "disconnect"}:
            return [
                {
                    "checker": "docker_network_attachment",
                    "args": {
                        "network": args.get("network"),
                        "container": args.get("container"),
                        "attached": operation == "connect",
                    },
                }
            ]
        return [
            {
                "checker": "docker_network_state",
                "args": {
                    "network": args.get("network"),
                    "present": operation != "remove",
                },
            }
        ]
    if action == "manage_docker_image":
        operation = str(args.get("operation") or "")
        image = (
            args.get("expected_image")
            if operation == "load"
            else args.get("image")
        )
        if not image:
            return []
        return [
            {
                "checker": "docker_image_state",
                "args": {
                    "image": image,
                    "present": operation != "remove",
                },
            }
        ]
    if action == "manage_network_link":
        operation = str(args.get("operation") or "")
        return [
            {
                "checker": "network_link_state",
                "args": {
                    "name": args.get("name"),
                    "state": "absent" if operation == "delete" else operation,
                },
            }
        ]
    if action == "manage_libvirt_domain":
        states = {
            "start": "running",
            "reboot": "running",
            "shutdown": "shut off",
            "undefine": "absent",
        }
        expected = states.get(str(args.get("operation") or ""))
        return [
            {
                "checker": "libvirt_domain_state",
                "args": {
                    "domain": args.get("domain"),
                    "state": expected,
                },
            }
        ] if expected else []
    if action == "stop_klonet_runtime_instance":
        ports = args.get("ports")
        if not isinstance(ports, list):
            return []
        return [
            {
                "checker": "port_not_listening",
                "args": {"port": port},
            }
            for port in ports
        ]
    if action == "manage_process":
        if normalize_process_signal(args.get("signal")) in PROCESS_TERMINATING_SIGNALS:
            return [
                {
                    "checker": "process_pid_absent",
                    "args": {"pid": args.get("pid")},
                }
            ]
        return []
    if action == "manage_ovs_resource":
        return [
            {
                "checker": "ovs_resource_state",
                "args": {
                    "resource_type": args.get("resource_type"),
                    "name": args.get("name"),
                    "present": args.get("operation") == "add",
                },
            }
        ]
    if action in {
        "start_screen_component",
        "restart_screen_component",
    }:
        return [
            {
                "checker": "screen_session_exists",
                "args": {"session": args.get("screen_session")},
            }
        ]
    if action == "stop_screen_component":
        return [
            {
                "checker": "screen_session_absent",
                "args": {"session": args.get("screen_session")},
            }
        ]
    if action in {"start_platform_screens", "stop_platform_screens"}:
        platform = str(args.get("platform") or "")
        checker = (
            "screen_session_exists"
            if action == "start_platform_screens"
            else "screen_session_absent"
        )
        return [
            {
                "checker": checker,
                "args": {"session": "%s_%s" % (platform, suffix)},
            }
            for suffix in ("m", "c", "web", "w")
        ]
    if action == "ensure_user_group":
        return [
            {
                "checker": "user_in_group",
                "args": {
                    "user": args.get("user"),
                    "group": args.get("group"),
                },
            }
        ]
    if action == "manage_file_permissions" and args.get("mode"):
        return [
            {
                "checker": "file_mode",
                "args": {
                    "path": args.get("path"),
                    "mode": args.get("mode"),
                },
            }
        ]
    if action == "install_system_packages":
        packages = args.get("packages") if isinstance(args.get("packages"), list) else []
        return [
            {
                "checker": "system_package_installed",
                "args": {"package": package},
            }
            for package in packages
        ]
    if action == "install_python_packages":
        packages = args.get("packages") if isinstance(args.get("packages"), list) else []
        return [
            {
                "checker": "python_package_state",
                "args": {
                    "python_executable": args.get("python_executable"),
                    "package": package,
                    "present": args.get("operation") != "uninstall",
                },
            }
            for package in packages
        ]
    if action == "git_operation":
        operation = str(args.get("operation") or "")
        if operation == "reset" and args.get("ref"):
            return [
                {
                    "checker": "git_revision",
                    "args": {
                        "repository": args.get("repository"),
                        "revision": args.get("ref"),
                    },
                }
            ]
    return []


def _parse_json_object(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("response does not contain JSON")
        text = match.group(0)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("response JSON must be an object")
    return data


def _probe_request_key(request: dict[str, Any]) -> str:
    return json.dumps(
        {
            "probe": str(request.get("probe") or "").strip(),
            "args": (
                request.get("args")
                if isinstance(request.get("args"), dict)
                else {}
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _probe_history_keys(history: list[dict[str, Any]]) -> set[str]:
    keys = set()
    for item in history:
        if not isinstance(item, dict):
            continue
        requests = item.get("requests")
        if not isinstance(requests, list):
            continue
        for request in requests:
            if isinstance(request, dict):
                keys.add(_probe_request_key(request))
    return keys


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _action_risk(value: str) -> str:
    normalized = normalize_semantic_risk(value, "medium")
    return normalized if normalized in RISK_LEVELS else "medium"


def _highest_risk(steps: list[PrivilegedStep]) -> str:
    if not steps:
        return "readonly"
    return max(steps, key=lambda item: RISK_LEVELS.index(item.risk)).risk


def _requires_plan_resource_manifest(
    goal: str,
    steps: list[PrivilegedStep],
) -> bool:
    text = " ".join(
        [goal]
        + [
            "%s %s" % (step.title, step.objective)
            for step in steps
        ]
    ).lower()
    deployment = bool(
        re.search(r"部署|新增.{0,12}实例|新.{0,8}平台|\bdeploy(?:ment)?\b|\bnew instance\b", text)
    )
    mutating = any(
        step.risk != "readonly" or bool(step.expected_changes)
        for step in steps
    )
    return deployment and mutating


def _validate_deployment_plan_shape(
    goal: str,
    steps: list[PrivilegedStep],
    resources: list[PlanResource],
    *,
    grounded_context: GroundedPlanContext | None = None,
) -> None:
    """Reject confirmable new-instance routes that cannot be isolated."""

    text = " ".join(
        [goal]
        + ["%s %s" % (step.title, step.objective) for step in steps]
    ).lower()
    if not re.search(
        r"新增.{0,12}(?:实例|平台)|新.{0,8}(?:实例|平台)|"
        r"\bnew\s+(?:instance|platform)\b|\bdeploy.{0,20}\binstance\b",
        text,
    ):
        return
    by_role = {
        (item.role or item.name): item
        for item in resources
        if item.status == "frozen"
    }
    instance_root = by_role.get("instance_root") or next(
        (
            item for item in resources
            if item.status == "frozen" and item.name == "instance_root"
        ),
        None,
    )
    source_root = by_role.get("source_repo_root")
    if instance_root is None:
        raise ValueError(
            "new_instance_isolation_missing=freeze_distinct_instance_root"
        )
    if source_root is not None and str(instance_root.value) == str(source_root.value):
        raise ValueError(
            "new_instance_isolation_invalid=instance_root_equals_source_repo_root"
        )

    preparation = [
        step for step in steps
        if re.search(
            r"复制|克隆|准备.{0,8}源码|"
            r"\bcopy\b|\bclone\b|\bprepare.{0,12}source",
            "%s %s %s" % (
                step.title,
                step.objective,
                " ".join(step.expected_changes),
            ),
            flags=re.IGNORECASE,
        )
    ]
    start_steps = [
        step for step in steps
        if re.search(
            r"启动.{0,16}(?:平台|组件|服务)|\bstart.{0,20}(?:platform|component|service)",
            "%s %s" % (step.title, step.objective),
            flags=re.IGNORECASE,
        )
    ]
    if start_steps and not preparation:
        raise ValueError(
            "new_instance_isolation_missing=prepare_or_copy_instance_source"
        )
    preparation_ids = {step.step_id for step in preparation}
    step_by_id = {step.step_id: step for step in steps}

    def dependency_closure(step: PrivilegedStep) -> set[str]:
        pending = list(step.depends_on)
        found: set[str] = set()
        while pending:
            dependency_id = pending.pop()
            if dependency_id in found:
                continue
            found.add(dependency_id)
            dependency = step_by_id.get(dependency_id)
            if dependency is not None:
                pending.extend(dependency.depends_on)
        return found

    for start in start_steps:
        if preparation_ids and not preparation_ids.intersection(
            dependency_closure(start)
        ):
            raise ValueError(
                "new_instance_dependency_missing=%s_requires_instance_preparation"
                % start.step_id
            )

    for prepare in preparation:
        source = next(
            (
                resource for resource in resources
                if "%s.source" % prepare.step_id in resource.consumers
            ),
            None,
        )
        destination = next(
            (
                resource for resource in resources
                if "%s.destination" % prepare.step_id in resource.consumers
            ),
            None,
        )
        if source is None:
            raise ValueError(
                "new_instance_copy_source_missing=%s.source" % prepare.step_id
            )
        source_path = Path(str(source.value))
        try:
            source_exists = source_path.is_dir()
        except OSError:
            source_exists = False
        if source.status != "frozen" or not source_exists:
            raise ValueError(
                "new_instance_copy_source_not_existing=%s:%s"
                % (source.name, source.value)
            )
        if grounded_context is not None and source.source != "user_input":
            environment = grounded_context.facts.get("environment_model")
            projects = (
                environment.get("projects", [])
                if isinstance(environment, dict)
                else []
            )
            grounded_paths = set()
            for project in projects:
                if not isinstance(project, dict):
                    continue
                for key in (
                    "candidate_root",
                    "source_repo_root",
                    "platform_root",
                    "backend_package_root",
                ):
                    value = str(project.get(key) or "").strip()
                    if value.startswith("/"):
                        grounded_paths.add(Path(value))
            if grounded_paths and not any(
                source_path == root or root in source_path.parents
                for root in grounded_paths
            ):
                raise ValueError(
                    "new_instance_copy_source_not_grounded_in_environment=%s:%s"
                    % (source.name, source.value)
                )
        if source.source == "user_input" and str(source.value) not in goal:
            raise ValueError(
                "new_instance_copy_source_not_present_in_user_input=%s:%s"
                % (source.name, source.value)
            )
        if destination is None or str(destination.value) != str(instance_root.value):
            raise ValueError(
                "new_instance_copy_destination_must_equal_instance_root=%s"
                % prepare.step_id
            )

    start_ids = {step.step_id for step in start_steps}
    for step in steps:
        step_text = "%s %s %s" % (
            step.title,
            step.objective,
            " ".join(step.success_criteria),
        )
        if (
            re.search(r"nginx|反向代理|路由|proxy", step_text, re.IGNORECASE)
            and re.search(
                r"响应|可达|accessible|responsive|curl|"
                r"http.{0,16}(?:status|response|200|2\d\d|3\d\d)",
                step_text,
                re.IGNORECASE,
            )
            and start_ids
            and not start_ids.intersection(step.depends_on)
        ):
            raise ValueError(
                "semantic_dependency_order_invalid=%s_http_health_requires_backend_start"
                % step.step_id
            )


def _effective_declared_risk(
    deterministic_floor: str,
    declared: Any,
) -> str:
    """Allow the Planner to raise risk, never lower the action-policy floor."""

    normalized = normalize_semantic_risk(declared)
    if not normalized:
        return deterministic_floor
    if normalized not in RISK_LEVELS:
        raise ValueError("invalid declared risk: %s" % normalized)
    return max(
        (deterministic_floor, normalized),
        key=RISK_LEVELS.index,
    )


def _validate_host_facts(action: str, args: dict[str, Any]) -> str:
    """Validate model-selected filesystem facts before a plan can be approved."""

    project_actions = {
        "validate_project_files",
        "prepare_project_files",
        "start_screen_component",
        "start_platform_screens",
        "restart_screen_component",
    }
    if action in project_actions:
        root = Path(str(args.get("project_root") or "")).expanduser()
        if not root.is_dir():
            # A hierarchical implementation plan may create/copy this root in
            # an earlier confirmed step. Route-level validation proves that
            # producer relationship after every micro-step is bound; the
            # Executor checks the real layout again at execution time.
            return ""
        required = (
            "gun.py",
            "master_main.py",
            "celery_worker.py",
            "web_terminal_main.py",
            "worker_gun.py",
            "worker_main.py",
        )
        missing = [
            name
            for name in required
            if not (root / name).is_file()
            and not (root / "mains" / name).is_file()
            and not (root / "vemu_uestc" / "mains" / name).is_file()
        ]
        if missing:
            return "grounding_failed=project_root_missing_entries:%s" % ",".join(missing)
    if action in {"ensure_shared_services", "run_install_script"}:
        script_dir = Path(str(args.get("script_dir") or "")).expanduser()
        script_name = (
            str(args.get("script_name") or "")
            if action == "run_install_script"
            else "docker_service.sh"
        )
        if not script_dir.is_dir() or not (script_dir / script_name).is_file():
            return "grounding_failed=install_script_not_found:%s" % script_name
    if action == "install_nginx_config":
        if str(args.get("content") or "").strip():
            return ""
        source = Path(str(args.get("source_path") or "")).expanduser()
        if not source.is_file():
            return "grounding_failed=nginx_source_or_content_required"
    if action == "sync_directory":
        destination = Path(str(args.get("destination") or "")).expanduser()
        if destination.is_dir():
            try:
                if next(destination.iterdir(), None) is not None:
                    return "grounding_failed=sync_destination_not_empty"
            except OSError:
                return "grounding_failed=sync_destination_not_inspectable"
    if action == "extract_archive":
        archive = Path(str(args.get("archive_path") or "")).expanduser()
        if not archive.is_file():
            return "grounding_failed=archive_not_found"
    return ""


def _validate_action_semantics(action: str, args: dict[str, Any]) -> str:
    """Reject incomplete operation variants before they become confirmable plans."""

    operation = str(args.get("operation") or "").strip()
    allowed_operations = {
        "edit_text_file": {
            "replace_file",
            "replace_once",
            "insert_before",
            "insert_after",
            "append",
        },
        "manage_service": {"start", "stop", "restart", "reload", "enable", "disable"},
        "manage_container": {
            "start", "stop", "restart", "remove", "set_restart_policy",
        },
        "manage_libvirt_domain": {
            "start", "shutdown", "reboot", "destroy", "undefine",
        },
        "manage_docker_network": {
            "create", "remove", "connect", "disconnect",
        },
        "manage_docker_image": {"load", "tag", "remove"},
        "manage_network_link": {"up", "down", "delete"},
        "manage_ovs_resource": {"add", "remove"},
        "git_operation": {
            "status", "rev_parse", "pull", "fetch", "checkout", "switch",
            "clone", "submodule_update", "reset", "revert", "restore", "tag",
            "push",
        },
    }
    if action in allowed_operations and operation not in allowed_operations[action]:
        return "action=%s invalid_operation=%s" % (
            action,
            operation or "missing",
        )
    if action == "edit_text_file":
        anchor = str(args.get("anchor") or "")
        if operation in {"replace_once", "insert_before", "insert_after"}:
            if not anchor:
                return "action=edit_text_file anchor_required"
        elif anchor:
            return "action=edit_text_file anchor_must_be_empty"
    if action == "upsert_python_class":
        class_name = str(args.get("class_name") or "").strip()
        base_class = str(args.get("base_class") or "").strip()
        body = textwrap.dedent(str(args.get("body") or "")).strip("\n")
        if not re.fullmatch(r"[A-Za-z_]\w*", class_name):
            return "action=upsert_python_class invalid_class_name"
        if base_class and not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", base_class
        ):
            return "action=upsert_python_class invalid_base_class"
        try:
            wrapped = ast.parse(
                "class %s(%s):\n%s\n"
                % (class_name, base_class or "object", textwrap.indent(body, "    "))
            )
        except SyntaxError:
            return "action=upsert_python_class invalid_body"
        target = wrapped.body[0]
        if any(isinstance(node, ast.ClassDef) for node in target.body):
            return "action=upsert_python_class body_must_not_include_class_header"
    if action in {"start_screen_component", "restart_screen_component"}:
        component = str(args.get("component") or "").strip()
        platform = str(args.get("platform") or "").strip()
        session = str(args.get("screen_session") or "").strip()
        suffixes = {
            "master": "m",
            "celery": "c",
            "web_terminal": "web",
            "worker": "w",
        }
        if component not in suffixes:
            return "action=%s invalid_component=%s" % (
                action,
                component or "missing",
            )
        if platform and session != "%s_%s" % (platform, suffixes[component]):
            return "action=%s screen_session_mismatch" % action
    if action == "manage_container" and operation == "set_restart_policy":
        if str(args.get("restart_policy") or "") not in {
            "no", "always", "unless-stopped", "on-failure",
        }:
            return "action=manage_container restart_policy_required"
    if action == "manage_docker_network":
        if operation in {"connect", "disconnect"} and not args.get("container"):
            return "action=manage_docker_network container_required"
        if operation in {"remove", "disconnect"} and not _planner_truthy(
            args.get("ownership_confirmed")
        ):
            return "action=manage_docker_network ownership_confirmed_required"
    if action == "manage_docker_image":
        if operation == "load" and not (
            args.get("archive_path") and args.get("expected_image")
        ):
            return "action=manage_docker_image archive_path_and_expected_image_required"
        if operation == "tag" and not (
            args.get("source_image") and args.get("image")
        ):
            return "action=manage_docker_image source_and_target_required"
        if operation == "remove" and not (
            args.get("image") and _planner_truthy(args.get("ownership_confirmed"))
        ):
            return "action=manage_docker_image owned_image_required"
    if action in {"manage_libvirt_domain", "manage_network_link", "manage_ovs_resource"}:
        destructive = (
            action == "manage_libvirt_domain" and operation in {"destroy", "undefine"}
        ) or (
            action == "manage_network_link" and operation == "delete"
        ) or (
            action == "manage_ovs_resource" and operation == "remove"
        )
        if destructive and not _planner_truthy(args.get("ownership_confirmed")):
            return "action=%s ownership_confirmed_required" % action
    if action == "git_operation":
        if operation in {"checkout", "switch", "reset", "revert"} and not args.get("ref"):
            return "action=git_operation ref_required"
        if operation == "restore" and not args.get("path"):
            return "action=git_operation path_required"
        if operation == "tag" and not args.get("tag"):
            return "action=git_operation tag_required"
        if operation == "clone" and not args.get("url"):
            return "action=git_operation url_required"
        if _planner_truthy(args.get("force_with_lease")) and not (
            operation == "push" and args.get("remote") and args.get("ref")
        ):
            return "action=git_operation force_with_lease_requires_remote_and_ref"
    return ""


def _planner_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _validate_environment_model(
    steps: list[PrivilegedStep],
    context: GroundedPlanContext,
) -> None:
    """Reject path semantics that contradict the typed environment model."""

    model = context.facts.get("environment_model")
    if not isinstance(model, dict):
        return
    projects = [
        item for item in model.get("projects", [])
        if isinstance(item, dict)
    ]
    if not projects:
        return
    project_actions = {
        "validate_project_files",
        "prepare_project_files",
        "start_screen_component",
        "start_platform_screens",
        "restart_screen_component",
    }
    prepared_roots: set[str] = set()
    future_paths: set[str] = set()
    for step in steps:
        produced_path = ""
        if step.action in {"sync_directory", "copy_files"}:
            produced_path = str(step.args.get("destination") or "").strip()
        elif step.action == "create_directory":
            produced_path = str(step.args.get("path") or "").strip()
        elif step.action == "extract_archive":
            produced_path = str(
                step.args.get("destination_dir") or ""
            ).strip()
        elif (
            step.action == "git_operation"
            and str(step.args.get("operation") or "") == "clone"
        ):
            produced_path = str(step.args.get("repository") or "").strip()
        elif step.action == "shell_artifact":
            raw_cwd = str(step.args.get("cwd") or "").strip()
            if raw_cwd:
                cwd = str(Path(raw_cwd).expanduser().resolve())
                if not Path(cwd).is_dir() and cwd not in future_paths:
                    raise ValueError(
                        "grounding_failed=shell_future_cwd_has_no_prior_producer"
                    )
            changes = step.args.get("declared_changes")
            if isinstance(changes, list):
                for value in changes:
                    raw = str(value or "").strip()
                    if raw:
                        future_paths.add(
                            str(Path(raw).expanduser().resolve())
                        )
        if produced_path:
            raw_destination = produced_path
            if raw_destination:
                try:
                    future_paths.add(
                        str(Path(raw_destination).expanduser().resolve())
                    )
                except OSError:
                    future_paths.add(raw_destination)
        if step.action == "prepare_project_files":
            raw_prepared = str(step.args.get("project_root") or "").strip()
            if raw_prepared:
                try:
                    prepared_roots.add(
                        str(Path(raw_prepared).expanduser().resolve())
                    )
                except OSError:
                    prepared_roots.add(raw_prepared)
        if step.action not in project_actions:
            continue
        raw_root = str(step.args.get("project_root") or "").strip()
        if not raw_root:
            continue
        try:
            root = str(Path(raw_root).expanduser().resolve())
        except OSError:
            root = raw_root
        source_match = next(
            (
                item for item in projects
                if item.get("source_repo_root") == root
                and item.get("platform_root") != root
            ),
            None,
        )
        if source_match:
            raise ValueError(
                "grounding_failed=project_root_is_source_repo:"
                "use_platform_root=%s" % source_match.get("platform_root")
            )
        layout = next(
            (item for item in projects if item.get("platform_root") == root),
            None,
        )
        if layout is None:
            root_path = Path(root)
            produced = root in future_paths or any(
                candidate == root_path / "mains"
                for candidate in (Path(item) for item in future_paths)
            )
            if not produced:
                raise ValueError(
                    "grounding_failed=future_project_root_has_no_prior_producer"
                )
            prepared_roots.add(root)
            continue
        if layout.get("readiness") == "invalid":
            raise ValueError(
                "grounding_failed=invalid_project_layout:%s"
                % ",".join(layout.get("violations") or ["unknown"])
            )
        if step.action in {"start_platform_screens", "start_screen_component"} and (
            layout.get("readiness") != "runnable"
            and root not in prepared_roots
        ):
            raise ValueError(
                "grounding_failed=runtime_entries_not_prepared"
            )
        if step.action == "prepare_project_files":
            expected_source = str(layout.get("entry_source_root") or "")
            supplied_source = str(step.args.get("source_root") or "")
            if supplied_source and supplied_source != expected_source:
                raise ValueError(
                    "grounding_failed=entry_source_root_mismatch"
                )
            prepared_roots.add(root)


def _grounding_summary(
    context: GroundedPlanContext | None,
    planner_source: str,
) -> dict[str, Any]:
    summary = (
        context.audit_summary()
        if context is not None
        else {"context_policy": "caller_environment+registered_probes"}
    )
    summary["planner_source"] = planner_source
    return summary
