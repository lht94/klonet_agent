"""Mutation-only Change Planner for Ops-Privilege V4."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from klonet_agent.ops.privileged.contracts import PlanResource, RISK_LEVELS
from klonet_agent.ops.privileged.checkers import (
    CHECKER_REQUIRED_ARGS,
    DefaultCheckerRegistry,
)
from klonet_agent.ops.privileged.v4.contracts import (
    ChangePlanV4,
    ChangeStepV4,
    EvidenceBundle,
    EvidenceConclusion,
    ProbeRequest,
)
from klonet_agent.ops.privileged.v4.discovery import parse_json_object


CHANGE_PLANNER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege V4 Change Planner.
Plan only real host state changes. Discovery, inspection, evidence aggregation,
summaries, reports, answers and verification are separate workflow phases and
must never appear as changes. Do not select Action names or emit commands.
When the evidence conclusion contains a deterministic `User-selected Screen
source maps authoritatively` fact with repository, remote, branch and revision,
use it as the source contract and do not request source discovery again.

Return one JSON object with status `need_evidence`, `ready`, or `blocked`.
For need_evidence return at most four registered read-only probe_requests.
For blocked return reason and missing_decisions.
For ready return goal, assumptions, frozen/deferred resources and `changes`.
Every change needs step_id, title, objective, reason, evidence_refs, depends_on,
risk, non-empty expected_changes, and non-empty structured postconditions.
Keep assumptions to at most 12 concise items of at most 500 characters each;
never generate repetitive paraphrases. Return no more than 12 changes and 64
resources. The entire JSON response must fit within 8000 output tokens.
Risk cannot be readonly. Evidence references must be supplied evidence IDs.

For status=ready, use this exact shape (repeat the change object as needed):
{
  "status": "ready",
  "goal": "...",
  "assumptions": [],
  "resources": [
    {"name":"instance_root","kind":"path","status":"frozen",
     "role":"instance_root","value":"/absolute/new/root",
     "source":"user_input","consumers":["change-1.repository"]},
    {"name":"source_remote","kind":"identifier","status":"frozen",
     "role":"source_remote","value":"git@example/repository.git",
     "source":"evidence","consumers":["change-1.url"]},
    {"name":"source_branch","kind":"identifier","status":"frozen",
     "role":"source_branch","value":"develop","source":"evidence",
     "consumers":["change-1.ref"]},
    {"name":"instance_identifier","kind":"identifier","status":"frozen",
     "role":"instance_identifier","value":"new-instance",
     "source":"user_input","consumers":["change-1.instance_name"]},
    {"name":"service_port","kind":"port","status":"frozen",
     "role":"service_port","value":47001,"source":"evidence",
     "consumers":["change-1.port"]}
  ],
  "changes": [{
    "step_id": "change-1",
    "title": "...",
    "objective": "...",
    "reason": "...",
    "evidence_refs": ["ev-..."],
    "depends_on": [],
    "risk": "medium",
    "expected_changes": ["..."],
    "postconditions": [
      {"checker": "file_exists", "args": {"path": "/absolute/path"}}
    ]
  }]
}
postconditions must be a JSON array of checker objects, never prose and never
omitted. Common checkers include file_exists, file_contains, git_revision,
service_active, process_running, port_listening, http_status,
nginx_config_valid, screen_session_exists, and exit_code_zero. Prefer
independent state checkers; exit_code_zero alone is only a last resort.

Use blocked only for a material user choice that changes the desired outcome.
Never ask the user to choose implementation details that Discovery or Binding
can resolve, including free ports, local IPs, generated service/screen names,
whether to isolate rather than reuse an existing container, Nginx syntax,
configuration file edits, startup commands, or source layout. For those, use
need_evidence when state is missing, otherwise choose isolated values, freeze
them as resources, and emit semantic changes.

Keep every ChangeStep semantically cohesive. A configuration or service-group
ChangeStep may contain multiple related attributes or components; the Binding
stage will decompose it into atomic Action/Shell implementation steps. For an
isolated deployment, explicitly plan new uniquely named containers and
sessions; never assume reuse of an existing resource. Every numeric port used
in host configuration or postconditions must have its own frozen port resource.
When a `ports` evidence record checks an explicit candidate list and reports no
matching listeners, select host ports only from that checked list. Never invent
familiar-looking 470xx ports or reuse ports merely seen in unrelated evidence.
Mark fixed image/container-side ports with role `container_internal_port`;
only host/listening ports require availability proof.
If MySQL, Redis, RabbitMQ, or another stateful dependency is required by an
isolated deployment, add semantic ChangeSteps that create new instance-named
containers. Application startup must depend on those provisioning steps and
must appear after them. Do not substitute host server processes for the new
instance-named stateful containers. Nginx activation must depend on and appear
after application startup. An isolated Nginx site must listen on its own explicit,
availability-verified frozen port; its http_status postcondition must include
that port instead of implicitly checking an existing port 80 route. Missing
image or credential details are resolvable by Discovery and Binding and must
never justify sharing an existing service.

A complete Klonet platform runtime has exactly four Screen components:
master (`<instance>_m`), celery (`<instance>_c`), web terminal
(`<instance>_web`), and worker (`<instance>_w`). It has distinct frozen
`master_port`, `worker_port`, and `web_terminal_port` host resources; celery
does not listen on a fourth application port. Configuration changes must name
those exact Python attributes as well as mysql_port, redis_port and
rabbitmq_port when isolated stateful containers are planned. The Nginx site
fronts the master application port; web-terminal and worker liveness are
proved independently. Do not rename the web-terminal port to a generic
`web_port`, and do not spell Screen suffixes as `_master` or `_worker`.
The currently registered complete-runtime capability does not start a separate
data-server component, so do not add `data_server_port`, a data-server Screen,
or a fifth application component to this V4 deployment contract.
The discovered source initializes empty database tables during application
startup (`app_factory` calls `create_all`); do not invent a separate migration,
seed, or database-initialization ChangeStep without explicit contrary evidence.
""".strip()


DISCOVERABLE_IMPLEMENTATION_MARKERS = (
    "port",
    "ip address",
    "docker container",
    "nginx",
    "screen session",
    "startup command",
    "configuration file",
    "source layout",
    "端口",
    "ip 地址",
    "容器",
    "启动命令",
    "配置文件",
)


@dataclass
class V4PlanningOutcome:
    status: str
    plan: ChangePlanV4 | None = None
    candidate_plan: ChangePlanV4 | None = None
    probe_requests: list[ProbeRequest] = field(default_factory=list)
    reason: str = ""
    missing_decisions: list[str] = field(default_factory=list)


