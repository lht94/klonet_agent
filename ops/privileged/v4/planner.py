"""Mutation-only Change Planner for Ops-Privilege V4."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from klonet_agent.ops.privileged.contracts import PlanResource, RISK_LEVELS
from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
from klonet_agent.ops.privileged.checkers import (
    CHECKER_REQUIRED_ARGS,
    DefaultCheckerRegistry,
)
from klonet_agent.ops.privileged.v4.contracts import (
    ChangePlanV4,
    ChangeStepV4,
    EvidenceBundle,
    EvidenceConclusion,
    EvidenceRecord,
    ProbeRequest,
    normalize_probe_request,
)
from klonet_agent.ops.privileged.v4.discovery import parse_json_object


def _inventory_missing_runtime_roles(
    bundle: EvidenceBundle,
) -> list[tuple[str, str, set[str]]]:
    missing = []
    for record in bundle.records:
        if record.request.probe != "running_platforms":
            continue
        for line in record.output.splitlines():
            if "backend_status=abnormal" not in line:
                continue
            root_match = re.search(r"\bproject_root=(/[^\s]+)", line)
            alias_match = re.search(r"\bplatform=([^\s]+)", line)
            roles = {
                role
                for role in ("master", "worker")
                if re.search(
                    r"\b%s_endpoint=not_checked\s+reason=role_not_running\b"
                    % role,
                    line,
                )
            }
            if root_match is not None and roles:
                missing.append(
                    (
                        root_match.group(1),
                        alias_match.group(1) if alias_match else "",
                        roles,
                    )
                )
    return missing


def _request_rechecks_confirmed_missing_role(
    request: ProbeRequest,
    missing: list[tuple[str, str, set[str]]],
) -> bool:
    if request.probe not in {"process", "process_detail", "screen", "screen_session"}:
        return False
    text = "%s %s" % (
        json.dumps(request.args, ensure_ascii=False),
        request.purpose,
    )
    lowered = text.lower()
    for root, alias, roles in missing:
        selected = (
            root in text
            or bool(alias and re.search(
                r"(?<![A-Za-z0-9_.:-])%s(?![A-Za-z0-9_.:-])"
                % re.escape(alias),
                text,
                re.I,
            ))
            or len(missing) == 1
        )
        if not selected:
            continue
        for role in roles:
            suffix = "_m" if role == "master" else "_w"
            if role in lowered or suffix in lowered:
                return True
    return False


CHANGE_PLANNER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege V4 Change Planner.
Plan only real host state changes. Discovery, inspection, evidence aggregation,
summaries, reports, answers and verification are separate workflow phases and
must never appear as changes. Do not select Action names or emit commands.
When the evidence conclusion contains a deterministic `User-selected Screen
source maps authoritatively` fact with repository, remote, branch and revision,
use it as the source contract and do not request source discovery again.
When `plan_execution` evidence is present, this is recovery replanning after an
approved plan failed verification. Preserve steps already proved completed,
never emit them again, and plan only the failed or still-unmet effects. Use the
execution observation, checks, and fresh runtime evidence to address the actual
failure instead of blindly retrying the old plan.

Return one JSON object with status `need_evidence`, `ready`, or `blocked`.
For need_evidence return at most four registered read-only probe_requests.
A ports probe must contain at most 64 candidate ports; a small bounded sample
is sufficient and prevents evidence requests from dominating the plan JSON.
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
those exact Python attributes as well as mysql_port, redis_port,
rabbitmq_port, master_ip, mysql_ip, rabbitmq_ip, celery_redis_port_db and
celery_rabbitmq_port_db when isolated stateful containers are planned. For a
same-host isolated instance with loopback-only container publishing, set the
three *_ip fields to 127.0.0.1, set both Celery Redis DB endpoint strings to
the frozen Redis host port plus their existing /6 and /7 DB suffixes, and
keep the top-level `PROJ_CONFIG = WtxConfig()` activation. Existing cloned
MySQL/Redis credentials are consumed locally by the container Action and must
not be copied into the model response. The Nginx site
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
        deterministic = self._deterministic_runtime_restart(goal, bundle)
        if deterministic is not None:
            try:
                return self._outcome(deterministic, goal, bundle)
            except ValueError:
                # Keep the existing bounded planner as a compatibility fallback
                # when evidence is insufficient for the deterministic contract.
                pass
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
        if len(set(re.findall(r"/[A-Za-z0-9._/-]+", goal))) >= 2:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "This is a multi-root repair. Return at most 4 semantic changes "
                        "and 16 resources. Group each instance's cohesive configuration "
                        "and runtime recovery into one semantic change; leave atomic file, "
                        "process, and component actions to Binding. Do not repeat evidence "
                        "or narrate per-process diagnostics in expected_changes."
                    ),
                }
            )
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
        oversized_repairs = 0
        max_generations = 4
        for attempt in range(max_generations):
            try:
                response = self._complete(messages)
                choices = getattr(response, "choices", None)
                message = getattr(choices[0], "message", None) if choices else None
                content = self._raw_planner_content(message)
                data, normalized_content = self._planner_payload(response)
                content = normalized_content or content
                return self._outcome(data, goal, bundle)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < max_generations - 1:
                    oversized_output = len(content) > 12000
                    if oversized_output and oversized_repairs >= 1:
                        last_error = ValueError(
                            "bounded output remained oversized after compact retry"
                        )
                        break
                    if oversized_output:
                        oversized_repairs += 1
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
                                + (
                                    " The previous output exceeded the bounded contract. "
                                    "Return at most 4 semantic changes and 16 resources; "
                                    "group cohesive work by instance and leave atomic "
                                    "implementation steps to Binding."
                                    if oversized_output
                                    else ""
                                )
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

    @staticmethod
    def _deterministic_runtime_restart(
        goal: str,
        bundle: EvidenceBundle,
    ) -> dict[str, Any] | None:
        """Compile explicit master/worker restart intent from runtime evidence."""

        text = str(goal or "")
        lowered = text.lower()
        if not any(marker in lowered for marker in ("重启", "restart")):
            return None
        requested_roles = [
            role for role in ("master", "worker")
            if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % role, lowered)
        ]
        if not requested_roles:
            return None
        selected: tuple[EvidenceRecord, str, str] | None = None
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "running_platforms":
                continue
            for line in record.output.splitlines():
                root_match = re.search(r"\bproject_root=(/[^\s]+)", line)
                alias_match = re.search(r"\bplatform=([^\s]+)", line)
                if root_match is None:
                    continue
                root = root_match.group(1)
                alias = alias_match.group(1) if alias_match else Path(root).name
                if root in text or re.search(
                    r"(?<![A-Za-z0-9_.:-])%s(?![A-Za-z0-9_.:-])"
                    % re.escape(alias),
                    text,
                    re.I,
                ):
                    selected = (record, line, alias)
                    break
            if selected is not None:
                break
        if selected is None:
            return None
        record, line, alias = selected
        root = re.search(r"\bproject_root=(/[^\s]+)", line).group(1)
        step_id = "restart-backend-roles"
        resources: list[dict[str, Any]] = [
            {
                "name": "instance_root", "kind": "path", "status": "frozen",
                "role": "instance_root", "value": root,
                "source": "running_platforms",
                "consumers": [step_id + ".instance_root", step_id + ".project_root"],
            },
            {
                "name": "instance_identifier", "kind": "identifier",
                "status": "frozen", "role": "instance_identifier", "value": alias,
                "source": "running_platforms",
                "consumers": [step_id + ".platform"],
            },
        ]
        expected = []
        postconditions = []
        for role in requested_roles:
            port_match = re.search(r"\b%s_port=(\d{1,5})" % role, line)
            endpoint_match = re.search(r"\b%s_endpoint=([^\s]+)" % role, line)
            if port_match is None or endpoint_match is None:
                return None
            port = int(port_match.group(1))
            endpoint = endpoint_match.group(1)
            disposition = (
                "start missing"
                if endpoint == "not_checked"
                and re.search(
                    r"\b%s_endpoint=not_checked\s+reason=role_not_running" % role,
                    line,
                )
                else "restart requested"
            )
            expected.append(
                "%s %s role at %s and backend health succeeds"
                % (disposition, role, port)
            )
            resources.append({
                "name": role + "_port", "kind": "port", "status": "frozen",
                "role": role + "_port", "value": port,
                "source": "existing_runtime",
                "consumers": [step_id + "." + role + "_port"],
            })
            postconditions.append({
                "checker": "backend_health",
                "args": {
                    "url": "http://127.0.0.1:%s/server_health/" % port,
                    "expected_code": 1,
                },
            })
        identity_text = "\n".join(
            item.output for item in bundle.records if item.status == "available"
        )
        run_as_uid: int | None = None
        python_executable = ""
        process_identity = re.search(
            r"pid=\d+\s+cwd=%s(?:/mains)?\s+cmdline=(/[^\s]*python[^\s]*)"
            % re.escape(root.rstrip("/")),
            identity_text,
        )
        current_uid = re.search(r"\bcurrent_uid=(\d+)\b", identity_text)
        if process_identity is not None and current_uid is not None:
            run_as_uid = int(current_uid.group(1))
            python_executable = process_identity.group(1)
        else:
            # Identity tuples must come from the selected instance row; a
            # global first match could silently borrow another root's UID.
            identity = re.search(
                r"(?:^|[=,\s])(\d+):(\d+):(/[^,\s]*python[^,\s]*)",
                line,
            )
            if identity is not None:
                run_as_uid = int(identity.group(2))
                python_executable = identity.group(3)
        if run_as_uid is not None and python_executable:
            resources.extend([
                {
                    "name": "run_as_uid", "kind": "identifier", "status": "frozen",
                    "role": "run_as_uid", "value": run_as_uid,
                    "source": "runtime_evidence",
                    "consumers": [step_id + ".run_as_uid"],
                },
                {
                    "name": "python_executable", "kind": "path", "status": "frozen",
                    "role": "python_executable", "value": python_executable,
                    "source": "runtime_evidence",
                    "consumers": [step_id + ".python_executable"],
                },
            ])
        return {
            "status": "ready",
            "goal": goal,
            "assumptions": [],
            "resources": resources,
            "changes": [{
                "step_id": step_id,
                "title": "重启 %s 的后端角色" % alias,
                "objective": "按项目根目录 %s 重启 %s" % (
                    root, " 和 ".join(requested_roles),
                ),
                "reason": "用户明确要求重启，运行清单已绑定实例根目录和角色端口",
                "evidence_refs": [record.evidence_id],
                "depends_on": [],
                "risk": "medium",
                "expected_changes": expected,
                "postconditions": postconditions,
            }],
        }

    @staticmethod
    def _raw_planner_content(message: Any) -> str:
        if message is None:
            return ""
        calls = getattr(message, "tool_calls", None)
        if calls:
            function = getattr(calls[0], "function", None)
            arguments = getattr(function, "arguments", None)
            if arguments is not None:
                if isinstance(arguments, dict):
                    return json.dumps(arguments, ensure_ascii=False)
                return str(arguments)
        return str(getattr(message, "content", None) or "")

    def _complete(self, messages: list[dict[str, str]]) -> Any:
        tool = self._planner_tool()
        choice = {
            "type": "function",
            "function": {"name": "submit_v4_change_plan"},
        }
        try:
            return self.llm.complete(
                messages=messages,
                tools=[tool],
                tool_choice=choice,
                reasoning_effort="medium",
                temperature=0,
                max_tokens=8000,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except TypeError:
            return self.llm.complete(messages=messages, tools=[tool])

    @staticmethod
    def _planner_tool() -> dict[str, Any]:
        text = {"type": "string", "maxLength": 500}
        checker_args = {
            "type": "object",
            "description": "Arguments for the named registered checker.",
            "additionalProperties": True,
        }
        return {
            "type": "function",
            "function": {
                "name": "submit_v4_change_plan",
                "description": (
                    "Submit one bounded semantic V4 planning outcome. This is the "
                    "only accepted Change Planner response channel."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["need_evidence", "ready", "blocked"],
                        },
                        "goal": {"type": "string", "maxLength": 1000},
                        "reason": {"type": "string", "maxLength": 1000},
                        "missing_decisions": {
                            "type": "array",
                            "maxItems": 8,
                            "items": text,
                        },
                        "probe_requests": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "probe": {"type": "string", "maxLength": 100},
                                    "args": {
                                        "type": "object",
                                        "properties": {
                                            "ports": {
                                                "type": "array",
                                                "maxItems": 64,
                                                "items": {"type": "integer"},
                                            },
                                            "candidates": {
                                                "type": "array",
                                                "maxItems": 64,
                                                "items": {"type": "integer"},
                                            },
                                            "candidate_ports": {
                                                "type": "array",
                                                "maxItems": 64,
                                                "items": {"type": "integer"},
                                            },
                                        },
                                        "additionalProperties": True,
                                    },
                                    "purpose": text,
                                },
                                "required": ["probe", "args", "purpose"],
                                "additionalProperties": False,
                            },
                        },
                        "assumptions": {
                            "type": "array",
                            "maxItems": 12,
                            "items": text,
                        },
                        "resources": {
                            "type": "array",
                            "maxItems": 64,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "maxLength": 100},
                                    "kind": {"type": "string", "maxLength": 50},
                                    "status": {
                                        "type": "string",
                                        "enum": ["frozen", "deferred"],
                                    },
                                    "role": {"type": "string", "maxLength": 100},
                                    "value": {},
                                    "source": {"type": "string", "maxLength": 200},
                                    "consumers": {
                                        "type": "array",
                                        "maxItems": 24,
                                        "items": {
                                            "type": "string",
                                            "maxLength": 150,
                                        },
                                    },
                                },
                                "required": [
                                    "name", "kind", "status", "role", "source",
                                    "consumers",
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "changes": {
                            "type": "array",
                            "maxItems": 12,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step_id": {"type": "string", "maxLength": 100},
                                    "title": text,
                                    "objective": {"type": "string", "maxLength": 1000},
                                    "reason": {"type": "string", "maxLength": 1000},
                                    "evidence_refs": {
                                        "type": "array",
                                        "maxItems": 24,
                                        "items": {"type": "string", "maxLength": 100},
                                    },
                                    "depends_on": {
                                        "type": "array",
                                        "maxItems": 12,
                                        "items": {"type": "string", "maxLength": 100},
                                    },
                                    "risk": {
                                        "type": "string",
                                        "enum": ["low", "medium", "high", "critical"],
                                    },
                                    "expected_changes": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 24,
                                        "items": text,
                                    },
                                    "postconditions": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 24,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "checker": {
                                                    "type": "string",
                                                    "enum": list(
                                                        DefaultCheckerRegistry().names
                                                    ),
                                                },
                                                "args": checker_args,
                                            },
                                            "required": ["checker", "args"],
                                            "additionalProperties": False,
                                        },
                                    },
                                },
                                "required": [
                                    "step_id", "title", "objective", "reason",
                                    "evidence_refs", "depends_on", "risk",
                                    "expected_changes", "postconditions",
                                ],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["status"],
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _planner_payload(response: Any) -> tuple[dict[str, Any], str]:
        choices = getattr(response, "choices", None)
        if not choices:
            raise ValueError("planner response missing choices")
        message = getattr(choices[0], "message", None)
        if message is None:
            raise ValueError("planner response missing message")
        calls = getattr(message, "tool_calls", None)
        if calls:
            if len(calls) != 1:
                raise ValueError("planner must emit exactly one function call")
            function = getattr(calls[0], "function", None)
            name = getattr(function, "name", None)
            arguments = getattr(function, "arguments", None)
            if str(name or "") != "submit_v4_change_plan":
                raise ValueError("unexpected planner function call")
            if isinstance(arguments, dict):
                data = arguments
            else:
                data = json.loads(str(arguments or ""))
            if not isinstance(data, dict):
                raise ValueError("planner function arguments must be an object")
            return data, json.dumps(data, ensure_ascii=False)

        # Compatibility for lightweight test doubles which predate tool calls.
        # The production request above always forces submit_v4_change_plan.
        content = getattr(message, "content", None) or ""
        return parse_json_object(content), content

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
            impossible_process_logs = [
                request
                for request in requests
                if request.probe == "process_logs"
                and (
                    not isinstance(request.args.get("pids"), list)
                    or not any(
                        str(pid).isdigit() and int(pid) > 1
                        for pid in request.args.get("pids", [])
                    )
                    or not str(request.args.get("project_root") or "").startswith("/")
                )
            ]
            if impossible_process_logs and len(impossible_process_logs) == len(requests):
                raise ValueError(
                    "process_logs requires an existing PID list and project_root; "
                    "a role confirmed as role_not_running has no process log to probe. "
                    "Use the frozen missing-role, configuration, dependency, and port "
                    "evidence to return a ready plan or a genuine blocker."
                )
            if impossible_process_logs:
                requests = [
                    request
                    for request in requests
                    if request not in impossible_process_logs
                ]
            missing_roles = _inventory_missing_runtime_roles(bundle)
            redundant_absence_requests = [
                request
                for request in requests
                if _request_rechecks_confirmed_missing_role(
                    request, missing_roles
                )
            ]
            if redundant_absence_requests and len(redundant_absence_requests) == len(requests):
                raise ValueError(
                    "running_platforms already proves the requested runtime role is "
                    "role_not_running for its project_root. Do not re-query process or "
                    "screen presence for that role; start_screen_component handles a "
                    "stale/dead target session safely. Return a ready plan or a genuine "
                    "non-presence blocker."
                )
            if redundant_absence_requests:
                requests = [
                    request
                    for request in requests
                    if request not in redundant_absence_requests
                ]
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
        self._normalize_instance_container_names(data)
        self._normalize_ungrounded_dependency_installs(data)
        self._normalize_semantic_dependencies(data)
        self._normalize_change_order(data)
        self._normalize_verification_changes(data)
        self._normalize_change_order(data)
        self._normalize_postcondition_args(data)
        self._normalize_port_resource_roles(data)
        self._normalize_runtime_stop_scope(data, goal, bundle)
        self._normalize_runtime_repair_coverage(data, bundle)
        explicit_runtime_restart = (
            any(marker in str(goal or "").lower() for marker in ("重启", "restart"))
            and any(role in str(goal or "").lower() for role in ("master", "worker"))
        )
        if not explicit_runtime_restart:
            self._normalize_healthy_runtime_role_changes(data, bundle)
        self._collapse_redundant_runtime_repair_changes(data)
        resources = [
            PlanResource.from_dict(item)
            for item in data.get("resources", [])
            if isinstance(item, dict)
        ]
        self._normalize_core_resource_consumers(data, resources)
        resources = self._normalize_derived_resources(data, resources)
        self._normalize_existing_config_paths(data, resources)
        self._normalize_resource_consumer_owners(data, resources)
        self._compile_existing_runtime_role_ports(data, resources, bundle)
        self._mark_existing_runtime_ports(data, resources, bundle)
        self._mark_existing_config_ports(data, resources, bundle)
        self._normalize_occupied_host_ports(data, resources, bundle)
        self._normalize_backend_role_health_contracts(data, resources)
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
                probe_requests=self._candidate_evidence_requests(
                    candidate_plan, bundle, unproven_ports
                ),
            )
        if contract_errors:
            raise ValueError("; ".join(contract_errors))
        steps = self._steps(data.get("changes"), bundle)
        risk = max(steps, key=lambda item: RISK_LEVELS.index(item.risk)).risk
        assumptions = data.get("assumptions")
        plan = ChangePlanV4.new(
            goal=goal,
            risk=risk,
            steps=steps,
            resources=resources,
            assumptions=[str(item) for item in assumptions]
            if isinstance(assumptions, list)
            else [],
        )
        if self._plan_needs_docker_images(plan) and not self._has_docker_images(bundle):
            return V4PlanningOutcome(
                status="need_evidence",
                candidate_plan=plan,
                probe_requests=[
                    ProbeRequest(
                        "docker_images",
                        {},
                        "select an already installed image for each new container",
                    )
                ],
            )
        return V4PlanningOutcome(status="ready", plan=plan)

    @staticmethod
    def _normalize_postcondition_args(data: dict[str, Any]) -> None:
        """Compile common model aliases into registered checker contracts."""

        aliases = {
            "git_revision": {"path": "repository"},
            "file_contains": {"content": "text", "pattern": "text"},
            "screen_session_exists": {"name": "session"},
            "process_running": {"name": "pattern"},
            "container_running": {"name": "container"},
        }
        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        config_module = ""
        raw_resources = data.get("resources")
        if isinstance(raw_resources, list):
            if any(
                isinstance(item, dict)
                and str(item.get("value") or "").endswith("/vemu_config/config.py")
                for item in raw_resources
            ):
                config_module = "vemu_config.config"
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
                checker = str(check.get("checker") or "")
                if checker == "python_attribute_equals":
                    if "expected" not in args and "value" in args:
                        args["expected"] = args.pop("value")
                    path = str(args.get("path") or "")
                    if "module" not in args and (
                        path.endswith("/vemu_config/config.py") or config_module
                    ):
                        args["module"] = "vemu_config.config"
                    args.pop("path", None)
                elif checker == "python_import_succeeds":
                    path = str(args.get("path") or "")
                    if "module" not in args and (
                        path.endswith("/vemu_config/config.py") or config_module
                    ):
                        args["module"] = "vemu_config.config"
                    args.pop("path", None)

    @staticmethod
    def _normalize_ungrounded_dependency_installs(data: dict[str, Any]) -> None:
        """Drop standalone dependency installation invented for same-host clones."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        pattern = re.compile(
            r"(?:install|pip).{0,30}(?:python\s+)?(?:dependencies|requirements|packages)|"
            r"(?:dependencies|requirements|packages).{0,30}(?:install|pip)|"
            r"安装.{0,20}(?:依赖|包)|(?:依赖|包).{0,20}安装",
            re.I,
        )
        removed: dict[str, list[str]] = {}
        retained = []
        for change in changes:
            if not isinstance(change, dict):
                retained.append(change)
                continue
            text = "%s %s %s %s" % (
                change.get("title") or "",
                change.get("objective") or "",
                change.get("reason") or "",
                " ".join(str(item) for item in change.get("expected_changes", [])),
            )
            step_id = str(change.get("step_id") or "")
            if step_id and pattern.search(text):
                dependencies = change.get("depends_on")
                removed[step_id] = [
                    str(item) for item in dependencies
                ] if isinstance(dependencies, list) else []
                continue
            retained.append(change)
        if not removed:
            return

        def expand(step_id: str, seen: set[str] | None = None) -> list[str]:
            if step_id not in removed:
                return [step_id]
            seen = set(seen or ())
            if step_id in seen:
                return []
            seen.add(step_id)
            result = []
            for dependency in removed[step_id]:
                for expanded in expand(dependency, seen):
                    if expanded and expanded not in result:
                        result.append(expanded)
            return result

        for change in retained:
            if not isinstance(change, dict):
                continue
            dependencies = change.get("depends_on")
            if not isinstance(dependencies, list):
                continue
            rewired = []
            for dependency in dependencies:
                for expanded in expand(str(dependency)):
                    if expanded not in rewired:
                        rewired.append(expanded)
            change["depends_on"] = rewired
        data["changes"] = retained

    @staticmethod
    def _normalize_instance_container_names(data: dict[str, Any]) -> None:
        """Compile instance service names to the canonical hyphenated form."""

        raw_resources = data.get("resources")
        if not isinstance(raw_resources, list):
            return
        instance = next(
            (
                str(item.get("value") or "").strip()
                for item in raw_resources
                if isinstance(item, dict)
                and str(item.get("role") or "") == "instance_identifier"
            ),
            "",
        )
        if not instance:
            return
        pattern = re.compile(
            r"(?<![A-Za-z0-9])%s_(mysql|redis|rabbitmq)(?![A-Za-z0-9])"
            % re.escape(instance),
            re.I,
        )

        def rewrite(value: Any) -> Any:
            if isinstance(value, str):
                return pattern.sub(
                    lambda match: "%s-%s" % (instance, match.group(1).lower()),
                    value,
                )
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            return value

        rewritten = rewrite(data)
        data.clear()
        data.update(rewritten)

    @staticmethod
    def _normalize_semantic_dependencies(data: dict[str, Any]) -> None:
        """Compile the fixed clone -> stateful -> runtime -> Nginx DAG."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        valid = [item for item in changes if isinstance(item, dict)]
        text = lambda item: "%s %s" % (
            item.get("title") or "", item.get("objective") or ""
        )
        clones = [
            str(item.get("step_id") or "") for item in valid
            if re.search(r"\b(?:clone|checkout)\b|克隆|检出", text(item), re.I)
            and re.search(r"\b(?:git|repository|source)\b|仓库|源码", text(item), re.I)
        ]
        stateful = [
            str(item.get("step_id") or "") for item in valid
            if re.search(r"mysql|redis|rabbitmq", text(item), re.I)
            and re.search(r"containers?\b|容器", text(item), re.I)
            and re.search(r"\b(?:create|provision)\b|创建|部署", text(item), re.I)
        ]
        runtime = [
            str(item.get("step_id") or "") for item in valid
            if re.search(r"\b(?:start|launch)\b|启动", text(item), re.I)
            and re.search(r"screen|master|celery|web.?terminal|worker", text(item), re.I)
            and not re.search(r"mysql|redis|rabbitmq", text(item), re.I)
        ]
        nginx = [
            str(item.get("step_id") or "") for item in valid
            if re.search(r"nginx", text(item), re.I)
            and re.search(r"\b(?:activate|enable|reload|create)\b|激活|启用|重载|创建", text(item), re.I)
        ]
        required_by_id = {
            **{step_id: clones for step_id in stateful},
            **{step_id: stateful for step_id in runtime},
            **{step_id: runtime for step_id in nginx},
        }
        for item in valid:
            step_id = str(item.get("step_id") or "")
            dependencies = item.get("depends_on")
            if not isinstance(dependencies, list):
                continue
            for required in required_by_id.get(step_id, []):
                if required and required != step_id and required not in dependencies:
                    dependencies.append(required)

    @staticmethod
    def _normalize_resource_consumer_owners(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> None:
        """Repair a stale model step id when its field group has one owner."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        known = {
            str(item.get("step_id") or "") for item in changes
            if isinstance(item, dict) and str(item.get("step_id") or "")
        }
        unknown_fields: dict[str, set[str]] = {}
        for resource in resources:
            for consumer in resource.consumers:
                owner, separator, field = str(consumer).partition(".")
                if separator and owner not in known:
                    unknown_fields.setdefault(owner, set()).add(field)
        nginx_ids = [
            str(item.get("step_id") or "") for item in changes
            if isinstance(item, dict)
            and re.search(
                r"nginx",
                "%s %s" % (item.get("title") or "", item.get("objective") or ""),
                re.I,
            )
        ]
        replacements = {}
        nginx_fields = {
            "config_name", "site_name", "proxy_pass", "listen", "listen_port",
            "nginx_port", "http_status", "enabled_path", "nginx_config_path",
        }
        if len(nginx_ids) == 1:
            for owner, fields in unknown_fields.items():
                if fields.intersection(nginx_fields):
                    replacements[owner] = nginx_ids[0]
        for resource in resources:
            resource.consumers = [
                "%s.%s" % (replacements.get(owner, owner), field)
                if separator else str(consumer)
                for consumer in resource.consumers
                for owner, separator, field in [str(consumer).partition(".")]
            ]

        # Model repairs can remove or renumber semantic changes while leaving
        # old resource consumers behind. Unknown owners are never bindable and
        # must not survive merely because a different resource happened to
        # make the stale id look Nginx-related.
        for resource in resources:
            resource.consumers = list(dict.fromkeys(
                consumer
                for consumer in resource.consumers
                if consumer.partition(".")[0] in known
            ))

        change_text = {
            str(item.get("step_id") or ""): json.dumps(
                item, ensure_ascii=False, sort_keys=True
            )
            for item in changes
            if isinstance(item, dict) and str(item.get("step_id") or "")
        }

        # A project root is an instance identity, not a global path hint.  A
        # same-named sibling must never inherit it merely because the model
        # attached both roots to one semantic change.
        for resource in resources:
            if resource.kind != "path" or resource.role != "instance_root":
                continue
            value = str(resource.value or "").rstrip("/")
            matching = [
                step_id for step_id, serialized in change_text.items()
                if value and value in serialized
            ]
            if not matching:
                continue
            grounded = [
                consumer for consumer in resource.consumers
                if consumer.partition(".")[0] in matching
            ]
            grounded_owners = {item.partition(".")[0] for item in grounded}
            for step_id in matching:
                if step_id not in grounded_owners:
                    grounded.append("%s.instance_root" % step_id)
            resource.consumers = list(dict.fromkeys(grounded))

        # Future file paths are stronger evidence than a model-generated
        # change number. Ground them only in semantic changes that explicitly
        # mention the exact path. Keep root and runtime-directory resources on
        # their dedicated repository/project_root bindings.
        for resource in resources:
            value = str(resource.value or "")
            if (
                resource.kind != "path"
                or resource.role in {"instance_root", "runtime_mains_root"}
                or not value.startswith("/")
            ):
                continue
            matching = [
                step_id for step_id, serialized in change_text.items()
                if value in serialized
            ]
            if not matching:
                continue
            grounded = [
                consumer for consumer in resource.consumers
                if consumer.partition(".")[0] in matching
            ]
            grounded_owners = {
                consumer.partition(".")[0] for consumer in grounded
            }
            for step_id in matching:
                if step_id not in grounded_owners:
                    grounded.append("%s.path" % step_id)
            resource.consumers = list(dict.fromkeys(grounded))

        V4ChangePlannerAgent._normalize_consumer_owners(resources)

    @staticmethod
    def _normalize_core_resource_consumers(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> None:
        """Compile authoritative clone slots instead of asking the model to wire them."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        clone_step = next(
            (
                item
                for item in changes
                if isinstance(item, dict)
                and str(item.get("step_id") or "")
                and re.search(
                    r"\b(?:clone|git|repository)\b|克隆|仓库",
                    "%s %s"
                    % (item.get("title") or "", item.get("objective") or ""),
                    re.I,
                )
                and any(
                    isinstance(check, dict)
                    and check.get("checker") == "git_revision"
                    for check in item.get("postconditions", [])
                )
            ),
            None,
        )
        if clone_step is None:
            return
        step_id = str(clone_step["step_id"])
        fields = {
            "instance_root": "repository",
            "target_root": "repository",
            "deployment_root": "repository",
            "source_remote": "url",
            "source_branch": "ref",
            "source_revision": "revision",
        }
        for resource in resources:
            field = fields.get(str(resource.role or "").lower())
            if field is None:
                continue
            consumer = "%s.%s" % (step_id, field)
            resource.consumers = [
                item
                for item in resource.consumers
                if not item.endswith(".%s" % field)
            ]
            if consumer not in resource.consumers:
                resource.consumers.append(consumer)

    @staticmethod
    def _normalize_nginx_postconditions(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> None:
        """Materialize the HTTP proof implied by a frozen Nginx listen port."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        nginx_changes = [
            change
            for change in changes
            if isinstance(change, dict)
            and re.search(
                r"nginx",
                "%s %s" % (change.get("title") or "", change.get("objective") or ""),
                re.I,
            )
        ]
        if not nginx_changes:
            return
        listen_resource = next(
            (
                resource
                for resource in resources
                if resource.status == "frozen"
                and resource.kind == "port"
                and V4ChangePlannerAgent._requires_host_port_availability(resource)
                and "nginx" in (
                    "%s %s" % (resource.name, resource.role)
                ).lower()
            ),
            None,
        )
        if listen_resource is None:
            return
        port = int(listen_resource.value)
        activation = next(
            (
                change
                for change in reversed(nginx_changes)
                if re.search(
                    r"\b(?:activate|reload|restart)\b|激活|重载|重新加载",
                    "%s %s" % (
                        change.get("title") or "", change.get("objective") or ""
                    ),
                    re.I,
                )
            ),
            nginx_changes[-1],
        )
        for change in nginx_changes:
            postconditions = change.get("postconditions")
            if not isinstance(postconditions, list):
                postconditions = []
                change["postconditions"] = postconditions
            if change is activation:
                continue
            change["postconditions"] = [
                check
                for check in postconditions
                if not (
                    isinstance(check, dict)
                    and (
                        check.get("checker") == "http_status"
                        or (
                            check.get("checker") == "port_listening"
                            and int((check.get("args") or {}).get("port") or 0) == port
                        )
                    )
                )
            ]
        postconditions = activation.get("postconditions")
        if not isinstance(postconditions, list):
            postconditions = []
            activation["postconditions"] = postconditions
        matching_http = next(
            (
                check for check in postconditions
                if isinstance(check, dict) and check.get("checker") == "http_status"
            ),
            None,
        )
        if matching_http is None:
            postconditions.append(
                {
                    "checker": "http_status",
                    "args": {
                        "url": "http://127.0.0.1:%s/healthz" % port,
                        "expected_status": 200,
                    },
                }
            )
        else:
            args = matching_http.get("args")
            if not isinstance(args, dict):
                args = {}
                matching_http["args"] = args
            args["url"] = "http://127.0.0.1:%s/healthz" % port

    @staticmethod
    def _normalize_occupied_host_ports(
        data: dict[str, Any],
        resources: list[PlanResource],
        bundle: EvidenceBundle,
    ) -> None:
        """Allocate checked-free candidates when the model selects checked-busy ports."""

        explicit_records = [
            record
            for record in bundle.records
            if record.status == "available"
            and record.request.probe == "ports"
            and isinstance(record.request.args.get("ports"), list)
            and record.request.args.get("ports")
        ]
        if not explicit_records:
            return
        candidates: list[int] = []
        occupied: set[int] = set()
        for record in explicit_records:
            for raw_port in record.request.args.get("ports", []):
                try:
                    port = int(raw_port)
                except (TypeError, ValueError):
                    continue
                if port not in candidates:
                    candidates.append(port)
                if re.search(r":%s\b" % port, record.output):
                    occupied.add(port)
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "running_platforms":
                continue
            for configured in re.findall(r"\bconfigured_ports=([^\s]+)", record.output):
                occupied.update(
                    int(raw)
                    for raw in re.findall(r"(?:^|,)[A-Za-z_]+:(\d{1,5})", configured)
                    if 1 <= int(raw) <= 65535
                )
        host_resources = [
            resource
            for resource in resources
            if V4ChangePlannerAgent._requires_host_port_availability(resource)
        ]
        busy_selected = []
        busy_resources = []
        for resource in host_resources:
            port = int(resource.value)
            if port in occupied and port not in busy_selected:
                busy_selected.append(port)
            if port in occupied:
                busy_resources.append(resource)
        if not busy_selected:
            return
        reserved = {
            int(resource.value)
            for resource in resources
            if resource.status == "frozen"
            and resource.kind == "port"
            and resource not in busy_resources
        }
        free = [
            port
            for port in candidates
            if port not in occupied and port not in reserved
        ]
        if len(free) < len(busy_selected):
            return
        replacements = dict(zip(busy_selected, free))
        replacements_by_step: dict[str, dict[int, int]] = {}
        for resource in host_resources:
            old = int(resource.value)
            if old in replacements:
                resource.value = replacements[old]
                resource.source = "compiler_selected_from_checked_free_candidates"
                for consumer in resource.consumers:
                    owner = str(consumer).partition(".")[0]
                    replacements_by_step.setdefault(owner, {})[old] = replacements[old]

        def rewrite(value: Any, scoped: dict[int, int]) -> Any:
            if isinstance(value, bool):
                return value
            if isinstance(value, int):
                return scoped.get(value, value)
            if isinstance(value, str):
                result = value
                for old, new in scoped.items():
                    result = re.sub(
                        r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % old,
                        str(new),
                        result,
                    )
                return result
            if isinstance(value, list):
                return [rewrite(item, scoped) for item in value]
            if isinstance(value, dict):
                return {key: rewrite(item, scoped) for key, item in value.items()}
            return value

        changes = data.get("changes")
        if isinstance(changes, list):
            data["changes"] = [
                rewrite(
                    change,
                    replacements_by_step.get(str(change.get("step_id") or ""), {}),
                )
                if isinstance(change, dict)
                else change
                for change in changes
            ]

    @staticmethod
    def _mark_existing_config_ports(
        data: dict[str, Any],
        resources: list[PlanResource],
        bundle: EvidenceBundle,
    ) -> None:
        """Mark role-matched active config ports as existing, not allocations."""

        changes = {
            str(item.get("step_id") or ""): json.dumps(item, ensure_ascii=False)
            for item in data.get("changes", [])
            if isinstance(item, dict)
        }
        configured: list[tuple[str, str, int]] = []
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "running_platforms":
                continue
            for line in record.output.splitlines():
                root_match = re.search(r"\bproject_root=(/[^\s]+)", line)
                ports_match = re.search(r"\bconfigured_ports=([^\s]+)", line)
                if root_match is None or ports_match is None:
                    continue
                for role, raw in re.findall(
                    r"(?:^|,)([A-Za-z_]+):(\d{1,5})",
                    ports_match.group(1),
                ):
                    configured.append((root_match.group(1), role, int(raw)))
        for resource in resources:
            if resource.status != "frozen" or resource.kind != "port":
                continue
            owners = {str(item).partition(".")[0] for item in resource.consumers}
            owner_text = " ".join(changes.get(owner, "") for owner in owners)
            if any(
                resource.role == role
                and int(resource.value) == port
                and root in owner_text
                for root, role, port in configured
            ):
                resource.source = "existing_config"

    @staticmethod
    def _compile_existing_runtime_role_ports(
        data: dict[str, Any],
        resources: list[PlanResource],
        bundle: EvidenceBundle,
    ) -> None:
        """Keep an existing instance's evidenced role ports unless migration is explicit."""

        changes = {
            str(item.get("step_id") or ""): item
            for item in data.get("changes", [])
            if isinstance(item, dict)
        }
        observed: dict[tuple[str, str], int] = {}
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "running_platforms":
                continue
            for line in record.output.splitlines():
                root_match = re.search(r"\bproject_root=(/[^\s]+)", line)
                if root_match is None:
                    continue
                for role in ("master", "worker"):
                    port_match = re.search(r"\b%s_port=(\d{1,5})\b" % role, line)
                    if port_match is not None:
                        observed[(root_match.group(1), "%s_port" % role)] = int(
                            port_match.group(1)
                        )

        replacements: dict[str, dict[int, int]] = {}
        for resource in resources:
            if (
                resource.status != "frozen"
                or resource.kind != "port"
                or resource.role not in {"master_port", "worker_port"}
            ):
                continue
            owners = {str(item).partition(".")[0] for item in resource.consumers}
            for owner in owners:
                change = changes.get(owner)
                if change is None:
                    continue
                text = json.dumps(change, ensure_ascii=False)
                root = next(
                    (
                        candidate_root
                        for candidate_root, candidate_role in observed
                        if candidate_role == resource.role and candidate_root in text
                    ),
                    "",
                )
                if not root:
                    continue
                if re.search(
                    r"\b(?:migrate|move|change|set|allocate)\w*\b[^.!?。！？]{0,40}\bport\b|"
                    r"(?:迁移|切换|修改|设置|分配)[^。！？]{0,30}端口",
                    text,
                    re.I,
                ):
                    continue
                expected = observed[(root, resource.role)]
                old = int(resource.value)
                if old != expected:
                    replacements.setdefault(owner, {})[old] = expected
                    resource.value = expected
                resource.source = "existing_runtime"

        def rewrite(value: Any, mapping: dict[int, int]) -> Any:
            if isinstance(value, int):
                return mapping.get(value, value)
            if isinstance(value, str):
                result = value
                for old, new in mapping.items():
                    result = re.sub(r"(?<!\d)%s(?!\d)" % old, str(new), result)
                return result
            if isinstance(value, list):
                return [rewrite(item, mapping) for item in value]
            if isinstance(value, dict):
                return {key: rewrite(item, mapping) for key, item in value.items()}
            return value

        if replacements:
            data["changes"] = [
                rewrite(item, replacements.get(str(item.get("step_id") or ""), {}))
                if isinstance(item, dict)
                else item
                for item in data.get("changes", [])
            ]

    @staticmethod
    def _normalize_backend_role_health_contracts(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> None:
        """Verify every started/migrated backend role on its planned port."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        for change in changes:
            if not isinstance(change, dict):
                continue
            step_id = str(change.get("step_id") or "")
            fragments = [
                "%s %s" % (change.get("title") or "", change.get("objective") or ""),
                *[str(item) for item in change.get("expected_changes") or []],
            ]
            postconditions = change.setdefault("postconditions", [])
            if not isinstance(postconditions, list):
                postconditions = []
                change["postconditions"] = postconditions
            for role in ("master", "worker"):
                role_port = next(
                    (
                        int(resource.value)
                        for resource in resources
                        if resource.status == "frozen"
                        and resource.kind == "port"
                        and resource.role == "%s_port" % role
                        and any(
                            str(consumer).rsplit(".", 1)[0] == step_id
                            for consumer in resource.consumers
                        )
                        and str(resource.value).isdigit()
                    ),
                    None,
                )
                existing_role_check = bool(
                    role_port is not None
                    and any(
                        isinstance(item, dict)
                        and item.get("checker") in {"http_status", "backend_health"}
                        and isinstance(item.get("args"), dict)
                        and re.search(
                            r":%s(?:/|$)" % role_port,
                            str(item["args"].get("url") or ""),
                        )
                        for item in postconditions
                    )
                )
                authorized = any(
                    re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % role, fragment, re.I)
                    and re.search(
                        r"\b(?:start|restart|restore|recover|migrate|move|healthy|health)\w*\b|"
                        r"启动|重启|恢复|迁移|健康",
                        fragment,
                        re.I,
                    )
                    and not re.search(r"\b(?:preserve|keep|remain|unchanged)\b|保持|保留|不变|不修改", fragment, re.I)
                    for fragment in fragments
                )
                if not authorized and not existing_role_check:
                    continue
                port = role_port
                if port is None:
                    continue
                postconditions[:] = [
                    item for item in postconditions
                    if not (
                        isinstance(item, dict)
                        and item.get("checker") in {"http_status", "backend_health"}
                        and isinstance(item.get("args"), dict)
                        and re.search(
                            r":%s(?:/|$)" % port,
                            str(item["args"].get("url") or ""),
                        )
                    )
                ]
                postconditions.append({
                    "checker": "backend_health",
                    "args": {
                        "url": "http://127.0.0.1:%s/server_health/" % port,
                        "expected_code": 1,
                    },
                })

    @staticmethod
    def _mark_existing_runtime_ports(
        data: dict[str, Any],
        resources: list[PlanResource],
        bundle: EvidenceBundle,
    ) -> None:
        """Distinguish ports owned by a repaired root from new-port candidates."""

        changes = {
            str(item.get("step_id") or ""): json.dumps(item, ensure_ascii=False)
            for item in data.get("changes", [])
            if isinstance(item, dict)
        }
        runtime_ports: list[tuple[str, str, int]] = []
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "running_platforms":
                continue
            for line in record.output.splitlines():
                root_match = re.search(r"\bproject_root=(/[^\s]+)", line)
                if root_match is None:
                    continue
                for role, raw_port in re.findall(
                    r"\b(master_port|worker_port)=(\d{1,5})\b",
                    line,
                ):
                    runtime_ports.append((root_match.group(1), role, int(raw_port)))
        for resource in resources:
            if resource.status != "frozen" or resource.kind != "port":
                continue
            try:
                value = int(resource.value)
            except (TypeError, ValueError):
                continue
            consumer_steps = {
                consumer.rsplit(".", 1)[0] for consumer in resource.consumers
            }
            owner_text = " ".join(changes.get(step_id, "") for step_id in consumer_steps)
            matches = [
                role
                for root, role, port in runtime_ports
                if value == port and root in owner_text
            ]
            if matches:
                resource.source = "existing_runtime"
                if str(resource.role or "") not in {"master_port", "worker_port"}:
                    unique_roles = sorted(set(matches))
                    if len(unique_roles) == 1:
                        resource.role = unique_roles[0]

    @staticmethod
    def _normalize_runtime_repair_coverage(
        data: dict[str, Any],
        bundle: EvidenceBundle,
    ) -> None:
        """Compile unhealthy runtime roles into mandatory repair acceptance."""

        unhealthy: dict[
            str,
            dict[str, tuple[int, str, list[int], dict[int, tuple[int, str]]]],
        ] = {}
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "running_platforms":
                continue
            for line in record.output.splitlines():
                root_match = re.search(r"\bproject_root=(/[^\s]+)", line)
                if root_match is None or "backend_status=abnormal" not in line:
                    continue
                roles: dict[
                    str,
                    tuple[int, str, list[int], dict[int, tuple[int, str]]],
                ] = {}
                for role in ("master", "worker"):
                    port_match = re.search(r"\b%s_port=(\d{1,5})\b" % role, line)
                    endpoint_match = re.search(
                        r"\b%s_endpoint=([^\s]+)" % role,
                        line,
                    )
                    if (
                        port_match is not None
                        and endpoint_match is not None
                        and endpoint_match.group(1) != "healthy"
                    ):
                        endpoint = endpoint_match.group(1)
                        disposition = (
                            "start missing"
                            if endpoint == "not_checked"
                            and re.search(r"\b%s_endpoint=not_checked\s+reason=role_not_running" % role, line)
                            else "restart unhealthy"
                        )
                        pids_match = re.search(
                            r"\b%s_pids=([0-9,]+)" % role,
                            line,
                        )
                        pids = (
                            sorted({int(item) for item in pids_match.group(1).split(",")})
                            if pids_match is not None
                            else []
                        )
                        identities_match = re.search(
                            r"\b%s_identities=([^\s]+)" % role,
                            line,
                        )
                        identities: dict[int, tuple[int, str]] = {}
                        if identities_match is not None:
                            for raw_identity in identities_match.group(1).split(","):
                                match = re.fullmatch(
                                    r"(\d+):(\d+):(/[^\s,]+)", raw_identity,
                                )
                                if match is not None:
                                    identities[int(match.group(1))] = (
                                        int(match.group(2)), match.group(3),
                                    )
                        roles[role] = (
                            int(port_match.group(1)), disposition, pids, identities,
                        )
                if roles:
                    unhealthy[root_match.group(1)] = roles
        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        for root, roles in unhealthy.items():
            candidates = [
                item for item in changes
                if isinstance(item, dict)
                and root in json.dumps(item, ensure_ascii=False)
            ]
            change = next(
                (
                    item for item in candidates
                    if re.search(
                        r"\b(?:start|restart|restore|recover|launch)\w*\b|"
                        r"启动|重启|恢复",
                        "%s %s %s" % (
                            item.get("title") or "",
                            item.get("objective") or "",
                            " ".join(item.get("expected_changes") or []),
                        ),
                        re.I,
                    )
                    and not re.match(
                        r"^\s*(?:(?:precisely|safely|only|精确|安全|仅)\s*)?"
                        r"(?:stop|terminate|停止|终止)",
                        str(item.get("title") or ""),
                        re.I,
                    )
                ),
                candidates[0] if candidates else None,
            )
            if change is None:
                continue
            expected = change.setdefault("expected_changes", [])
            postconditions = change.setdefault("postconditions", [])
            additions = []
            step_id = str(change.get("step_id") or "")
            raw_resources = data.get("resources")
            if not isinstance(raw_resources, list):
                raw_resources = []
                data["resources"] = raw_resources
            for role, (observed_port, disposition, pids, identities) in roles.items():
                planned_port = next(
                    (
                        int(item.get("value"))
                        for item in raw_resources or []
                        if isinstance(item, dict)
                        and str(item.get("role") or "") == "%s_port" % role
                        and any(
                            str(consumer).rsplit(".", 1)[0] == step_id
                            for consumer in item.get("consumers") or []
                        )
                        and str(item.get("value") or "").isdigit()
                    ),
                    observed_port,
                )
                marker = "%s %s role at %s and backend health succeeds" % (
                    disposition, role, planned_port,
                )
                if disposition == "restart unhealthy":
                    expected[:] = [
                        item
                        for item in expected
                        if not (
                            re.search(
                                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % role,
                                str(item),
                                re.I,
                            )
                            and re.search(
                                r"\b(?:preserve|keeps?|remains?|unchanged|do not restart|not restart\w*)\b|"
                                r"保持|保留|不变|不重启|无需重启",
                                str(item),
                                re.I,
                            )
                        )
                    ]
                if not any(
                    disposition in str(item).lower()
                    and role in str(item).lower()
                    for item in expected
                ):
                    expected.append(marker)
                additions.append("recover %s" % role)
                if disposition == "restart unhealthy" and pids:
                    resource_name = "%s_pid" % role
                    if len(unhealthy) > 1:
                        resource_name = "%s_%s" % (
                            re.sub(r"[^A-Za-z0-9]+", "_", root).strip("_")[-60:],
                            resource_name,
                        )
                    if not any(
                        isinstance(item, dict)
                        and item.get("name") == resource_name
                        for item in raw_resources or []
                    ):
                        raw_resources.append(
                            {
                                "name": resource_name,
                                "kind": "identifier",
                                "status": "frozen",
                                "role": "%s_pid" % role,
                                "value": pids[0],
                                "source": "running_platforms",
                                "consumers": [
                                    "%s.%s_pid" % (step_id, role)
                                ],
                            }
                        )
                    runtime_identity = identities.get(pids[0])
                    if runtime_identity is not None:
                        run_as_uid, python_executable = runtime_identity
                        identity_resources = (
                            ("%s_uid" % role, "identifier", "%s_uid" % role,
                             run_as_uid, "run_as_uid"),
                            ("%s_python_executable" % role, "path",
                             "%s_python_executable" % role,
                             python_executable, "python_executable"),
                        )
                        for name, kind, resource_role, value, arg_name in identity_resources:
                            if any(
                                isinstance(item, dict)
                                and item.get("role") == resource_role
                                and any(
                                    str(consumer).rsplit(".", 1)[0] == step_id
                                    for consumer in item.get("consumers") or []
                                )
                                for item in raw_resources
                            ):
                                continue
                            raw_resources.append({
                                "name": name,
                                "kind": kind,
                                "status": "frozen",
                                "role": resource_role,
                                "value": value,
                                "source": "running_platforms",
                                "consumers": ["%s.%s" % (step_id, arg_name)],
                            })
                # Backend acceptance is one exact contract.  Remove generic or
                # legacy HTTP checks for this role's port (/, /check_health,
                # external host aliases) and require /server_health/ JSON code=1.
                postconditions[:] = [
                    item
                    for item in postconditions
                    if not (
                        isinstance(item, dict)
                        and item.get("checker") in {"http_status", "backend_health"}
                        and isinstance(item.get("args"), dict)
                        and re.search(
                            r":%s(?:/|$)" % planned_port,
                            str(item["args"].get("url") or ""),
                        )
                    )
                ]
                postconditions.append({
                    "checker": "backend_health",
                    "args": {
                        "url": "http://127.0.0.1:%s/server_health/" % planned_port,
                        "expected_code": 1,
                    },
                })
            objective = str(change.get("objective") or "")
            coverage = "; ".join(additions)
            if coverage and coverage not in objective:
                change["objective"] = "%s; %s for %s" % (objective, coverage, root)

    @staticmethod
    def _collapse_redundant_runtime_repair_changes(data: dict[str, Any]) -> None:
        """Merge same-root recovery semantics after role coverage is compiled."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        runtime_roots = sorted(
            {
                str(resource.get("value") or "").rstrip("/")
                for resource in data.get("resources") or []
                if isinstance(resource, dict)
                and resource.get("status") == "frozen"
                and resource.get("kind") == "path"
                and str(resource.get("name") or resource.get("role") or "")
                in {
                    "project_root", "runtime_cwd", "instance_root",
                    "platform_runtime_root", "platform_instance_root",
                }
                and str(resource.get("value") or "").startswith("/")
            },
            key=len,
            reverse=True,
        )
        owner_by_root: dict[str, dict[str, Any]] = {}
        replacements: dict[str, str] = {}
        kept = []
        for change in changes:
            if not isinstance(change, dict):
                kept.append(change)
                continue
            text = json.dumps(change, ensure_ascii=False)
            root = next((item for item in runtime_roots if item in text), "")
            if not root and not runtime_roots:
                paths = re.findall(
                    r"/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+",
                    text,
                )
                root = min(paths, key=len).rstrip("/") if paths else ""
            recovery = bool(re.search(
                r"\b(?:start|restart|restore|recover)\w*\b|启动|重启|恢复",
                "%s %s %s" % (
                    change.get("title") or "",
                    change.get("objective") or "",
                    " ".join(change.get("expected_changes") or []),
                ),
                re.I,
            ))
            if not root or not recovery:
                kept.append(change)
                continue
            owner = owner_by_root.get(root)
            if owner is None:
                owner_by_root[root] = change
                kept.append(change)
                continue
            duplicate_id = str(change.get("step_id") or "")
            owner_id = str(owner.get("step_id") or "")
            if duplicate_id and owner_id:
                replacements[duplicate_id] = owner_id
            for field in ("expected_changes", "postconditions"):
                target = owner.setdefault(field, [])
                for value in change.get(field) or []:
                    if value not in target:
                        target.append(value)
        # A source/runtime recovery owner deterministically decomposes to
        # edit -> exact role stop -> start.  A sibling semantic step that only
        # repeats that same role stop must be folded into the owner, otherwise
        # the confirmed plan can target the predecessor PID twice.
        for change in list(kept):
            if not isinstance(change, dict):
                continue
            text = json.dumps(change, ensure_ascii=False)
            root = next((item for item in runtime_roots if item in text), "")
            owner = owner_by_root.get(root)
            if not root or owner is None or owner is change:
                continue
            primary = "%s %s" % (
                change.get("title") or "", change.get("objective") or "",
            )
            stop_role = next(
                (
                    role for role in ("master", "worker")
                    if re.search(
                        r"\b(?:stop|terminate)\w*\b|停止|终止", primary, re.I,
                    )
                    and re.search(
                        r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % role,
                        primary,
                        re.I,
                    )
                ),
                "",
            )
            owner_text = json.dumps(owner, ensure_ascii=False)
            if not stop_role or not re.search(
                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % stop_role,
                owner_text,
                re.I,
            ):
                continue
            duplicate_id = str(change.get("step_id") or "")
            owner_id = str(owner.get("step_id") or "")
            if duplicate_id and owner_id:
                replacements[duplicate_id] = owner_id
            for field in ("expected_changes", "postconditions"):
                target = owner.setdefault(field, [])
                for value in change.get(field) or []:
                    if value not in target:
                        target.append(value)
            kept.remove(change)
        if not replacements:
            return
        for change in kept:
            if not isinstance(change, dict):
                continue
            dependencies = []
            for raw in change.get("depends_on") or []:
                dependency = replacements.get(str(raw), str(raw))
                if dependency != change.get("step_id") and dependency not in dependencies:
                    dependencies.append(dependency)
            change["depends_on"] = dependencies
        for resource in data.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            consumers = []
            for consumer in resource.get("consumers") or []:
                owner, separator, argument = str(consumer).partition(".")
                owner = replacements.get(owner, owner)
                normalized = "%s.%s" % (owner, argument) if separator else str(consumer)
                if normalized not in consumers:
                    consumers.append(normalized)
            resource["consumers"] = consumers
        data["changes"] = kept

    @staticmethod
    def _normalize_healthy_runtime_role_changes(
        data: dict[str, Any],
        bundle: EvidenceBundle,
    ) -> None:
        """Keep authoritative healthy roles read-only during a scoped repair."""

        states: dict[str, dict[str, tuple[str, int]]] = {}
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "running_platforms":
                continue
            for line in record.output.splitlines():
                root_match = re.search(r"\bproject_root=(/[^\s]+)", line)
                if root_match is None:
                    continue
                root = root_match.group(1)
                for role in ("master", "worker"):
                    endpoint = re.search(r"\b%s_endpoint=([^\s]+)" % role, line)
                    port = re.search(r"\b%s_port=(\d{1,5})" % role, line)
                    if endpoint is not None and port is not None:
                        states.setdefault(root, {})[role] = (
                            endpoint.group(1), int(port.group(1)),
                        )
        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        mutation_pattern = re.compile(
            r"\b(?:start|restart|restore|recover|launch)\w*\b|启动|重启|恢复",
            re.I,
        )
        for change in changes:
            if not isinstance(change, dict):
                continue
            serialized = json.dumps(change, ensure_ascii=False)
            root = next(
                (candidate for candidate in states if candidate in serialized),
                "",
            )
            if not root:
                continue
            mutating_roles = {
                role
                for role in ("master", "worker")
                if re.search(
                    r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % role,
                    serialized,
                    re.I,
                )
                and mutation_pattern.search(serialized)
            }
            healthy_mutations = {
                role for role in mutating_roles
                if states[root].get(role, ("", 0))[0] == "healthy"
            }
            unhealthy_mutations = mutating_roles.difference(healthy_mutations)
            if len(healthy_mutations) != 1 or unhealthy_mutations:
                if healthy_mutations:
                    expected = change.setdefault("expected_changes", [])
                    for role in sorted(healthy_mutations):
                        marker = "preserve healthy %s role without restart" % role
                        if marker not in expected:
                            expected.append(marker)
                continue
            role = next(iter(healthy_mutations))
            port = states[root][role][1]
            change["title"] = "Verify healthy %s backend" % role
            change["objective"] = (
                "Verify %s for %s remains healthy without restarting it"
                % (role, root)
            )
            change["reason"] = (
                "running_platforms already proves %s /server_health/ healthy"
                % role
            )
            change["risk"] = "readonly"
            change["expected_changes"] = []
            change["postconditions"] = [{
                "checker": "backend_health",
                "args": {
                    "url": "http://127.0.0.1:%s/server_health/" % port,
                    "expected_code": 1,
                },
            }]

    @staticmethod
    def _normalize_port_resource_roles(data: dict[str, Any]) -> None:
        """Canonicalize instance-prefixed role names before health compilation."""

        resources = data.get("resources")
        if not isinstance(resources, list):
            return
        canonical = (
            "master_port", "worker_port", "public_port", "web_terminal_port",
            "redis_port", "mysql_port", "rabbitmq_port",
        )
        for resource in resources:
            if not isinstance(resource, dict) or resource.get("kind") != "port":
                continue
            identity = "%s %s" % (
                resource.get("name") or "", resource.get("role") or ""
            )
            matches = [
                role for role in canonical
                if re.search(r"(?:^|_)%s(?:$|\s)" % re.escape(role), identity)
            ]
            for component in ("master", "worker", "public", "web_terminal"):
                if re.search(
                    r"(?:^|_)%s_(?:(?:new|target|destination)_port|"
                    r"port_(?:new|target|destination))(?:$|\s)" % component,
                    identity,
                    re.I,
                ):
                    matches.append("%s_port" % component)
            if len(matches) == 1:
                resource["role"] = matches[0]

    @staticmethod
    def _normalize_existing_config_paths(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> None:
        """Bind config resources to the existing active Klonet config file."""

        changes = {
            str(item.get("step_id") or ""): json.dumps(item, ensure_ascii=False)
            for item in data.get("changes", [])
            if isinstance(item, dict)
        }
        roots = [
            resource for resource in resources
            if resource.status == "frozen"
            and resource.kind == "path"
            and resource.role == "instance_root"
        ]
        for root_resource in roots:
            root = Path(str(root_resource.value))
            candidates = (
                root / "vemu_config" / "config.py",
                root / "vemu_uestc" / "vemu_config" / "config.py",
            )
            config = next((path for path in candidates if path.is_file()), None)
            if config is None:
                continue
            owners = {
                step_id for step_id, serialized in changes.items()
                if str(root) in serialized
            }
            port_mutation_owners = {
                step_id for step_id in owners
                if re.search(r"\b(?:master_port|worker_port)\b", changes[step_id], re.I)
                and re.search(r"\b(?:set|change|update|edit|migrate|move)\w*\b|设置|修改|更新|迁移|改为", changes[step_id], re.I)
            }
            for resource in resources:
                if resource.status != "frozen" or resource.kind != "path":
                    continue
                if Path(str(resource.value)).name in {"gun.py", "worker_gun.py"}:
                    rewritten = []
                    for consumer in resource.consumers:
                        owner, separator, target = str(consumer).partition(".")
                        if owner in port_mutation_owners and target == "path":
                            rewritten.append("%s.worker_gun" % owner)
                        else:
                            rewritten.append(str(consumer))
                    resource.consumers = rewritten
                    continue
                if resource.role != "config_path" and "config" not in resource.name:
                    continue
                resource_owners = {
                    str(consumer).partition(".")[0]
                    for consumer in resource.consumers
                }
                if resource_owners.intersection(owners):
                    resource.value = str(config.resolve())
                    resource.role = "config_path"
                    resource.source = "derived_from_existing_instance_root"
            for owner in port_mutation_owners:
                if any(
                    resource.status == "frozen"
                    and resource.kind == "path"
                    and str(resource.value) == str(config.resolve())
                    and "%s.path" % owner in resource.consumers
                    for resource in resources
                ):
                    continue
                resources.append(PlanResource(
                    "%s_active_config" % owner.replace("-", "_"),
                    "path",
                    "frozen",
                    "config_path",
                    str(config.resolve()),
                    "derived_from_existing_instance_root",
                    consumers=["%s.path" % owner],
                ))

    @staticmethod
    def _normalize_runtime_stop_scope(
        data: dict[str, Any],
        goal: str = "",
        bundle: EvidenceBundle | None = None,
    ) -> None:
        """Narrow a port-release stop to the named root, role, and port."""

        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        authoritative: list[tuple[str, int]] = []
        if bundle is not None and re.search(r"\bworker\b|工作进程", goal, re.I):
            for record in bundle.records:
                if record.status != "available" or record.request.probe != "running_platforms":
                    continue
                for line in record.output.splitlines():
                    root_match = re.search(r"\bproject_root=(/[^\s]+)", line)
                    port_match = re.search(r"\bworker_port=(\d{1,5})\b", line)
                    if root_match is None or port_match is None:
                        continue
                    root = root_match.group(1).rstrip("/")
                    port = int(port_match.group(1))
                    if root in goal and re.search(r"(?<!\d)%s(?!\d)" % port, goal):
                        authoritative.append((root, port))
        authoritative = sorted(set(authoritative), key=lambda item: len(item[0]), reverse=True)
        for change in changes:
            if not isinstance(change, dict):
                continue
            primary = "%s %s" % (
                change.get("title") or "", change.get("objective") or ""
            )
            if not (
                re.search(
                    r"\b(?:stop|terminate)\w*\b|停止|终止",
                    primary,
                    re.I,
                )
                and re.search(r"\b(?:master|worker)\b|主进程|工作进程", primary, re.I)
                and re.search(r"\b(?:port|listener)\b|端口|监听", primary, re.I)
            ):
                continue
            roots = re.findall(r"/[A-Za-z0-9._/-]*vemu_uestc", primary)
            root = max(roots, key=len).rstrip("/") if roots else ""
            port_match = re.search(r"\b([1-9]\d{3,4})\b", primary)
            if authoritative:
                root, port = authoritative[0]
            elif root and port_match is not None:
                port = int(port_match.group(1))
            else:
                continue
            combined_migration = bool(
                (
                    re.search(r"\bworker_port\b|config|配置|改端口|\b(?:set|change|move)\w*\b.{0,30}\bport\b", primary, re.I)
                    or len(set(re.findall(r"\b[1-9]\d{3,4}\b", primary))) >= 2
                )
                and re.search(r"\b(?:start|restart|migrate|move)\w*\b|启动|重启|迁移", primary, re.I)
            )
            if not combined_migration:
                change["title"] = "Stop root-bound worker on port %s" % port
                change["objective"] = (
                    "Stop only worker processes whose cwd belongs to %s and whose "
                    "listener owns port %s; preserve master, data_server, celery, "
                    "web_terminal, and every other project root."
                ) % (root, port)
                change["expected_changes"] = [
                    "worker processes for %s on port %s stop" % (root, port),
                    "all non-worker roles and other project roots remain unchanged",
                ]
                change["postconditions"] = [
                    {"checker": "port_not_listening", "args": {"port": port}}
                ]
            step_id = str(change.get("step_id") or "")
            owner_pid: int | None = None
            if bundle is not None:
                for record in bundle.records:
                    if record.status != "available" or record.request.probe != "port_owner":
                        continue
                    for line in record.output.splitlines():
                        if not (
                            re.search(r"\bport=%s\b" % port, line)
                            and "worker_main:flask_app" in line
                            and re.search(r"\bcwd=%s(?:\s|$)" % re.escape(root), line)
                        ):
                            continue
                        pid_match = re.search(r"\btree_root_pid=(\d+)\b", line)
                        if pid_match is None:
                            pid_match = re.search(r"\bpid=(\d+)\b", line)
                        if pid_match is not None:
                            owner_pid = int(pid_match.group(1))
                            break
                    if owner_pid is not None:
                        break
                if owner_pid is None:
                    for record in bundle.records:
                        if record.status != "available" or record.request.probe != "running_platforms":
                            continue
                        for line in record.output.splitlines():
                            if not (
                                re.search(r"\bproject_root=%s(?:\s|$)" % re.escape(root), line)
                                and re.search(r"\bworker_port=%s\b" % port, line)
                            ):
                                continue
                            group_match = re.search(r"\bworker_pgids=([0-9,]+)", line)
                            if group_match is None:
                                continue
                            groups = [
                                int(value) for value in group_match.group(1).split(",")
                                if value.isdigit()
                            ]
                            if groups:
                                owner_pid = min(groups)
                                break
                        if owner_pid is not None:
                            break
            for resource in data.get("resources") or []:
                if not isinstance(resource, dict) or resource.get("kind") != "port":
                    continue
                if not any(
                    str(consumer).rsplit(".", 1)[0] == step_id
                    for consumer in resource.get("consumers") or []
                ):
                    continue
                identity = "%s %s" % (
                    resource.get("name") or "", resource.get("role") or "",
                )
                consumer_targets = " ".join(
                    str(consumer).rsplit(".", 1)[-1]
                    for consumer in resource.get("consumers") or []
                )
                if (
                    not re.search(r"new|target|destination", identity, re.I)
                    and (
                        re.search(r"old|source|current", identity, re.I)
                        or re.search(r"old|source|current", consumer_targets, re.I)
                        or str(resource.get("value") or "") == str(port)
                    )
                ):
                    resource["value"] = port
                    resource["role"] = "worker_port"
                    resource["source"] = "existing_runtime"
            raw_resources = data.setdefault("resources", [])
            if owner_pid is not None and not any(
                isinstance(resource, dict)
                and resource.get("kind") == "identifier"
                and str(resource.get("value") or "") == str(owner_pid)
                and any(
                    str(consumer).rsplit(".", 1)[0] == step_id
                    for consumer in resource.get("consumers") or []
                )
                for resource in raw_resources
            ):
                raw_resources.append({
                    "name": "%s_worker_pid" % step_id.replace("-", "_"),
                    "kind": "identifier",
                    "status": "frozen",
                    "role": "worker_pid",
                    "value": owner_pid,
                    "source": "port_owner_tree_root",
                    "consumers": ["%s.pid" % step_id],
                })
            if not any(
                isinstance(resource, dict)
                and resource.get("kind") == "path"
                and str(resource.get("value") or "").rstrip("/") == root
                and any(
                    str(consumer).rsplit(".", 1)[0] == step_id
                    for consumer in resource.get("consumers") or []
                )
                for resource in raw_resources
            ):
                raw_resources.append({
                    "name": "%s_runtime_cwd" % step_id.replace("-", "_"),
                    "kind": "path",
                    "status": "frozen",
                    "role": "runtime_cwd",
                    "value": root,
                    "source": "port_owner_cwd",
                    "consumers": ["%s.runtime_cwd" % step_id],
                })

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
                probe_requests=V4ChangePlannerAgent._candidate_evidence_requests(
                    candidate, bundle, unproven
                ),
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
        if (
            V4ChangePlannerAgent._plan_needs_docker_images(candidate)
            and not V4ChangePlannerAgent._has_docker_images(bundle)
        ):
            return V4PlanningOutcome(
                status="need_evidence",
                candidate_plan=candidate,
                probe_requests=[
                    ProbeRequest(
                        "docker_images",
                        {},
                        "select an already installed image for each new container",
                    )
                ],
            )
        return V4PlanningOutcome(status="ready", plan=candidate)

    @staticmethod
    def _plan_needs_docker_images(plan: ChangePlanV4) -> bool:
        return any(
            re.search(
                r"\b(?:docker\s+)?containers?\b|容器",
                "%s %s" % (step.title, step.objective),
                re.I,
            )
            for step in plan.steps
        )

    @staticmethod
    def _candidate_evidence_requests(
        candidate: ChangePlanV4,
        bundle: EvidenceBundle,
        unproven_ports: list[PlanResource],
    ) -> list[ProbeRequest]:
        requests = []
        if unproven_ports:
            requests.append(
                ProbeRequest(
                    "ports",
                    {"ports": sorted({int(item.value) for item in unproven_ports})},
                    "verify frozen port availability",
                )
            )
        if (
            V4ChangePlannerAgent._plan_needs_docker_images(candidate)
            and not V4ChangePlannerAgent._has_docker_images(bundle)
        ):
            requests.append(
                ProbeRequest(
                    "docker_images",
                    {},
                    "select an already installed image for each new container",
                )
            )
        return requests

    @staticmethod
    def _has_docker_images(bundle: EvidenceBundle) -> bool:
        return any(
            record.request.probe == "docker_images"
            and record.status == "available"
            and "inspect_docker_images" in record.output
            for record in bundle.records
        )

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

            stateful_candidates = []
            for item in indexed_changes:
                primary = primary_change_text(item)
                title_text = str(item.get("title") or "")
                names_service_in_title = re.search(
                    r"mysql|redis|rabbitmq|数据库|消息队列", title_text, re.I
                )
                provisions_group = (
                    re.search(r"stateful|有状态", primary, re.I)
                    and re.search(r"provision\b|create\b|部署|创建", primary, re.I)
                    and re.search(r"containers?\b|容器", primary, re.I)
                )
                mutates_named_service = names_service_in_title and re.search(
                    r"provision\b|create\b|start\b|run\b|container\b|"
                    r"部署|创建|启动|容器",
                    primary,
                    re.I,
                )
                if mutates_named_service or provisions_group:
                    stateful_candidates.append(item)
            stateful = [
                item
                for item in stateful_candidates
                if re.search(r"containers?\b|容器", change_text(item), re.I)
            ]
            source_preparations = [
                item
                for item in indexed_changes
                if re.search(r"\b(?:clone|checkout)\b|克隆|检出", change_text(item), re.I)
                and re.search(r"\b(?:git|repository|source)\b|仓库|源码", change_text(item), re.I)
            ]
            for service in stateful_candidates:
                if service in stateful:
                    continue
                errors.append(
                    "isolated stateful service must use a new named container=%s"
                    % str(service.get("step_id") or "")
                )
            for service in stateful:
                service_id = str(service.get("step_id") or "")
                service_ancestors = ancestors(service_id)
                for preparation in source_preparations:
                    preparation_id = str(preparation.get("step_id") or "")
                    if preparation_id not in service_ancestors:
                        errors.append(
                            "stateful credential source requires earlier clone="
                            "%s:%s" % (service_id, preparation_id)
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
        invented_database_step = False
        for change in data.get("changes", []):
            if not isinstance(change, dict):
                continue
            primary = "%s %s" % (
                change.get("title") or "",
                change.get("objective") or "",
            )
            mentions_database_change = re.search(
                r"(?:database|schema).{0,24}(?:migration|initialize|initialization|seed)|"
                r"(?:migration|initialize|initialization|seed).{0,24}(?:database|schema)|"
                r"数据库.{0,16}(?:迁移|初始化|种子)|(?:迁移|初始化|种子).{0,16}数据库",
                primary,
                re.I,
            )
            allowed_startup_fact = (
                re.search(r"\b(?:start|launch|startup)\b|启动", primary, re.I)
                and re.search(r"create_all|app_factory", primary, re.I)
            )
            if mentions_database_change and not allowed_startup_fact:
                invented_database_step = True
                break
        if invented_database_step:
            errors.append(
                "complete Klonet runtime invents unsupported database initialization step"
            )
        if re.search(
            r"(?:install|pip).{0,30}(?:python\s+)?(?:dependencies|requirements|packages)|"
            r"(?:dependencies|requirements|packages).{0,30}(?:install|pip)|"
            r"安装.{0,20}(?:依赖|包)|(?:依赖|包).{0,20}安装",
            payload,
            re.I,
        ):
            errors.append(
                "complete Klonet runtime invents ungrounded dependency installation step"
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
            "master_ip", "mysql_ip", "rabbitmq_ip",
            "celery_redis_port_db", "celery_rabbitmq_port_db",
            "proj_config",
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
        text = re.sub(
            r"(?:never|no|not|without|do\s+not|must\s+not|禁止|不要|不得|不应|不复用)"
            r"[^.!?。！？]{0,30}(?:reuse|share|复用|共享)"
            r"[^.!?。！？]{0,40}(?:existing|container|现有|容器)",
            "",
            text,
            flags=re.I,
        )
        return re.sub(
            r"(?:rather\s+than|instead\s+of)"
            r"[^.!?。！？]{0,30}(?:reus\w*|shar\w*|复用|共享)"
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
        declared_listeners = V4ChangePlannerAgent._declared_listening_ports_by_step(data)
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
                observational = port not in declared_listeners.get(step_id, set())
                resource = PlanResource(
                    name="derived_host_port_%s" % port,
                    kind="port",
                    status="frozen",
                    role=(
                        "observed_endpoint_port"
                        if observational else "selected_host_port"
                    ),
                    value=port,
                    source=(
                        "derived_observation"
                        if observational else "derived_from_change_contract"
                    ),
                    consumers=[consumer],
                )
                normalized.append(resource)
                by_port[port] = resource

        has_nginx_name = any(
            item.status == "frozen"
            and item.role in {"nginx_config_name", "nginx_site_name"}
            for item in normalized
        )
        if not has_nginx_name and isinstance(changes, list):
            for change in changes:
                if not isinstance(change, dict):
                    continue
                step_id = str(change.get("step_id") or "")
                text = json.dumps(change, ensure_ascii=False)
                if "nginx" not in text.lower():
                    continue
                match = re.search(
                    r"/etc/nginx/sites-available/([A-Za-z0-9_.-]+)",
                    text,
                )
                if match is None:
                    continue
                normalized.append(
                    PlanResource(
                        name="nginx_config_name",
                        kind="identifier",
                        status="frozen",
                        role="nginx_config_name",
                        value=match.group(1),
                        source="derived_from_nginx_site_path",
                        consumers=["%s.config_name" % step_id],
                    )
                )
                break

        roots = [
            item
            for item in normalized
            if item.status == "frozen"
            and item.kind == "path"
            and str(item.role or "").lower()
            in {"instance_root", "target_root", "deployment_root"}
        ]
        if not roots:
            return V4ChangePlannerAgent._normalize_consumer_owners(normalized)
        changes = data.get("changes")
        if not isinstance(changes, list):
            return V4ChangePlannerAgent._normalize_consumer_owners(normalized)

        def changes_for_root(root: PlanResource) -> list[dict[str, Any]]:
            if len(roots) == 1:
                return [change for change in changes if isinstance(change, dict)]
            root_text = str(root.value).rstrip("/")
            consumer_owners = {
                str(consumer).partition(".")[0]
                for consumer in root.consumers
            }
            return [
                change for change in changes
                if isinstance(change, dict)
                and (
                    str(change.get("step_id") or "") in consumer_owners
                    or root_text in json.dumps(change, ensure_ascii=False)
                )
            ]

        for root_index, root in enumerate(roots, start=1):
            root_text = str(root.value).rstrip("/")
            owned_changes = changes_for_root(root)
            screen_consumers = [
                "%s.project_root" % str(change.get("step_id") or "")
                for change in owned_changes
                if re.search(
                    r"screen",
                    "%s %s" % (
                        change.get("title") or "",
                        change.get("objective") or "",
                    ),
                    re.I,
                )
                and re.search(
                    r"\b(?:start|launch|restart)\b|启动|重启",
                    "%s %s" % (
                        change.get("title") or "",
                        change.get("objective") or "",
                    ),
                    re.I,
                )
            ]
            if not screen_consumers:
                continue
            root_path = Path(root_text)
            mains_candidates = (
                root_path / "mains",
                root_path / "vemu_uestc" / "mains",
            )
            mains_path = str(
                next(
                    (
                        candidate
                        for candidate in mains_candidates
                        if all(
                            (candidate / name).is_file()
                            for name in REQUIRED_ENTRY_FILES
                        )
                    ),
                    mains_candidates[0],
                )
            )
            existing_mains = next(
                (
                    item
                    for item in normalized
                    if item.status == "frozen"
                    and item.kind == "path"
                    and str(item.value).rstrip("/") == mains_path
                ),
                None,
            )
            if existing_mains is None:
                normalized.append(
                    PlanResource(
                        name=(
                            "runtime_mains_root"
                            if len(roots) == 1
                            else "runtime_mains_root_%s" % root_index
                        ),
                        kind="path",
                        status="frozen",
                        role="runtime_mains_root",
                        value=mains_path,
                        source="derived_from_instance_root",
                        consumers=screen_consumers,
                    )
                )
            else:
                for consumer in screen_consumers:
                    if consumer not in existing_mains.consumers:
                        existing_mains.consumers.append(consumer)

        for root_index, root in enumerate(roots, start=1):
            root_text = str(root.value).rstrip("/")
            owned_changes = changes_for_root(root)
            config_path = root_text + "/vemu_config/config.py"
            config_consumers = [
                "%s.path" % str(change.get("step_id") or "")
                for change in owned_changes
                if re.search(
                    r"\bvemu_config/config\.py\b",
                    "%s %s %s" % (
                        change.get("title") or "",
                        change.get("objective") or "",
                        " ".join(change.get("expected_changes") or []),
                    ),
                    re.I,
                )
            ]
            if not config_consumers:
                continue
            existing_config = next(
                (
                    item
                    for item in normalized
                    if item.status == "frozen"
                    and item.kind == "path"
                    and str(item.value) == config_path
                ),
                None,
            )
            if existing_config is None:
                normalized.append(
                    PlanResource(
                        name=(
                            "config_path"
                            if len(roots) == 1
                            else "config_path_%s" % root_index
                        ),
                        kind="path",
                        status="frozen",
                        role="config_path",
                        value=config_path,
                        source="derived_from_instance_root",
                        consumers=config_consumers,
                    )
                )
            else:
                for consumer in config_consumers:
                    if consumer not in existing_config.consumers:
                        existing_config.consumers.append(consumer)
        for root in roots:
            root_text = str(root.value).rstrip("/")
            for change in changes_for_root(root):
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
            for match in re.finditer(
                r"(?:\bport\b|_port|\u7aef\u53e3).{0,20}?([1-9]\d{1,4})(?![\d.])",
                text,
                re.I,
            ):
                number_start = match.start(1)
                prefix = text[max(0, number_start - 24):number_start]
                if re.search(
                    r"(?:http(?:[_ -]?status)?|status|状态码)\s*[:=]?\s*$",
                    prefix,
                    re.I,
                ):
                    continue
                port = int(match.group(1))
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
            for match in re.findall(
                r"(?:\b(?:set|configure|assign|allocate|change|migrate)\w*\b|"
                r"设置|配置|分配|修改|迁移).{0,35}?"
                r"(?:\bport\b|_port|端口).{0,20}?([1-9]\d{1,4})",
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
        # Old/current ports describe observed runtime state (for example the
        # listener a root-bound stop must target).  They are not destination
        # allocations and must never be rewritten merely because they are, by
        # definition, occupied.
        if re.search(
            r"(?:^|_)(?:old|current|existing|observed|source)(?:_|$).*port|"
            r"port(?:_|$).*(?:old|current|existing|observed|source)(?:_|$)",
            lowered,
        ):
            return False
        return str(resource.source or "") not in {"existing_runtime", "existing_config"} and not (
            "internal" in lowered
            or "container_port" in lowered
            or "image_port" in lowered
            or str(resource.source or "") == "derived_observation"
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
        requests = []
        for item in value:
            if not isinstance(item, dict):
                continue
            probe = str(item.get("probe") or "")
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            args = dict(args)
            probe, args = normalize_probe_request(probe, args)
            if probe == "ports" and "ports" not in args:
                candidates = args.pop(
                    "candidates",
                    args.pop("candidate_ports", None),
                )
                if isinstance(candidates, list):
                    ports = []
                    for value in candidates:
                        try:
                            port = int(value)
                        except (TypeError, ValueError):
                            continue
                        if 1 <= port <= 65535 and port not in ports:
                            ports.append(port)
                    args["ports"] = ports[:64]
            elif probe == "ports" and isinstance(args.get("ports"), list):
                args["ports"] = args["ports"][:64]
            requests.append(
                ProbeRequest(
                    probe,
                    args,
                    str(item.get("purpose") or "resolve planning evidence gap"),
                )
            )
        return requests

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