class V4ChangePlannerAgent:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def plan(
        self,
        goal: str,
        bundle: EvidenceBundle,
        conclusion: EvidenceConclusion,
        *,
        binding_feedback: str = "",
    ) -> V4PlanningOutcome:
        messages = [
            {"role": "system", "content": CHANGE_PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Goal:\n%s\n\nEvidence conclusion:\n%s\n\nEvidence records:\n%s"
                % (
                    goal,
                    self._conclusion_json(conclusion),
                    self._evidence_json(bundle),
                ),
            },
        ]
        if binding_feedback:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The previous semantic plan could not proceed safely. Replan "
                        "once without changing the goal or inventing evidence. Preserve "
                        "all grounded decisions except those explicitly rejected by the "
                        "feedback. Planning or binding feedback: %s"
                        % binding_feedback
                    ),
                }
            )
        last_error: Exception | None = None
        content = ""
        max_generations = 4
        for attempt in range(max_generations):
            try:
                response = self._complete(messages)
                content = response.choices[0].message.content or ""
                return self._outcome(parse_json_object(content), goal, bundle)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < max_generations - 1:
                    previous = (
                        content
                        if len(content) <= 12000
                        else "Previous planner output omitted: contract size exceeded."
                    )
                    messages.append({"role": "assistant", "content": previous})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Repair the Change Planner JSON. Error: %s\n"
                                "Return the complete object again. Every changes[] item "
                                "must include non-empty expected_changes and a non-empty "
                                '"postconditions" array of objects shaped as '
                                '{"checker":"file_exists","args":{"path":"/absolute/path"}}.'
                                " Do not use readonly or summary steps. Deployment plans "
                                "must include frozen resources for instance_root, "
                                "source_remote, source_branch, instance identifiers, and "
                                "every selected port, with semantic-step consumers such "
                                "as change-1.repository, change-1.url, and change-1.ref. "
                                "Freeze every future configuration file path derived "
                                "from instance_root and bind it as change-N.path. "
                                "Do not reuse or modify existing instance resources. "
                                "Keep semantic changes cohesive and let Binding split "
                                "them into atomic implementation steps. Freeze every "
                                "host/listening port used anywhere in the plan; mark "
                                "fixed container-side ports as container_internal_port. "
                                "For isolated stateful dependencies, add semantic changes "
                                "that create new instance-named containers; never reuse "
                                "shared services merely because image or credential "
                                "details still need Binding-time discovery. Application "
                                "startup must depend on and follow stateful provisioning. "
                                "Use new instance-named containers, not host server "
                                "processes. Nginx activation must follow application startup. "
                                "An isolated Nginx site must use an explicit dedicated "
                                "frozen listen port, and http_status must check that port."
                            )
                            % exc,
                        }
                    )
            except Exception as exc:
                if isinstance(exc, IndexError) and last_error is not None:
                    break
                last_error = exc
        return V4PlanningOutcome(
            status="blocked",
            reason=(
                "Change Planner output invalid after bounded repairs: %s"
                % str(last_error or "unknown planner failure")
            ),
        )

    def _complete(self, messages: list[dict[str, str]]) -> Any:
        try:
            return self.llm.complete(
                messages=messages,
                tools=None,
                reasoning_effort="medium",
                temperature=0,
                response_format={"type": "json_object"},
                max_tokens=8000,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except TypeError:
            return self.llm.complete(messages=messages, tools=None)

    def _outcome(
        self,
        data: dict[str, Any],
        goal: str,
        bundle: EvidenceBundle,
    ) -> V4PlanningOutcome:
        assumptions = data.get("assumptions", [])
        if not isinstance(assumptions, list):
            raise ValueError("planner assumptions must be an array")
        if len(assumptions) > 12 or any(len(str(item)) > 500 for item in assumptions):
            raise ValueError("planner assumptions exceed bounded contract")
        changes = data.get("changes", [])
        resources = data.get("resources", [])
        if isinstance(changes, list) and len(changes) > 12:
            raise ValueError("planner changes exceed bounded contract")
        if isinstance(resources, list) and len(resources) > 64:
            raise ValueError("planner resources exceed bounded contract")
        status = str(data.get("status") or "").strip().lower()
        if status == "need_evidence":
            requests = self._probe_requests(data.get("probe_requests"))
            authoritative_roots = self._authoritative_screen_source_roots(
                goal,
                bundle,
            )
            if authoritative_roots and requests:
                fresh_requests = [
                    request
                    for request in requests
                    if not self._is_redundant_source_request(
                        request,
                        authoritative_roots,
                    )
                ]
                if not fresh_requests:
                    raise ValueError(
                        "authoritative Screen source evidence already provides Git root, "
                        "remote, branch, and revision; return a ready plan instead of "
                        "requesting duplicate source probes"
                    )
                requests = fresh_requests
            return V4PlanningOutcome(
                status=status,
                probe_requests=requests,
            )
        if status == "blocked":
            missing = data.get("missing_decisions")
            missing_items = (
                [str(item) for item in missing]
                if isinstance(missing, list)
                else []
            )
            lowered = " ".join(missing_items).lower()
            if any(
                marker in lowered
                for marker in DISCOVERABLE_IMPLEMENTATION_MARKERS
            ):
                raise ValueError(
                    "blocked cannot offload discoverable implementation details; "
                    "Discovery or Binding must resolve them"
                )
            return V4PlanningOutcome(
                status=status,
                reason=str(data.get("reason") or "planning blocked"),
                missing_decisions=missing_items,
            )
        if status != "ready":
            raise ValueError("planner status must be need_evidence, ready, or blocked")
        self._normalize_change_order(data)
        self._normalize_verification_changes(data)
        self._normalize_change_order(data)
        self._normalize_postcondition_args(data)
        resources = [
            PlanResource.from_dict(item)
            for item in data.get("resources", [])
            if isinstance(item, dict)
        ]
        resources = self._normalize_derived_resources(data, resources)
        self._normalize_nginx_postconditions(data, resources)
        contract_errors = self._ready_contract_errors(
            data,
            goal,
            resources,
            bundle,
        )
        unproven_ports = self._unproven_port_resources(resources, bundle)
        non_port_errors = [
            error
            for error in contract_errors
            if not error.startswith("port resource lacks availability evidence=")
        ]
        if unproven_ports and not non_port_errors:
            candidate_steps = self._steps(data.get("changes"), bundle)
            candidate_risk = max(
                candidate_steps,
                key=lambda item: RISK_LEVELS.index(item.risk),
            ).risk
            candidate_assumptions = data.get("assumptions")
            candidate_plan = ChangePlanV4.new(
                goal=goal,
                risk=candidate_risk,
                steps=candidate_steps,
                resources=resources,
                assumptions=[str(item) for item in candidate_assumptions]
                if isinstance(candidate_assumptions, list)
                else [],
            )
            return V4PlanningOutcome(
                status="need_evidence",
                candidate_plan=candidate_plan,
                probe_requests=[
                    ProbeRequest(
                        "ports",
                        {"ports": sorted({int(item.value) for item in unproven_ports})},
                        "verify frozen port availability",
                    )
                ],
            )
        if contract_errors:
            raise ValueError("; ".join(contract_errors))
        steps = self._steps(data.get("changes"), bundle)
        risk = max(steps, key=lambda item: RISK_LEVELS.index(item.risk)).risk
        assumptions = data.get("assumptions")
        return V4PlanningOutcome(
            status="ready",
            plan=ChangePlanV4.new(
                goal=goal,
                risk=risk,
                steps=steps,
                resources=resources,
                assumptions=[str(item) for item in assumptions]
                if isinstance(assumptions, list)
                else [],
            ),
        )

    @staticmethod
    def _normalize_postcondition_args(data: dict[str, Any]) -> None:
        """Compile common model aliases into registered checker contracts."""

        aliases = {
            "git_revision": {"path": "repository"},
            "file_contains": {"content": "text"},
            "screen_session_exists": {"name": "session"},
            "process_running": {"name": "pattern"},
        }
        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        for change in changes:
            postconditions = (
                change.get("postconditions") if isinstance(change, dict) else None
            )
            if not isinstance(postconditions, list):
                continue
            for check in postconditions:
                if not isinstance(check, dict):
                    continue
                args = check.get("args")
                if not isinstance(args, dict):
                    continue
                for source, target in aliases.get(
                    str(check.get("checker") or ""), {}
                ).items():
                    if target not in args and source in args:
                        args[target] = args[source]
                    if source != target and source in args:
                        del args[source]

    @staticmethod
    def _normalize_nginx_postconditions(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> None:
        """Materialize the HTTP proof implied by a frozen Nginx listen port."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        for change in changes:
            if not isinstance(change, dict):
                continue
            step_id = str(change.get("step_id") or "")
            text = "%s %s" % (
                str(change.get("title") or ""),
                str(change.get("objective") or ""),
            )
            if not re.search(r"nginx", text, re.I):
                continue
            listen_resource = next(
                (
                    resource
                    for resource in resources
                    if resource.status == "frozen"
                    and resource.kind == "port"
                    and V4ChangePlannerAgent._requires_host_port_availability(resource)
                    and any(
                        consumer.rsplit(".", 1)[0] == step_id
                        and (
                            consumer.endswith(".listen_port")
                            or "nginx" in str(resource.role or "").lower()
                            or "nginx" in str(resource.name or "").lower()
                        )
                        for consumer in resource.consumers
                    )
                ),
                None,
            )
            if listen_resource is None:
                continue
            postconditions = change.get("postconditions")
            if not isinstance(postconditions, list):
                postconditions = []
                change["postconditions"] = postconditions
            port = int(listen_resource.value)
            if any(
                isinstance(check, dict)
                and check.get("checker") == "http_status"
                and re.search(
                    r"https?://[^/:]+:%s(?:/|$)" % port,
                    str((check.get("args") or {}).get("url") or ""),
                    re.I,
                )
                for check in postconditions
            ):
                continue
            postconditions.append(
                {
                    "checker": "http_status",
                    "args": {
                        "url": "http://127.0.0.1:%s" % port,
                        "expected_status": 200,
                    },
                }
            )

    @staticmethod
    def _authoritative_screen_source_roots(
        goal: str,
        bundle: EvidenceBundle,
    ) -> set[str]:
        goal_text = str(goal or "").lower()
        roots: set[str] = set()
        all_grounded_roots: set[str] = set()
        for record in bundle.records:
            if (
                record.status == "available"
                and record.request.probe == "git_repository"
                and "derived authoritative Screen source"
                in str(record.request.purpose or "")
            ):
                repository = str(record.request.args.get("repository") or "")
                if repository:
                    all_grounded_roots.add(repository)
                    roots.add(repository)
                continue
            if record.status != "available" or record.request.probe != "screen":
                continue
            output = record.output
            if "remotes=origin" in output:
                all_grounded_roots.update(
                    match.group(1)
                    for match in re.finditer(
                        r"\bpath=(/[^\s]+)\s+inside_work_tree=true",
                        output,
                    )
                )
            for match in re.finditer(
                r"(?m)^session=([A-Za-z0-9_.-]+).*?\bgit_roots=([^\s]+)",
                output,
            ):
                session = match.group(1)
                prefix = re.sub(
                    r"_(?:web|m|c|w|worker|master|controller)$",
                    "",
                    session,
                    flags=re.I,
                )
                for root in match.group(2).split(","):
                    if root == "unknown" or not root:
                        continue
                    grounded = (
                        "path=%s inside_work_tree=true" % root in output
                        and "remotes=origin" in output
                        and re.search(r"status=##\s+[^\s]+", output) is not None
                    )
                    if grounded:
                        all_grounded_roots.add(root)
                        if prefix.lower() in goal_text:
                            roots.add(root)
        if roots:
            return roots
        if "screen" in goal_text and len(all_grounded_roots) == 1:
            return all_grounded_roots
        return set()

    @staticmethod
    def _is_redundant_source_request(
        request: ProbeRequest,
        authoritative_roots: set[str],
    ) -> bool:
        if request.probe not in {
            "git_repository",
            "screen",
            "screen_session",
            "process",
            "process_detail",
        }:
            return False
        requested_path = str(
            request.args.get("repository") or request.args.get("path") or ""
        )
        if requested_path in authoritative_roots:
            return True
        purpose = str(request.purpose or "").lower()
        if "target" in purpose or "目标" in purpose:
            return False
        return any(
            marker in purpose
            for marker in (
                "source",
                "remote",
                "branch",
                "screen",
                "cwd",
                "authoritative",
                "resolve planning evidence gap",
                "源实例",
            )
        )

    @staticmethod
    def finalize_candidate(
        candidate: ChangePlanV4,
        bundle: EvidenceBundle,
    ) -> V4PlanningOutcome:
        unproven = V4ChangePlannerAgent._unproven_port_resources(
            candidate.resources,
            bundle,
        )
        if unproven:
            return V4PlanningOutcome(
                status="need_evidence",
                candidate_plan=candidate,
                probe_requests=[
                    ProbeRequest(
                        "ports",
                        {"ports": sorted({int(item.value) for item in unproven})},
                        "verify frozen port availability",
                    )
                ],
            )
        occupied = []
        for resource in candidate.resources:
            if not V4ChangePlannerAgent._requires_host_port_availability(resource):
                continue
            port = int(resource.value)
            relevant = [
                record
                for record in bundle.records
                if record.request.probe == "ports"
                and port in record.request.args.get("ports", [])
            ]
            if any(re.search(r":%s\b" % port, record.output) for record in relevant):
                occupied.append(port)
        if occupied:
            return V4PlanningOutcome(
                status="blocked",
                candidate_plan=candidate,
                reason="candidate ports became occupied: %s"
                % ",".join(str(item) for item in occupied),
            )
        return V4PlanningOutcome(status="ready", plan=candidate)

    @staticmethod
    def _ready_contract_errors(
        data: dict[str, Any],
        goal: str,
        resources: list[PlanResource],
        bundle: EvidenceBundle,
    ) -> list[str]:
        errors: list[str] = []
        consumer_owners: dict[str, list[str]] = {}
        for resource in resources:
            for consumer in resource.consumers:
                owners = consumer_owners.setdefault(consumer, [])
                if resource.name not in owners:
                    owners.append(resource.name)
        for consumer, owners in consumer_owners.items():
            if len(owners) > 1:
                errors.append(
                    "plan resource consumer has multiple owners=%s:%s"
                    % (consumer, ",".join(owners))
                )
        known_checkers = set(DefaultCheckerRegistry().names)
        changes = data.get("changes")
        if isinstance(changes, list):
            for step_index, change in enumerate(changes):
                if not isinstance(change, dict):
                    continue
                if str(change.get("risk") or "").strip() == "readonly":
                    errors.append("ChangeStepV4 cannot be readonly")
                if not change.get("expected_changes"):
                    errors.append(
                        "change[%s] requires expected_changes" % step_index
                    )
                postconditions = change.get("postconditions")
                if not isinstance(postconditions, list):
                    errors.append(
                        "change[%s] requires postconditions" % step_index
                    )
                    continue
                if not postconditions:
                    errors.append(
                        "change[%s] requires postconditions" % step_index
                    )
                for check_index, check in enumerate(postconditions):
                    if not isinstance(check, dict):
                        errors.append(
                            "change[%s].postcondition[%s] must be an object"
                            % (step_index, check_index)
                        )
                        continue
                    name = str(check.get("checker") or "").strip()
                    args = check.get("args")
                    if name not in known_checkers:
                        errors.append(
                            "change[%s].postcondition[%s] checker_not_registered=%s"
                            % (step_index, check_index, name or "missing")
                        )
                        continue
                    if not isinstance(args, dict):
                        errors.append(
                            "change[%s].postcondition[%s] args must be an object"
                            % (step_index, check_index)
                        )
                        continue
                    missing = [
                        key
                        for key in CHECKER_REQUIRED_ARGS.get(name, ())
                        if key not in args or args[key] in (None, "")
                    ]
                    if missing:
                        errors.append(
                            "change[%s].postcondition[%s] checker=%s "
                            "missing_required_args=%s"
                            % (step_index, check_index, name, ",".join(missing))
                        )
        original_goal = str(goal or "")
        planned_goal = str(data.get("goal") or "")
        goal_text = "%s\n%s" % (original_goal, planned_goal)
        deployment = bool(
            re.search(r"\b(?:deploy|deployment|clone)\b|部署|克隆", goal_text, re.I)
        )
        if not deployment:
            return errors
        frozen = [item for item in resources if item.status == "frozen"]
        roles = {str(item.role or "").lower(): item for item in frozen}
        required_roles = {
            "instance_root": any(
                role in {"instance_root", "target_root", "deployment_root"}
                for role in roles
            ),
            "source_remote": any(
                "source" in role and ("remote" in role or "url" in role)
                for role in roles
            ),
            "source_branch": any(
                "source" in role and "branch" in role for role in roles
            ),
            "instance_identifier": any(
                role in {
                    "instance_identifier",
                    "instance_name",
                    "platform_instance_name",
                }
                for role in roles
            ),
            "port": any(
                V4ChangePlannerAgent._requires_host_port_availability(item)
                for item in frozen
            ),
        }
        missing_roles = [name for name, present in required_roles.items() if not present]
        if missing_roles:
            errors.append("missing frozen resources=%s" % ",".join(missing_roles))
        explicit_paths = set(
            match.rstrip(".,;:，。；：")
            for match in re.findall(r"/[A-Za-z0-9_./-]+", original_goal)
        )
        frozen_paths = {str(item.value) for item in frozen if item.kind == "path"}
        absent_paths = sorted(explicit_paths - frozen_paths)
        if absent_paths:
            errors.append("goal paths are not frozen=%s" % ",".join(absent_paths))
        fixed_identifiers = [
            value
            for value in re.findall(
                r"(?:实例名|instance name)\s*(?:固定)?\s*(?:为|is|=)\s*([A-Za-z0-9_.-]+)",
                goal_text,
                re.I,
            )
            if value
        ]
        frozen_identifiers = {
            str(item.value)
            for item in frozen
            if str(item.role or "").lower()
            in {
                "instance_identifier",
                "instance_name",
                "platform_instance_name",
            }
        }
        absent_identifiers = sorted(set(fixed_identifiers) - frozen_identifiers)
        if absent_identifiers:
            errors.append(
                "fixed instance identifiers are not frozen=%s"
                % ",".join(absent_identifiers)
            )
        fixed_nginx_names = [
            value
            for value in re.findall(
                r"Nginx\s*配置名\s*(?:固定)?\s*(?:为|is|=)\s*([A-Za-z0-9_.-]+)",
                original_goal,
                re.I,
            )
            if value
        ]
        frozen_nginx_names = {
            str(item.value)
            for item in frozen
            if str(item.role or "").lower()
            in {"nginx_config_name", "nginx_site_name"}
        }
        absent_nginx_names = sorted(set(fixed_nginx_names) - frozen_nginx_names)
        if absent_nginx_names:
            errors.append(
                "fixed Nginx config names are not frozen=%s"
                % ",".join(absent_nginx_names)
            )
        step_ids = {
            str(item.get("step_id") or "")
            for item in changes or []
            if isinstance(item, dict)
        }
        for resource in frozen:
            if not resource.consumers:
                errors.append("frozen resource has no consumers=%s" % resource.name)
                continue
            unknown = [
                consumer
                for consumer in resource.consumers
                if consumer.rsplit(".", 1)[0] not in step_ids
            ]
            if unknown:
                errors.append(
                    "resource consumers reference unknown steps=%s"
                    % ",".join(unknown)
                )
        frozen_port_values = {
            int(resource.value)
            for resource in frozen
            if resource.kind == "port"
        }
        if isinstance(changes, list):
            for change in changes:
                if not isinstance(change, dict):
                    continue
                step_id = str(change.get("step_id") or "")
                used_ports = V4ChangePlannerAgent._used_ports_by_step(data).get(
                    step_id,
                    set(),
                )
                for port in sorted(used_ports - frozen_port_values):
                    errors.append(
                        "change %s uses unfrozen port=%s" % (step_id, port)
                    )
        root_resource = next(
            (
                item
                for item in frozen
                if str(item.role or "").lower()
                in {"instance_root", "target_root", "deployment_root"}
            ),
            None,
        )
        if root_resource is not None and not any(
            consumer.endswith(".repository") for consumer in root_resource.consumers
        ):
            errors.append("instance_root requires a .repository consumer")
        if root_resource is not None and isinstance(changes, list):
            root_text = str(root_resource.value).rstrip("/")
            for change in changes:
                if not isinstance(change, dict):
                    continue
                step_id = str(change.get("step_id") or "")
                postconditions = change.get("postconditions")
                if not isinstance(postconditions, list):
                    continue
                for check in postconditions:
                    if not isinstance(check, dict):
                        continue
                    args = check.get("args")
                    path = str(args.get("path") or "") if isinstance(args, dict) else ""
                    if not path.startswith(root_text + "/"):
                        continue
                    if path == root_text + "/.git":
                        continue
                    matching = [
                        resource
                        for resource in frozen
                        if resource.kind == "path" and str(resource.value) == path
                    ]
                    if not any(
                        consumer.rsplit(".", 1)[0] == step_id
                        and (
                            consumer.rsplit(".", 1)[1]
                            in {"path", "source_path", "repository"}
                            or consumer.rsplit(".", 1)[1].startswith("path_")
                        )
                        for resource in matching
                        for consumer in resource.consumers
                    ):
                        errors.append(
                            "future file path is not frozen for %s=%s"
                            % (step_id, path)
                        )
        source_remote = next(
            (
                item
                for item in frozen
                if "source" in str(item.role or "").lower()
                and (
                    "remote" in str(item.role or "").lower()
                    or "url" in str(item.role or "").lower()
                )
            ),
            None,
        )
        if source_remote is not None and not any(
            consumer.endswith(".url") for consumer in source_remote.consumers
        ):
            errors.append("source_remote requires a .url consumer")
        if source_remote is not None:
            remote_value = str(source_remote.value)
            if remote_value.startswith("/") or not any(
                marker in remote_value for marker in (".git", ":", "https://", "ssh://")
            ):
                errors.append("source_remote must be a discovered Git remote")
            elif not any(
                remote_value in record.output for record in bundle.records
            ):
                errors.append("source_remote is not grounded in evidence")
        source_branch = next(
            (
                item
                for item in frozen
                if "source" in str(item.role or "").lower()
                and "branch" in str(item.role or "").lower()
            ),
            None,
        )
        if source_branch is not None and not any(
            consumer.endswith(".ref") for consumer in source_branch.consumers
        ):
            errors.append("source_branch requires a .ref consumer")
        if source_branch is not None and not any(
            str(source_branch.value) in record.output for record in bundle.records
        ):
            errors.append("source_branch is not grounded in evidence")
        for port_resource in V4ChangePlannerAgent._unproven_port_resources(
            frozen,
            bundle,
        ):
            errors.append(
                "port resource lacks availability evidence=%s" % port_resource.name
            )
        for port_resource in (
            item
            for item in frozen
            if V4ChangePlannerAgent._requires_host_port_availability(item)
        ):
            port = int(port_resource.value)
            relevant = [
                record
                for record in bundle.records
                if record.request.probe == "ports"
                and port in record.request.args.get("ports", [])
            ]
            if not relevant:
                continue
            if any(re.search(r":%s\b" % port, record.output) for record in relevant):
                errors.append("port resource is already listening=%s" % port)
        isolation_requested = bool(re.search(r"isolat|隔离|不干扰", goal_text, re.I))
        isolation_payload = json.dumps(
            {
                "assumptions": data.get("assumptions", []),
                "changes": data.get("changes", []),
            },
            ensure_ascii=False,
        ).lower()
        isolation_payload = V4ChangePlannerAgent._strip_negated_reuse_claims(
            isolation_payload
        )
        if isolation_requested and re.search(
            r"(?:reuse|share|复用|共享).{0,40}(?:existing|container|现有|容器)",
            isolation_payload,
            re.I,
        ):
            errors.append("isolated deployment cannot reuse existing resources")
        if isolation_requested and isinstance(changes, list):
            indexed_changes = [item for item in changes if isinstance(item, dict)]
            step_positions = {
                str(item.get("step_id") or ""): index
                for index, item in enumerate(indexed_changes)
            }
            step_dependencies = {
                str(item.get("step_id") or ""): {
                    str(value) for value in item.get("depends_on", [])
                }
                for item in indexed_changes
                if isinstance(item.get("depends_on", []), list)
            }

            def change_text(item: dict[str, Any]) -> str:
                return json.dumps(
                    {
                        "title": item.get("title", ""),
                        "objective": item.get("objective", ""),
                        "expected_changes": item.get("expected_changes", []),
                    },
                    ensure_ascii=False,
                ).lower()

            def primary_change_text(item: dict[str, Any]) -> str:
                return json.dumps(
                    {
                        "title": item.get("title", ""),
                        "objective": item.get("objective", ""),
                    },
                    ensure_ascii=False,
                ).lower()

            def ancestors(step_id: str) -> set[str]:
                found: set[str] = set()
                pending = list(step_dependencies.get(step_id, set()))
                while pending:
                    dependency = pending.pop()
                    if dependency in found:
                        continue
                    found.add(dependency)
                    pending.extend(step_dependencies.get(dependency, set()))
                return found

            verification_step_ids: set[str] = set()
            for change in indexed_changes:
                title = str(change.get("title") or "").strip()
                objective = str(change.get("objective") or "").strip()
                primary = title or objective
                if re.match(
                    r"^(?:verify|validate|check|confirm|assert)\b|"
                    r"^(?:验证|校验|检查|确认|验收)",
                    primary,
                    re.I,
                ):
                    verification_step_ids.add(str(change.get("step_id") or ""))
                    errors.append(
                        "verification-only change is not allowed=%s"
                        % str(change.get("step_id") or "")
                    )

            stateful_candidates = [
                item
                for item in indexed_changes
                if re.search(
                    r"stateful|mysql|redis|rabbitmq|数据库|消息队列|有状态",
                    primary_change_text(item),
                    re.I,
                )
                and re.search(
                    r"provision\b|create\b|start\b|run\b|container\b|"
                    r"部署|创建|启动|容器",
                    primary_change_text(item),
                    re.I,
                )
            ]
            stateful = [
                item
                for item in stateful_candidates
                if re.search(r"containers?\b|容器", change_text(item), re.I)
            ]
            for service in stateful_candidates:
                if service in stateful:
                    continue
                errors.append(
                    "isolated stateful service must use a new named container=%s"
                    % str(service.get("step_id") or "")
                )
            application_starts = [
                item
                for item in indexed_changes
                if re.search(r"start|launch|启动|拉起", change_text(item), re.I)
                and re.search(
                    r"application|component|screen|master|worker|web terminal|"
                    r"应用|组件|会话",
                    change_text(item),
                    re.I,
                )
                and item not in stateful
            ]
            for application in application_starts:
                application_id = str(application.get("step_id") or "")
                application_ancestors = ancestors(application_id)
                for service in stateful:
                    service_id = str(service.get("step_id") or "")
                    if (
                        step_positions.get(service_id, -1)
                        >= step_positions.get(application_id, -1)
                        or service_id not in application_ancestors
                    ):
                        errors.append(
                            "application start must depend on earlier stateful "
                            "provisioning=%s:%s" % (application_id, service_id)
                        )

            nginx_changes = [
                item
                for item in indexed_changes
                if "nginx" in change_text(item)
                and str(item.get("step_id") or "") not in verification_step_ids
            ]
            nginx_activations = [
                item
                for item in nginx_changes
                if re.search(
                    r"\b(?:reload|activate|restart)\b|重载|激活|重新加载",
                    change_text(item),
                    re.I,
                )
                or any(
                    isinstance(check, dict)
                    and check.get("checker") in {"http_status", "service_active"}
                    for check in item.get("postconditions", [])
                )
            ]
            for nginx_change in nginx_activations:
                nginx_id = str(nginx_change.get("step_id") or "")
                nginx_ancestors = ancestors(nginx_id)
                for application in application_starts:
                    application_id = str(application.get("step_id") or "")
                    if (
                        step_positions.get(application_id, -1)
                        >= step_positions.get(nginx_id, -1)
                        or application_id not in nginx_ancestors
                    ):
                        errors.append(
                            "Nginx activation must depend on earlier application "
                            "start=%s:%s" % (nginx_id, application_id)
                        )

            frozen_host_ports = {
                int(item.value): item
                for item in frozen
                if V4ChangePlannerAgent._requires_host_port_availability(item)
            }
            used_ports_by_step = (
                V4ChangePlannerAgent._declared_listening_ports_by_step(data)
            )
            nginx_ids = {
                str(change.get("step_id") or "") for change in nginx_changes
            }
            http_ports: set[int] = set()
            for change in nginx_changes:
                for check in change.get("postconditions", []):
                    if not isinstance(check, dict) or check.get("checker") != "http_status":
                        continue
                    args = check.get("args")
                    url = str(args.get("url") or "") if isinstance(args, dict) else ""
                    match = re.match(r"https?://[^/:]+:(\d+)(?:/|$)", url, re.I)
                    if match:
                        http_ports.add(int(match.group(1)))
            grounded = any(
                port in frozen_host_ports
                and any(
                    consumer.rsplit(".", 1)[0] in nginx_ids
                    for consumer in frozen_host_ports[port].consumers
                )
                and not any(
                    port in used_ports
                    for other_step, used_ports in used_ports_by_step.items()
                    if other_step not in nginx_ids
                )
                for port in http_ports
            )
            if nginx_changes and not grounded:
                errors.append(
                    "isolated Nginx requires an explicit frozen dedicated "
                    "listen port=%s" % ",".join(sorted(nginx_ids))
                )
        errors.extend(
            V4ChangePlannerAgent._complete_klonet_contract_errors(data, resources)
        )
        return errors

    @staticmethod
    def _complete_klonet_contract_errors(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> list[str]:
        """Keep a requested complete platform from degrading into a partial start."""

        goal = str(data.get("goal") or "")
        if not (
            re.search(r"(?:complete|full|fully operational|完整|全量)", goal, re.I)
            and re.search(r"(?:klonet|platform|instance|平台|实例)", goal, re.I)
        ):
            return []
        payload = json.dumps(data.get("changes", []), ensure_ascii=False).lower()
        component_patterns = {
            "master": r"\bmaster\b",
            "worker": r"\bworker\b",
            "celery": r"\bcelery\b",
            "web_terminal": r"web[_ -]?terminal|_web\b",
        }
        missing_components = [
            name
            for name, pattern in component_patterns.items()
            if not re.search(pattern, payload, re.I)
        ]
        errors = []
        if "data_server" in payload or any(
            "data_server" in "%s %s" % (resource.name, resource.role)
            for resource in resources
        ):
            errors.append(
                "complete Klonet runtime includes unsupported data_server component"
            )
        if re.search(
            r"(?:database|schema).{0,24}(?:migration|initialize|initialization|seed)|"
            r"(?:migration|initialize|initialization|seed).{0,24}(?:database|schema)|"
            r"数据库.{0,16}(?:迁移|初始化|种子)|(?:迁移|初始化|种子).{0,16}数据库",
            payload,
            re.I,
        ):
            errors.append(
                "complete Klonet runtime invents unsupported database initialization step"
            )
        if missing_components:
            errors.append(
                "complete Klonet runtime missing components=%s"
                % ",".join(missing_components)
            )
        role_texts = {
            re.sub(
                r"[^a-z0-9_]+",
                "_",
                "%s %s" % (resource.name, resource.role),
            ).strip("_")
            for resource in resources
            if resource.status == "frozen" and resource.kind == "port"
        }
        missing_ports = [
            name
            for name in ("master_port", "worker_port", "web_terminal_port")
            if not any(name in text for text in role_texts)
        ]
        if missing_ports:
            errors.append(
                "complete Klonet runtime missing port resources=%s"
                % ",".join(missing_ports)
            )
        instance = next(
            (
                str(resource.value)
                for resource in resources
                if resource.status == "frozen"
                and resource.role in {
                    "instance_identifier", "instance_name", "platform_instance_name"
                }
            ),
            "",
        )
        if instance:
            sessions = {
                str((check.get("args") or {}).get("session") or "")
                for change in data.get("changes", [])
                if isinstance(change, dict)
                for check in change.get("postconditions", [])
                if isinstance(check, dict)
                and check.get("checker") == "screen_session_exists"
            }
            expected = {"%s_%s" % (instance, suffix) for suffix in ("m", "c", "web", "w")}
            missing_sessions = sorted(expected - sessions)
            if missing_sessions:
                errors.append(
                    "complete Klonet runtime missing Screen sessions=%s"
                    % ",".join(missing_sessions)
                )
        changes = [
            change for change in data.get("changes", []) if isinstance(change, dict)
        ]
        config_payload = "\n".join(
            json.dumps(change, ensure_ascii=False).lower()
            for change in changes
            if re.search(
                r"config|配置",
                "%s %s" % (change.get("title", ""), change.get("objective", "")),
                re.I,
            )
        )
        required_attributes = (
            "master_port", "worker_port", "web_terminal_port",
            "mysql_port", "redis_port", "rabbitmq_port",
        )
        missing_attributes = [
            attribute
            for attribute in required_attributes
            if attribute not in config_payload
        ]
        if missing_attributes:
            errors.append(
                "complete Klonet configuration missing attributes=%s"
                % ",".join(missing_attributes)
            )
        master_port = next(
            (
                int(resource.value)
                for resource in resources
                if resource.status == "frozen"
                and resource.kind == "port"
                and "master_port" in "%s %s" % (resource.name, resource.role)
            ),
            None,
        )
        nginx_payload = "\n".join(
            json.dumps(change, ensure_ascii=False).lower()
            for change in changes
            if "nginx" in json.dumps(change, ensure_ascii=False).lower()
        )
        if master_port is not None and (
            not re.search(r"\bmaster\b", nginx_payload, re.I)
            or str(master_port) not in nginx_payload
        ):
            errors.append(
                "complete Klonet Nginx must proxy to frozen master_port=%s"
                % master_port
            )
        return errors

    @staticmethod
    def _normalize_change_order(data: dict[str, Any]) -> None:
        """Put model-emitted semantic changes into deterministic DAG order."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        items = [item for item in changes if isinstance(item, dict)]
        if len(items) != len(changes):
            return
        ids = [str(item.get("step_id") or "") for item in items]
        if any(not step_id for step_id in ids) or len(set(ids)) != len(ids):
            return
        known = set(ids)
        dependencies: dict[str, list[str]] = {}
        for item, step_id in zip(items, ids):
            raw = item.get("depends_on", [])
            if not isinstance(raw, list):
                return
            dependencies[step_id] = [str(value) for value in raw]
        if any(
            dependency not in known
            for values in dependencies.values()
            for dependency in values
        ):
            return
        remaining = list(ids)
        emitted: list[str] = []
        while remaining:
            ready = [
                step_id
                for step_id in remaining
                if all(dep in emitted for dep in dependencies[step_id])
            ]
            if not ready:
                return
            for step_id in ready:
                emitted.append(step_id)
                remaining.remove(step_id)
        by_id = dict(zip(ids, items))
        data["changes"] = [by_id[step_id] for step_id in emitted]

    @staticmethod
    def _normalize_verification_changes(data: dict[str, Any]) -> None:
        """Compile attributable verification leaves into the V phase contract."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        items = [item for item in changes if isinstance(item, dict)]
        by_id = {
            str(item.get("step_id") or ""): item
            for item in items
            if str(item.get("step_id") or "")
        }
        removed: dict[str, list[str]] = {}
        for item in items:
            step_id = str(item.get("step_id") or "")
            primary = str(item.get("title") or item.get("objective") or "").strip()
            if not re.match(
                r"^(?:verify|validate|check|confirm|assert)\b|"
                r"^(?:验证|校验|检查|确认|验收)",
                primary,
                re.I,
            ):
                continue
            dependencies = item.get("depends_on")
            if not isinstance(dependencies, list) or not dependencies:
                continue
            dependency_ids = [str(value) for value in dependencies]
            target_id = dependency_ids[-1]
            target = by_id.get(target_id)
            if target is None:
                continue
            target_checks = target.get("postconditions")
            if not isinstance(target_checks, list):
                target_checks = []
                target["postconditions"] = target_checks
            existing = {
                json.dumps(check, ensure_ascii=False, sort_keys=True)
                for check in target_checks
                if isinstance(check, dict)
            }
            for check in item.get("postconditions", []):
                if not isinstance(check, dict):
                    continue
                fingerprint = json.dumps(check, ensure_ascii=False, sort_keys=True)
                if fingerprint not in existing:
                    target_checks.append(check)
                    existing.add(fingerprint)
            removed[step_id] = dependency_ids
        if not removed:
            return
        data["changes"] = [
            item
            for item in items
            if str(item.get("step_id") or "") not in removed
        ]
        for item in data["changes"]:
            dependencies = item.get("depends_on")
            if not isinstance(dependencies, list):
                continue
            rewired: list[str] = []
            for dependency in dependencies:
                dependency_id = str(dependency)
                replacements = removed.get(dependency_id, [dependency_id])
                for replacement in replacements:
                    if replacement not in rewired:
                        rewired.append(replacement)
            item["depends_on"] = rewired
        resources = data.get("resources")
        if not isinstance(resources, list):
            return
        for resource in resources:
            consumers = resource.get("consumers") if isinstance(resource, dict) else None
            if not isinstance(consumers, list):
                continue
            rewired_consumers: list[str] = []
            for consumer in consumers:
                text = str(consumer)
                owner, separator, field = text.partition(".")
                replacements = removed.get(owner, [owner])
                for replacement in replacements:
                    rewritten = "%s%s%s" % (replacement, separator, field)
                    if rewritten not in rewired_consumers:
                        rewired_consumers.append(rewritten)
            resource["consumers"] = rewired_consumers

    @staticmethod
    def _strip_negated_reuse_claims(text: str) -> str:
        return re.sub(
            r"(?:never|no|not|without|do\s+not|must\s+not|禁止|不要|不得|不应|不复用)"
            r"[^.!?。！？]{0,30}(?:reuse|share|复用|共享)"
            r"[^.!?。！？]{0,40}(?:existing|container|现有|容器)",
            "",
            text,
            flags=re.I,
        )

    @staticmethod
    def _normalize_derived_resources(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> list[PlanResource]:
        normalized = list(resources)
        by_port = {
            int(item.value): item
            for item in normalized
            if V4ChangePlannerAgent._requires_host_port_availability(item)
        }
        explicit_internal_ports = {
            int(item.value)
            for item in normalized
            if item.status == "frozen"
            and item.kind == "port"
            and not V4ChangePlannerAgent._requires_host_port_availability(item)
        }
        changes = data.get("changes")
        if isinstance(changes, list):
            standard_internal_ports = {
                "mysql": 3306,
                "redis": 6379,
                "rabbitmq": 5672,
            }
            for change in changes:
                if not isinstance(change, dict):
                    continue
                step_id = str(change.get("step_id") or "")
                text = json.dumps(
                    {
                        "title": change.get("title", ""),
                        "objective": change.get("objective", ""),
                        "expected_changes": change.get("expected_changes", []),
                    },
                    ensure_ascii=False,
                ).lower()
                if not re.search(r"container|容器", text, re.I):
                    continue
                for service, port in standard_internal_ports.items():
                    if service not in text:
                        continue
                    consumer = "%s.%s_internal_port" % (step_id, service)
                    existing = next(
                        (
                            item
                            for item in normalized
                            if item.status == "frozen"
                            and item.kind == "port"
                            and int(item.value) == port
                            and not V4ChangePlannerAgent._requires_host_port_availability(item)
                        ),
                        None,
                    )
                    if existing is not None:
                        if consumer not in existing.consumers:
                            existing.consumers.append(consumer)
                        continue
                    normalized.append(
                        PlanResource(
                            name="%s_container_internal_port" % service,
                            kind="port",
                            status="frozen",
                            role="container_internal_port",
                            value=port,
                            source="standard_service_contract",
                            consumers=[consumer],
                        )
                    )
                    explicit_internal_ports.add(port)
            for change in changes:
                if not isinstance(change, dict):
                    continue
                step_id = str(change.get("step_id") or "")
                postconditions = change.get("postconditions")
                if not isinstance(postconditions, list):
                    continue
                for check in postconditions:
                    if not isinstance(check, dict) or check.get("checker") != "git_revision":
                        continue
                    args = check.get("args")
                    revision = (
                        str(args.get("revision") or "").strip()
                        if isinstance(args, dict)
                        else ""
                    )
                    if not revision:
                        continue
                    consumer = "%s.revision" % step_id
                    existing = next(
                        (
                            item
                            for item in normalized
                            if item.status == "frozen"
                            and item.role == "source_revision"
                            and str(item.value) == revision
                        ),
                        None,
                    )
                    if existing is not None:
                        if consumer not in existing.consumers:
                            existing.consumers.append(consumer)
                        continue
                    normalized.append(
                        PlanResource(
                            name="source_revision",
                            kind="identifier",
                            status="frozen",
                            role="source_revision",
                            value=revision,
                            source="derived_from_git_revision_postcondition",
                            consumers=[consumer],
                        )
                    )
        for step_id, ports in V4ChangePlannerAgent._used_ports_by_step(data).items():
            for port in sorted(ports):
                consumer = "%s.port_%s" % (step_id, port)
                existing = by_port.get(port)
                if existing is not None:
                    if consumer not in existing.consumers:
                        existing.consumers.append(consumer)
                    continue
                if port in explicit_internal_ports:
                    continue
                resource = PlanResource(
                    name="derived_host_port_%s" % port,
                    kind="port",
                    status="frozen",
                    role="selected_host_port",
                    value=port,
                    source="derived_from_change_contract",
                    consumers=[consumer],
                )
                normalized.append(resource)
                by_port[port] = resource

        root = next(
            (
                item
                for item in normalized
                if item.status == "frozen"
                and item.kind == "path"
                and str(item.role or "").lower()
                in {"instance_root", "target_root", "deployment_root"}
            ),
            None,
        )
        if root is None:
            return V4ChangePlannerAgent._normalize_consumer_owners(normalized)
        root_text = str(root.value).rstrip("/")
        changes = data.get("changes")
        if not isinstance(changes, list):
            return V4ChangePlannerAgent._normalize_consumer_owners(normalized)
        for change in changes:
            if not isinstance(change, dict):
                continue
            step_id = str(change.get("step_id") or "")
            postconditions = change.get("postconditions")
            if not isinstance(postconditions, list):
                continue
            future_paths = []
            for check in postconditions:
                args = check.get("args") if isinstance(check, dict) else None
                path = str(args.get("path") or "") if isinstance(args, dict) else ""
                if path.startswith(root_text + "/") and path != root_text + "/.git":
                    if path not in future_paths:
                        future_paths.append(path)
            for path_index, path in enumerate(future_paths, start=1):
                consumer = "%s.path%s" % (
                    step_id,
                    "_%s" % path_index if len(future_paths) > 1 else "",
                )
                existing = next(
                    (
                        item
                        for item in normalized
                        if item.status == "frozen"
                        and item.kind == "path"
                        and str(item.value) == path
                    ),
                    None,
                )
                if existing is not None:
                    if consumer not in existing.consumers:
                        existing.consumers.append(consumer)
                    continue
                normalized.append(
                    PlanResource(
                        name="derived_path_%s" % (len(normalized) + 1),
                        kind="path",
                        status="frozen",
                        role="derived_configuration_path",
                        value=path,
                        source="derived_from_instance_root",
                        consumers=[consumer],
                    )
                )
        return V4ChangePlannerAgent._normalize_consumer_owners(normalized)

    @staticmethod
    def _normalize_consumer_owners(
        resources: list[PlanResource],
    ) -> list[PlanResource]:
        """Give ambiguous model consumers deterministic role-specific slots."""

        claimants: dict[str, list[PlanResource]] = {}
        for resource in resources:
            for consumer in resource.consumers:
                claimants.setdefault(consumer, []).append(resource)
        reserved = set(claimants)
        for consumer, owners in claimants.items():
            unique_owners = list(dict.fromkeys(owner.name for owner in owners))
            if len(unique_owners) < 2:
                continue
            semantic_id, field = consumer.rsplit(".", 1)
            scored = sorted(
                (
                    V4ChangePlannerAgent._consumer_owner_score(owner, field),
                    index,
                    owner,
                )
                for index, owner in enumerate(owners)
            )
            best_score = scored[-1][0]
            best = None
            if best_score > 0 and sum(
                1 for score, _, _ in scored if score == best_score
            ) == 1:
                best = scored[-1][2]
                reserved.add(consumer)
            for owner in owners:
                if owner is best:
                    continue
                replacement_field = re.sub(
                    r"[^a-z0-9_]+",
                    "_",
                    str(owner.role or owner.name).lower(),
                ).strip("_") or "resource"
                replacement = "%s.%s" % (semantic_id, replacement_field)
                suffix = 2
                while replacement in reserved:
                    replacement = "%s.%s_%s" % (
                        semantic_id,
                        replacement_field,
                        suffix,
                    )
                    suffix += 1
                owner.consumers = [
                    replacement if item == consumer else item
                    for item in owner.consumers
                ]
                reserved.add(replacement)
        return resources

    @staticmethod
    def _consumer_owner_score(resource: PlanResource, field: str) -> int:
        candidates = {
            re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_")
            for value in (resource.role, resource.name)
            if str(value or "").strip()
        }
        if field in candidates:
            return 2
        if any(candidate.endswith("_" + field) for candidate in candidates):
            return 1
        return 0

    @staticmethod
    def _used_ports_by_step(data: dict[str, Any]) -> dict[str, set[int]]:
        result: dict[str, set[int]] = {}
        changes = data.get("changes")
        if not isinstance(changes, list):
            return result
        for change in changes:
            if not isinstance(change, dict):
                continue
            step_id = str(change.get("step_id") or "")
            ports: set[int] = set()
            text_parts = [
                str(change.get("title") or ""),
                str(change.get("objective") or ""),
            ]
            expected = change.get("expected_changes")
            if isinstance(expected, list):
                text_parts.extend(str(item) for item in expected)
            text = "\n".join(text_parts)
            for match in re.findall(
                r"(?:\bport\b|_port|\u7aef\u53e3).{0,20}?([1-9]\d{1,4})(?![\d.])",
                text,
                re.I,
            ):
                port = int(match)
                if 1 <= port <= 65535:
                    ports.add(port)
            postconditions = change.get("postconditions")
            if isinstance(postconditions, list):
                for check in postconditions:
                    args = check.get("args") if isinstance(check, dict) else None
                    if not isinstance(args, dict):
                        continue
                    if str(check.get("checker") or "") == "port_listening":
                        try:
                            port = int(args.get("port"))
                        except (TypeError, ValueError):
                            port = 0
                        if 1 <= port <= 65535:
                            ports.add(port)
                    for value in args.values():
                        if not isinstance(value, str):
                            continue
                        for match in re.findall(r"https?://[^\s/:]+:(\d{1,5})", value):
                            port = int(match)
                            if 1 <= port <= 65535:
                                ports.add(port)
            result[step_id] = ports
        return result

    @staticmethod
    def _declared_listening_ports_by_step(
        data: dict[str, Any],
    ) -> dict[str, set[int]]:
        """Return ports a change claims to bind, excluding observation URLs."""

        result: dict[str, set[int]] = {}
        changes = data.get("changes")
        if not isinstance(changes, list):
            return result
        for change in changes:
            if not isinstance(change, dict):
                continue
            step_id = str(change.get("step_id") or "")
            ports: set[int] = set()
            text_parts = [
                str(change.get("title") or ""),
                str(change.get("objective") or ""),
            ]
            expected = change.get("expected_changes")
            if isinstance(expected, list):
                text_parts.extend(str(item) for item in expected)
            text = "\n".join(text_parts)
            for match in re.findall(
                r"(?:listen(?:s|ing)?(?:\s+on)?|监听).{0,20}?([1-9]\d{1,4})",
                text,
                re.I,
            ):
                port = int(match)
                if 1 <= port <= 65535:
                    ports.add(port)
            postconditions = change.get("postconditions")
            if isinstance(postconditions, list):
                for check in postconditions:
                    if not isinstance(check, dict) or check.get("checker") != "port_listening":
                        continue
                    args = check.get("args")
                    try:
                        port = int(args.get("port")) if isinstance(args, dict) else 0
                    except (TypeError, ValueError):
                        port = 0
                    if 1 <= port <= 65535:
                        ports.add(port)
            result[step_id] = ports
        return result

    @staticmethod
    def _unproven_port_resources(
        resources: list[PlanResource],
        bundle: EvidenceBundle,
    ) -> list[PlanResource]:
        unproven = []
        for resource in resources:
            if not V4ChangePlannerAgent._requires_host_port_availability(resource):
                continue
            port = int(resource.value)
            if not any(
                record.request.probe == "ports"
                and port in record.request.args.get("ports", [])
                for record in bundle.records
            ):
                unproven.append(resource)
        return unproven

    @staticmethod
    def _requires_host_port_availability(resource: PlanResource) -> bool:
        if resource.status != "frozen" or resource.kind != "port":
            return False
        role_and_name = "%s %s" % (resource.role or "", resource.name or "")
        lowered = role_and_name.lower()
        return not (
            "internal" in lowered
            or "container_port" in lowered
            or "image_port" in lowered
        )

    @staticmethod
    def _steps(value: Any, bundle: EvidenceBundle) -> list[ChangeStepV4]:
        if not isinstance(value, list) or not value:
            raise ValueError("ready Change Planner output requires changes")
        known = bundle.evidence_ids
        steps = []
        known_step_ids = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("change must be an object")
            refs = [str(ref) for ref in item.get("evidence_refs", [])]
            unknown = [ref for ref in refs if ref not in known]
            if unknown:
                raise ValueError("unknown evidence reference: %s" % ", ".join(unknown))
            dependencies = [str(dep) for dep in item.get("depends_on", [])]
            if any(dep not in known_step_ids for dep in dependencies):
                raise ValueError("change dependency must reference an earlier step")
            step = ChangeStepV4(
                step_id=str(item.get("step_id") or ""),
                title=str(item.get("title") or ""),
                objective=str(item.get("objective") or ""),
                reason=str(item.get("reason") or ""),
                evidence_refs=refs,
                depends_on=dependencies,
                risk=str(item.get("risk") or ""),
                expected_changes=[str(change) for change in item.get("expected_changes", [])],
                postconditions=[
                    dict(check)
                    for check in item.get("postconditions", [])
                    if isinstance(check, dict)
                ],
            )
            if not step.step_id or step.step_id in known_step_ids:
                raise ValueError("change step_id must be unique and non-empty")
            known_step_ids.add(step.step_id)
            steps.append(step)
        return steps

    @staticmethod
    def _probe_requests(value: Any) -> list[ProbeRequest]:
        if not isinstance(value, list):
            return []
        return [
            ProbeRequest(
                str(item.get("probe") or ""),
                item.get("args") if isinstance(item.get("args"), dict) else {},
                str(item.get("purpose") or "resolve planning evidence gap"),
            )
            for item in value
            if isinstance(item, dict)
        ]

    @staticmethod
    def _evidence_json(bundle: EvidenceBundle) -> str:
        return json.dumps(
            [
                {
                    "evidence_id": item.evidence_id,
                    "probe": item.request.probe,
                    "status": item.status,
                    "output": item.output[:7000],
                }
                for item in bundle.records
            ],
            ensure_ascii=False,
        )

    @staticmethod
    def _conclusion_json(conclusion: EvidenceConclusion) -> str:
        return json.dumps(
            {
                "confirmed_facts": [
                    {"text": item.text, "evidence_refs": item.evidence_refs}
                    for item in conclusion.confirmed_facts
                ],
                "uncertainties": [
                    {"text": item.text, "evidence_refs": item.evidence_refs}
                    for item in conclusion.uncertainties
                ],
                "missing_decisions": conclusion.missing_decisions,
            },
            ensure_ascii=False,
        )
