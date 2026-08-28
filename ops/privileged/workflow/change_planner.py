"""Mutation-only Change Planner for Ops-Privilege."""

from __future__ import annotations

import json
import base64
import hashlib
import re
from pathlib import Path
from typing import Any

from klonet_agent.ops.privileged.contracts import PlanResource, RISK_LEVELS
from klonet_agent.ops.privileged.environment_facts import REQUIRED_ENTRY_FILES
from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.context import klonet_domain_context
from klonet_agent.ops.privileged.checkers import (
    CHECKER_REQUIRED_ARGS,
    DefaultCheckerRegistry,
)
from klonet_agent.ops.privileged.workflow.contracts import (
    ChangePlan,
    ChangeStep,
    EvidenceBundle,
    EvidenceConclusion,
    GoalOutcome,
    ProbeRequest,
    RuntimeComponentSpec,
    normalize_probe_request,
)
from klonet_agent.ops.privileged.workflow.discovery import parse_json_object
from klonet_agent.ops.privileged.workflow.runtime_inventory import RuntimeInventory


def _plan_resource_name(*parts: Any) -> str:
    """Return one deterministic identifier accepted by PlanResource."""

    value = "_".join(
        re.sub(r"[^A-Za-z0-9_]+", "_", str(part or "")).strip("_")
        for part in parts
        if str(part or "").strip()
    ) or "resource"
    if not value[0].isalpha():
        value = "instance_" + value
    if len(value) > 64:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        value = value[:55].rstrip("_") + "_" + digest
    return value


def _inventory_missing_runtime_roles(
    bundle: EvidenceBundle,
) -> list[tuple[str, str, set[str]]]:
    missing = []
    for instance in RuntimeInventory.from_bundle(bundle).abnormal:
        roles = {
            role
            for role in ("master", "worker")
            if re.search(
                r"\b%s_endpoint=not_checked\s+reason=role_not_running\b"
                % role,
                instance.raw_line,
            )
        }
        if roles:
            missing.append((instance.project_root, instance.platform, roles))
    return missing


def _runtime_component_specs_from_line(
    line: str, *, evidence_ref: str,
) -> dict[str, RuntimeComponentSpec]:
    match = re.search(r"\bcomponent_specs_b64=([^\s]+)", str(line or ""))
    if match is None:
        return {}
    try:
        padding = "=" * (-len(match.group(1)) % 4)
        raw = json.loads(base64.urlsafe_b64decode(
            (match.group(1) + padding).encode("ascii")
        ).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return {}
    specs: dict[str, RuntimeComponentSpec] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            spec = RuntimeComponentSpec.from_dict(
                item, evidence_refs=(evidence_ref,),
            )
        except (TypeError, ValueError):
            continue
        specs[spec.name] = spec
    return specs


def _ordered_default_restart_components(
    specs: dict[str, RuntimeComponentSpec],
) -> list[str]:
    selected = {
        name: spec for name, spec in specs.items()
        if spec.category == "application" and spec.managed and spec.default_restart
    }
    ordered: list[str] = []
    pending = dict(selected)
    while pending:
        ready = sorted(
            name for name, spec in pending.items()
            if all(dep not in selected or dep in ordered for dep in spec.start_after)
        )
        if not ready:
            return []
        for name in ready:
            ordered.append(name)
            pending.pop(name)
    return ordered


def _ordered_managed_running_components(
    specs: dict[str, RuntimeComponentSpec],
    running_roles: set[str],
) -> list[str]:
    selected = {
        name: spec for name, spec in specs.items()
        if spec.category == "application"
        and spec.managed
        and name in running_roles
    }
    ordered: list[str] = []
    pending = dict(selected)
    while pending:
        ready = sorted(
            name for name, spec in pending.items()
            if all(dep not in selected or dep in ordered for dep in spec.start_after)
        )
        if not ready:
            return []
        for name in ready:
            ordered.append(name)
            pending.pop(name)
    return ordered


def _runtime_component_resource_payload(
    spec: RuntimeComponentSpec,
) -> dict[str, Any]:
    """Serialize one component contract without inventing empty capabilities."""

    payload: dict[str, Any] = {
        "name": spec.name,
        "category": spec.category,
        "managed": spec.managed,
        "default_restart": spec.default_restart,
        "screen_suffix": spec.screen_suffix,
    }
    for key, values in (
        ("command_argv", spec.command_argv),
        ("preflight_argv", spec.preflight_argv),
        ("ports", spec.ports),
        ("health_checks", spec.health_checks),
        ("start_after", spec.start_after),
    ):
        if values:
            payload[key] = list(values)
    return payload


def _frozen_step_role_port(
    resources: list[Any],
    step_id: str,
    role: str,
) -> int | None:
    """Resolve the one target port consumed by a semantic runtime step.

    Migration plans intentionally retain the observed old port as an input to
    the stop/config transition.  That evidence is not the target port.  The
    exact ``<step>.<role>_port`` consumer is the authoritative destination;
    a fallback is allowed only when there is exactly one role-port candidate.
    """

    exact: set[int] = set()
    candidates: set[int] = set()
    argument = "%s_port" % role
    for resource in resources:
        if isinstance(resource, dict):
            status = str(resource.get("status") or "")
            kind = str(resource.get("kind") or "")
            resource_role = str(resource.get("role") or "")
            value = resource.get("value")
            consumers = resource.get("consumers") or []
        else:
            status = str(getattr(resource, "status", "") or "")
            kind = str(getattr(resource, "kind", "") or "")
            resource_role = str(getattr(resource, "role", "") or "")
            value = getattr(resource, "value", None)
            consumers = getattr(resource, "consumers", []) or []
        if (
            status != "frozen"
            or kind != "port"
            or resource_role != argument
            or not str(value).isdigit()
        ):
            continue
        owned_arguments = {
            str(consumer).partition(".")[2]
            for consumer in consumers
            if str(consumer).partition(".")[0] == step_id
        }
        if not owned_arguments:
            continue
        port = int(value)
        candidates.add(port)
        if argument in owned_arguments:
            exact.add(port)
    if len(exact) == 1:
        return next(iter(exact))
    if not exact and len(candidates) == 1:
        return next(iter(candidates))
    return None


def _runtime_component_identity(
    role: str,
    *,
    project_root: str,
    inventory_line: str,
    evidence_text: str,
    fallback_roles: tuple[str, ...] = (),
) -> tuple[int, str] | None:
    """Resolve one component's UID/interpreter from its runtime contract.

    A missing component may inherit only from roles explicitly named by its
    manifest dependency edge.  This keeps the identity model generic while
    avoiding the unsafe old behavior of borrowing an arbitrary process from
    the same repository.
    """

    identities_match = re.search(
        r"\b%s_identities=([^\s]+)" % re.escape(role),
        inventory_line,
    )
    if identities_match is not None:
        identities = {
            (int(match.group(2)), match.group(3))
            for raw in identities_match.group(1).split(",")
            for match in [re.fullmatch(r"(\d+):(\d+):(/[^,\s]+)", raw)]
            if match is not None
        }
        if len(identities) == 1:
            return next(iter(identities))

    for dependency_role in fallback_roles:
        dependency_match = re.search(
            r"\b%s_identities=([^\s]+)" % re.escape(dependency_role),
            inventory_line,
        )
        if dependency_match is None:
            continue
        dependency_identities = {
            (int(match.group(2)), match.group(3))
            for raw in dependency_match.group(1).split(",")
            for match in [re.fullmatch(r"(\d+):(\d+):(/[^,\s]+)", raw)]
            if match is not None
        }
        if len(dependency_identities) == 1:
            return next(iter(dependency_identities))

    # A human-confirmed implementation decision is authoritative for a role
    # that has no live process to inherit.  It is carried separately from the
    # immutable base goal and must name the exact root, role, UID and Python.
    decision_blocks = re.findall(
        r"(?is)decision_history[^\n]*\n?(.*)", evidence_text,
    )
    decision_text = "\n".join(decision_blocks)
    if (
        project_root in decision_text
        and re.search(
            r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(role),
            decision_text,
            re.I,
        )
    ):
        uid = re.search(r"\b(?:run_as_uid|uid)\s*[=:]\s*(\d+)\b", decision_text, re.I)
        python = re.search(
            r"\b(?:python|python_executable)\s*[=:]\s*"
            r"(/[A-Za-z0-9._/+:-]*python(?:\d+(?:\.\d+)*)?)",
            decision_text,
            re.I,
        )
        if uid is not None and python is not None:
            return int(uid.group(1)), python.group(1)

    role_pattern = {
        "master": r"\bmaster_main\b",
        "worker": r"\bworker_main\b|\bworker_gun\.py\b",
        "celery": r"\bcelery_worker\b|(?:^|\s)-A\s+[^\s]*celery",
        "web_terminal": r"\bweb_terminal_main\b|\bcreate_web_terminal_app\b",
    }.get(role, r"(?!)")
    current_uid = re.search(r"\bcurrent_uid=(\d+)\b", evidence_text)
    for raw_line in evidence_text.splitlines():
        cwd = re.search(r"\bcwd=([^\s]+)", raw_line)
        executable = re.search(
            r"\bcmd(?:line)?=(/[^\s]*python[^\s]*)", raw_line,
        )
        if (
            cwd is None
            or executable is None
            or not re.search(role_pattern, raw_line, re.I)
            or cwd.group(1).rstrip("/") not in {
                project_root.rstrip("/"),
                project_root.rstrip("/") + "/mains",
            }
        ):
            continue
        uid = re.search(r"\buid=(\d+)\b", raw_line) or current_uid
        if uid is not None:
            return int(uid.group(1)), executable.group(1)

    generic = {
        (int(match.group(2)), match.group(3))
        for raw in re.findall(r"\bruntime_identities=([^\s]+)", inventory_line)
        for item in raw.split(",")
        for match in [re.fullmatch(r"(\d+):(\d+):(/[^,\s]+)", item)]
        if match is not None
    }
    return next(iter(generic)) if len(generic) == 1 else None


def _request_rechecks_confirmed_missing_role(
    request: ProbeRequest,
    missing: list[tuple[str, str, set[str]]],
) -> bool:
    if request.probe not in {"process", "process_detail"}:
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
You are the Klonet Ops-Privilege Change Planner.
Plan only real host state changes. Discovery, inspection, evidence aggregation,
summaries, reports, answers and verification are separate workflow phases and
must never appear as changes. Do not select Action names or emit commands.
Evidence describes the observed state; it is never an execution decision by
itself. In particular, healthy/running/passed/ready observations may determine
risk and postconditions, but must not be promoted to preserve, skip, or
do-not-restart decisions. A preserve/no-change decision is valid only when the
user explicitly requested that boundary, or when evidence proves that the
resource already satisfies the goal-specific target state. Compare evidence
with the complete goal before deciding whether a change is required.
When the evidence conclusion contains a deterministic `User-selected Screen
source maps authoritatively` fact with repository, remote, branch and revision,
use it as the source contract and do not request source discovery again.
When `plan_execution` evidence is present, this is recovery replanning after an
approved plan failed verification. Preserve steps already proved completed,
never emit them again, and plan only the failed or still-unmet effects. Use the
execution observation, checks, and fresh runtime evidence to address the actual
failure instead of blindly retrying the old plan.

Return one JSON object with status `need_evidence`, `ready`, or `blocked`.
For need_evidence return at most four semantic read-only probe_requests. Prefer
a registered probe, but when the catalog cannot express the required fact,
name the intended capability and let Discovery bind a safe read-only command.
Every request must state required_facts and may set freshness to `cached` or
`refresh`. Never emit a command yourself.
Give each unresolved semantic fact a stable gap_id beginning with `gap-` and
reuse that exact gap_id on every later request for the same missing fact even
if the probe, wording, or required_facts change. List the semantic step IDs
that may change after resolution in affected_steps.
When any semantic steps are already grounded, include them in changes/resources
even with need_evidence. They form the persistent candidate_plan; later Replan
may change only affected_steps and must preserve every other step verbatim.
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

A Klonet runtime's authoritative application-component set comes from the
current RuntimeInventory component manifest. The standard components are
master (`<instance>_m`), celery (`<instance>_c`), web terminal
(`<instance>_web`), and worker (`<instance>_w`), but an observed or manifested
managed application component is equally valid. An explicit component-scoped
user decision overrides the platform default-restart set: never replace an
explicit custom component with the four standard components, and never include
components the user explicitly excluded. It has distinct frozen
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
For a managed non-standard component, preserve its inventory-proven Screen
suffix, argv, runtime identity, ports and health checks. If a registered Action
cannot cover it, keep the semantic component unchanged so Binding can use the
existing policy-validated Shell fallback; never substitute another component.
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

USER_SCOPE_DECISION_MARKERS = (
    "是否允许", "是否授权", "纳入本次变更范围", "扩大变更范围",
    "不属于目标实例", "外部进程", "外部运行时", "改用空闲端口",
    "本次跳过", "本次不启动",
    "whether to allow", "whether to authorize", "include in this change",
    "expand the change scope", "outside the target instance",
)


def _offloads_discoverable_implementation_detail(item: str) -> bool:
    """Separate missing technical facts from genuine user authority.

    Discovery must resolve what owns a port or how a component starts.  Once
    those facts are known, only the user can authorize expanding a mutation to
    an independently owned process.  Mentioning a port in that authorization
    question does not turn it back into an implementation-detail gap.
    """

    lowered = str(item or "").lower()
    if any(marker in lowered for marker in USER_SCOPE_DECISION_MARKERS):
        return False
    return any(marker in lowered for marker in DISCOVERABLE_IMPLEMENTATION_MARKERS)


def _runtime_port_conflict_resolution(
    decision_text: str,
    *,
    project_root: str,
    role: str,
    port: int | None,
) -> str:
    """Interpret only the user's policy choice, never infer it from evidence."""

    text = str(decision_text or "").lower()
    if not text:
        return ""
    # A recovery reply may be normalized by the existing Planner before it
    # reaches this deterministic guard.  If it names an absolute path, that
    # path is authoritative: do not let a parent such as ``/home/lzl`` match
    # the explicitly selected ``/home/lzl/vemu_uestc`` merely by substring.
    explicit_paths = {
        candidate.rstrip("/.,;:，；。") or "/"
        for candidate in re.findall(r"/[A-Za-z0-9_.+@%=-]+(?:/[A-Za-z0-9_.+@%=-]+)*", text)
    }
    normalized_root = str(project_root or "").rstrip("/") or "/"
    if explicit_paths:
        scoped = normalized_root in explicit_paths
    else:
        scoped = bool(
            (port is not None and re.search(r"(?<!\d)%s(?!\d)" % port, text))
            or re.search(r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(role.lower()), text)
        )
    if not scoped:
        return ""
    if re.search(
        r"不(?:要|再)?启动|不启动|跳过|排除|暂不启动|本次不处理|"
        r"skip|exclude|do not start|don't start",
        text,
        re.I,
    ):
        return "skip"
    if (
        re.search(
            r"修改|换|改用|分配|自动选择|查找|"
            r"change|modify|reassign|allocate|select|find",
            text,
            re.I,
        )
        and re.search(r"端口|空闲|\bport\b|\bfree\b", text, re.I)
    ):
        return "reassign"
    return ""


def _runtime_port_candidates(
    current_port: int,
    inventory: RuntimeInventory,
    *,
    additionally_reserved: set[int] | None = None,
) -> list[int]:
    """Return a bounded deterministic sample; Discovery proves availability."""

    reserved = {
        int(port)
        for instance in inventory.instances
        for port in instance.configured_ports.values()
    }
    reserved.update(additionally_reserved or set())
    result = []
    for offset in range(1, 65):
        candidate = current_port + offset
        if candidate > 65535:
            candidate = current_port - offset
        if 1024 <= candidate <= 65535 and candidate not in reserved:
            result.append(candidate)
        if len(result) == 16:
            break
    return result


def _explicit_runtime_port_decision(
    decision_text: str,
    *,
    project_root: str,
    role: str,
) -> int | None:
    """Read an explicit target port from the user's decision contract.

    Runtime inventory may say which port is configured today, but it cannot
    override a user-approved migration target.  Require an exact instance
    path and role in the same bounded clause so a number belonging to another
    platform can never leak across an all-platform plan.
    """

    text = str(decision_text or "")
    root = str(project_root or "").rstrip("/") or "/"
    if root not in text:
        return None
    clauses = re.split(r"[\n。！？;；]", text)
    for clause in clauses:
        if root not in clause or not re.search(
            r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(role),
            clause,
            re.I,
        ):
            continue
        match = re.search(
            r"(?:使用|改用|迁移到|切换到|目标|new|target|use|move(?:d)?\s+to)"
            r"[^\d\n]{0,24}(?:新\s*)?(?:端口|port)?[^\d\n]{0,8}"
            r"(\d{4,5})",
            clause,
            re.I,
        )
        if match is None:
            match = re.search(
                r"(?:新\s*端口|new\s+port|target\s+port)\D{0,8}(\d{4,5})",
                clause,
                re.I,
            )
        if match is not None:
            port = int(match.group(1))
            if 1024 <= port <= 65535:
                return port
    return None


def _approved_component_identity(
    candidate_plan: ChangePlan | None,
    *,
    project_root: str,
    role: str,
) -> tuple[int, str] | None:
    """Reuse an approved static startup identity for the exact pending role.

    Runtime evidence remains authoritative for live processes.  During an
    execution Replan, however, a missing role must not rediscover immutable
    UID/interpreter decisions already frozen in the approved predecessor.
    """

    if candidate_plan is None:
        return None
    root_steps = {
        str(consumer).partition(".")[0]
        for resource in candidate_plan.resources
        if resource.status == "frozen"
        and resource.role == "instance_root"
        and str(resource.value).rstrip("/") == project_root.rstrip("/")
        for consumer in resource.consumers
        if str(consumer).partition(".")[0]
    }
    def exact_value(resource_role: str, argument: str) -> str | None:
        values = {
            str(resource.value)
            for resource in candidate_plan.resources
            if resource.status == "frozen"
            and resource.role == resource_role
            and any(
                "%s.%s_%s" % (owner_step, role, argument)
                in resource.consumers
                for owner_step in root_steps
            )
        }
        return next(iter(values)) if len(values) == 1 else None

    uid = exact_value("run_as_uid", "run_as_uid")
    python = exact_value("python_executable", "python_executable")
    if (
        uid is not None
        and python is not None
        and uid.isdigit()
        and python.startswith("/")
        and "python" in python.lower()
    ):
        return int(uid), python

    # Some older exact plans froze startup identity only in their atomic
    # ExecutionBinding.  That binding is still part of the authorized plan
    # hash, so it is authoritative after approval.  Never inherit it from an
    # unapproved draft or from another root/role.
    if not str(getattr(candidate_plan, "authorized_hash", "") or ""):
        return None
    bound_identities: set[tuple[int, str]] = set()
    for change in candidate_plan.steps:
        implementation = getattr(change, "implementation_plan", None)
        atomic_steps = (
            list(getattr(implementation, "steps", []) or [])
            if implementation is not None else [change]
        )
        for atomic in atomic_steps:
            binding = getattr(atomic, "execution_binding", None)
            if str(getattr(binding, "action", "") or "") not in {
                "start_screen_component", "restart_screen_component",
            }:
                continue
            args = dict(getattr(binding, "args", {}) or {})
            bound_root = str(
                args.get("project_root") or args.get("instance_root") or ""
            ).rstrip("/")
            if bound_root != project_root.rstrip("/"):
                continue
            if str(args.get("component") or "") != role:
                continue
            bound_uid = str(args.get("run_as_uid") or "")
            bound_python = str(args.get("python_executable") or "")
            if (
                bound_uid.isdigit()
                and bound_python.startswith("/")
                and "python" in bound_python.lower()
            ):
                bound_identities.add((int(bound_uid), bound_python))
    return next(iter(bound_identities)) if len(bound_identities) == 1 else None


def _automatic_conflict_port_policy(decision_text: str) -> bool:
    """Return only a user-authored, plan-wide automatic port decision."""

    text = str(decision_text or "")
    if re.search(r"/[A-Za-z0-9_.+@%=-]+(?:/[A-Za-z0-9_.+@%=-]+)+", text):
        # A path-scoped decision is local; it cannot silently authorize
        # changing unrelated instances in the same plan.
        pathless = "\n".join(
            clause for clause in re.split(r"[\n。！？;；]", text)
            if not re.search(r"/[A-Za-z0-9_.+@%=-]+(?:/[A-Za-z0-9_.+@%=-]+)+", clause)
        )
    else:
        pathless = text
    return bool(re.search(
        r"(?:自动(?:选择|分配|查找|改用)?|automatically(?:\s+(?:select|allocate|find|use))?)"
        r"[^\n。！？]{0,24}(?:空闲端口|未占用端口|free\s+port|unoccupied\s+port)",
        pathless,
        re.I,
    ))


def _first_checked_free_port(
    bundle: EvidenceBundle,
    candidates: list[int],
) -> int | None:
    checked: set[int] = set()
    occupied: set[int] = set()
    candidate_set = set(candidates)
    for record in bundle.records:
        if record.status != "available" or record.request.probe != "ports":
            continue
        requested = {
            int(item)
            for item in record.request.args.get("ports", [])
            if str(item).isdigit() and int(item) in candidate_set
        }
        checked.update(requested)
        for port in requested:
            if re.search(r":%s\b" % port, record.output):
                occupied.add(port)
    return next(
        (port for port in candidates if port in checked and port not in occupied),
        None,
    )


def _requests_screen_management_transition(text: str) -> bool:
    """Recognize the Screen-owned runtime invariant, not mere Screen mention."""

    lowered = str(text or "").lower()
    return bool(
        re.search(r"\bscreen\b", lowered, re.I)
        and re.search(
            r"收编|纳入[^\n。！？]{0,24}管理|"
            r"统一[^\n。！？]{0,24}管理|"
            r"(?:move|migrate|adopt|bring)[^\n.]{0,40}"
            r"(?:under|into)[^\n.]{0,16}\bscreen\b|"
            r"\bscreen[- ]managed\b|\bunder screen management\b",
            lowered,
            re.I,
        )
    )


def _requests_new_platform_deployment(text: str) -> bool:
    """Recognize creation as a deployment lifecycle, never a restart."""

    value = str(text or "")
    return bool(
        re.search(
            r"\b(?:create|deploy|provision|clone)\b|创建|新建|部署|克隆",
            value,
            re.I,
        )
        and re.search(r"\b(?:klonet|platform|instance)\b|平台|实例", value, re.I)
    )


def _reply_preserves_existing_goal_scope(text: str) -> bool:
    """Return whether the user explicitly freezes the existing objective."""

    value = str(text or "")
    return bool(re.search(
        r"(?:保持|保留|维持)[^\n。！？]{0,24}(?:完整)?(?:目标|范围|步骤)"
        r"[^\n。！？]{0,12}不变|"
        r"(?:其他|其余)[^\n。！？]{0,16}(?:目标|范围|步骤|平台|角色)"
        r"[^\n。！？]{0,12}不变|"
        r"(?:不改变|不要改变|不覆盖)[^\n。！？]{0,16}(?:目标|范围)|"
        r"\b(?:keep|preserve|leave)\b[^\n.]{0,32}"
        r"\b(?:goal|scope|other steps?|remaining targets?)\b"
        r"[^\n.]{0,16}\b(?:unchanged|intact)\b",
        value,
        re.I,
    ))


def _requests_all_runtime_roles(text: str) -> bool:
    """Recognize the complete managed-role scope before local refinements."""

    return bool(re.search(
        r"(?:所有|全部)(?:的)?(?:应用|受管|运行中|运行时|平台)?(?:的)?角色|"
        r"all\s+(?:(?:managed|application|runtime)\s+)?roles?",
        str(text or ""),
        re.I,
    ))


class ChangePlannerAgent:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def classify_reply_relation(
        self,
        *,
        base_goal: str,
        decision_history: list[str],
        reply: str,
        pending_question: str = "",
        evidence_bundle: EvidenceBundle | None = None,
    ) -> dict[str, Any]:
        """Classify one recovery reply without granting it authority to rewrite."""

        fallback = {
            "relation": "supplement",
            "reason": "无法证明用户明确覆盖或切换当前目标，安全保留 base_goal",
            "normalized_decision": str(reply or "").strip(),
            "candidate_base_goal": "",
            "conflicts": [],
        }
        if self.llm is None:
            return fallback
        knowledge = []
        if evidence_bundle is not None:
            knowledge = [
                {
                    "evidence_id": item.evidence_id,
                    "output": item.output[:6000],
                }
                for item in evidence_bundle.knowledge_records
                if item.status == "available"
            ]
        payload = {
            "base_goal": str(base_goal or "").strip(),
            "decision_history": [str(item) for item in decision_history],
            "pending_question": str(pending_question or "").strip(),
            "latest_reply": str(reply or "").strip(),
            "klonet_knowledge": knowledge,
        }
        prompt = """
You are the existing Klonet Change Planner classifying how one user reply
relates to the active base_goal. Return JSON only.

relation must be one of:
- supplement: answers a pending question, supplies a parameter/constraint, or
  adds implementation guidance without explicitly cancelling or replacing an
  existing target, scope, operation, or success criterion.
- revise: explicitly changes, removes, narrows, expands, or contradicts part of
  the active base_goal.
- new_goal: an independent objective not needed to finish the active goal.

The base_goal is authoritative and immutable in this classification call.
When uncertain, choose supplement. A reply focused on one failed component is
supplement unless it explicitly says other targets are no longer required.
Klonet knowledge explains domain meaning but cannot invent user intent.

Return:
{"relation":"supplement|revise|new_goal","reason":"...",
 "normalized_decision":"...","candidate_base_goal":"...","conflicts":["..."]}
For supplement candidate_base_goal must be empty. For revise/new_goal provide
the complete proposed replacement goal and list what would be superseded.
""".strip() + "\n\n" + klonet_domain_context("planner")
        try:
            response = self.llm.complete(
                messages=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    },
                ],
                tools=None,
                reasoning_effort="low",
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = str(response.choices[0].message.content or "")
            data = parse_json_object(content)
            relation = str(data.get("relation") or "").strip()
            if relation not in {"supplement", "revise", "new_goal"}:
                return fallback
            candidate = str(data.get("candidate_base_goal") or "").strip()
            if relation != "supplement" and not candidate:
                return fallback
            # The model may propose a replacement candidate even when the
            # user explicitly froze the existing goal and supplied only a
            # local implementation/acceptance correction.  Such a reply has
            # no authority to rewrite base_goal; preserve it as a decision.
            if (
                relation in {"revise", "new_goal"}
                and _reply_preserves_existing_goal_scope(reply)
            ):
                return {
                    **fallback,
                    "reason": (
                        "用户明确要求现有目标或其余范围保持不变；"
                        "本轮仅记录为局部补充"
                    ),
                    "normalized_decision": str(
                        data.get("normalized_decision") or reply or ""
                    ).strip(),
                }
            return {
                "relation": relation,
                "reason": str(data.get("reason") or "").strip(),
                "normalized_decision": str(
                    data.get("normalized_decision") or reply or ""
                ).strip(),
                "candidate_base_goal": candidate if relation != "supplement" else "",
                "conflicts": [
                    str(item) for item in data.get("conflicts") or []
                    if str(item).strip()
                ],
            }
        except Exception:
            return fallback

    def plan(
        self,
        goal: str,
        bundle: EvidenceBundle,
        conclusion: EvidenceConclusion,
        *,
        binding_feedback: str = "",
        intent_context: dict[str, Any] | None = None,
        candidate_plan: ChangePlan | None = None,
    ) -> GoalOutcome:
        effective_intent_context = dict(intent_context or {})
        authoritative_goal = str(
            effective_intent_context.get("base_goal") or goal
        )
        if _requests_new_platform_deployment(authoritative_goal):
            # A later implementation refinement may mention restarting or
            # Screen-owning the roles it will create.  The immutable base goal
            # still owns the lifecycle: this is deployment, not runtime restart.
            effective_intent_context["operation"] = "none"
        elif _requests_screen_management_transition(authoritative_goal):
            # Screen adoption is implemented by the existing restart lifecycle.
            # Normalize the semantic operation before all contract checks so a
            # healthy role is not incorrectly treated as out of scope.
            effective_intent_context["operation"] = "restart"
        deterministic = self._deterministic_runtime_restart(
            goal,
            bundle,
            intent_context=effective_intent_context,
            candidate_plan=candidate_plan,
        )
        if deterministic is not None:
            self._restore_authoritative_recovery_targets(
                deterministic,
                candidate_plan,
                bundle,
                intent_context=effective_intent_context,
            )
            # A recognized structured restart is authoritative.  Contract
            # errors must be repaired in that path; silently switching to a
            # free-form plan creates a second interpretation of the goal.
            return self._outcome(
                deterministic, goal, bundle,
                intent_context=effective_intent_context,
                candidate_plan=candidate_plan,
            )
        messages = [
            {
                "role": "system",
                "content": "%s\n\n%s\n\nRegistered read-only probe catalog:\n%s"
                % (
                    CHANGE_PLANNER_SYSTEM_PROMPT,
                    klonet_domain_context("planner"),
                    DEFAULT_READONLY_PROBES.render(),
                ),
            },
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
        if candidate_plan is not None:
            messages.append({
                "role": "user",
                "content": (
                    "Persistent candidate_plan (authoritative for unaffected steps):\n%s\n"
                    "Keep every step outside active_gap_affected_steps semantically"
                    " identical. Resolve only the stated evidence or binding gap."
                    % json.dumps(
                        candidate_plan.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                ),
            })
        if effective_intent_context:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Authoritative workflow intent and recovery directive:\n%s\n"
                        "base_goal is the complete authoritative objective."
                        " decision_history contains confirmed supplements and must"
                        " never replace or narrow base_goal. A proposed revision is"
                        " not authoritative until goal_revision_confirmed is true."
                        "When planning_strategy=component_by_component, preserve the"
                        " resolved project root and runtime identity, cover each"
                        " requested runtime component with independently bindable and"
                        " verifiable implementation effects, and do not repeat rejected"
                        " evidence needs unchanged."
                        % json.dumps(
                            effective_intent_context,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        )
                    ),
                }
            )
        last_error: Exception | None = None
        transport_error = False
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
                return self._outcome(
                    data, goal, bundle, intent_context=effective_intent_context,
                    candidate_plan=candidate_plan,
                )
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
                # A transport/provider failure is not a malformed planning
                # contract.  Repeating the same request here used to multiply
                # the SDK timeout by every JSON-repair generation.
                transport_error = True
                break
        return GoalOutcome(
            status="blocked",
            reason=(
                (
                    "Change Planner model request failed: %s"
                    if transport_error
                    else "Change Planner output invalid after bounded repairs: %s"
                )
                % str(last_error or "unknown planner failure")
            ),
        )

    @staticmethod
    def _restore_authoritative_recovery_targets(
        data: dict[str, Any],
        candidate_plan: ChangePlan | None,
        bundle: EvidenceBundle,
        *,
        intent_context: dict[str, Any] | None,
    ) -> None:
        """Keep unfinished approved targets that disappeared after execution.

        A stopped instance naturally disappears from ``running_platforms``.
        That observation changes its runtime state, not the user-approved
        target set.  Reuse the existing semantic step and frozen static
        resources for such a code-only target; discard transient PID
        resources so Binding discovers the current implementation safely.
        """

        context = intent_context or {}
        if (
            candidate_plan is None
            or not bool(context.get("recovery_scope_authoritative"))
            or str(data.get("status") or "") != "ready"
        ):
            return
        required_steps = {
            str(item)
            for item in context.get("recovery_required_step_ids") or []
            if str(item)
        }
        required_roots = {
            str(item).rstrip("/")
            for item in context.get("recovery_required_project_roots") or []
            if str(item).startswith("/")
        }
        if not required_steps or not required_roots:
            return
        inventory = RuntimeInventory.from_bundle(bundle)
        observable_roots = {
            *(item.project_root for item in inventory.instances),
            *inventory.code_only_roots,
        }
        raw_changes = data.get("changes")
        raw_resources = data.get("resources")
        if not isinstance(raw_changes, list) or not isinstance(raw_resources, list):
            return

        candidate_step_for_root: dict[str, str] = {}
        for resource in candidate_plan.resources:
            if str(resource.role or "") != "instance_root":
                continue
            root = str(resource.value or "").rstrip("/")
            for consumer in resource.consumers:
                step_id = str(consumer).split(".", 1)[0]
                if step_id in required_steps:
                    candidate_step_for_root[root] = step_id
                    break
        current_step_for_root: dict[str, str] = {}
        for item in raw_resources:
            if not isinstance(item, dict) or str(item.get("role") or "") != "instance_root":
                continue
            root = str(item.get("value") or "").rstrip("/")
            consumers = [str(value) for value in item.get("consumers") or []]
            if consumers:
                current_step_for_root[root] = consumers[0].split(".", 1)[0]
        renames = {
            current_step_for_root[root]: desired
            for root, desired in candidate_step_for_root.items()
            if root in current_step_for_root
            and current_step_for_root[root] != desired
        }
        if renames:
            for change in raw_changes:
                if not isinstance(change, dict):
                    continue
                step_id = str(change.get("step_id") or "")
                change["step_id"] = renames.get(step_id, step_id)
                change["depends_on"] = [
                    renames.get(str(item), str(item))
                    for item in change.get("depends_on") or []
                ]
            for item in raw_resources:
                if not isinstance(item, dict):
                    continue
                item["consumers"] = [
                    "%s%s" % (renames[prefix], consumer[len(prefix):])
                    if (prefix := str(consumer).split(".", 1)[0]) in renames
                    else str(consumer)
                    for consumer in item.get("consumers") or []
                ]

        current_roots = {
            str(item.get("value") or "").rstrip("/")
            for item in raw_resources
            if isinstance(item, dict)
            and str(item.get("role") or "") == "instance_root"
        }
        by_step = {step.step_id: step for step in candidate_plan.steps}
        evidence_refs = list(inventory.evidence_ids)
        restored_steps: set[str] = set()
        for resource in candidate_plan.resources:
            if str(resource.role or "") != "instance_root":
                continue
            root = str(resource.value or "").rstrip("/")
            consumers = [str(item) for item in resource.consumers]
            step_ids = {
                item.split(".", 1)[0] for item in consumers
                if item.split(".", 1)[0] in required_steps
            }
            if (
                not step_ids
                or root not in required_roots
                or root in current_roots
                or root not in observable_roots
            ):
                continue
            for step_id in sorted(step_ids):
                step = by_step.get(step_id)
                if step is None or step_id in restored_steps:
                    continue
                code_only = (
                    root in inventory.code_only_roots
                    and inventory.instance_for_root(root) is None
                )
                title = step.title
                objective = step.objective
                reason = step.reason
                expected_changes = list(step.expected_changes)
                if code_only:
                    title = re.sub(r"^重启", "恢复", title)
                    objective = re.sub(
                        r"(按项目根目录\s+\S+\s+)重启",
                        r"\1启动缺失的",
                        objective,
                        count=1,
                    )
                    expected_changes = list(dict.fromkeys(
                        re.sub(
                            r"^restart (?:requested|unhealthy) ",
                            "start missing ",
                            item,
                        )
                        for item in expected_changes
                    ))
                    recovery_reason = (
                        "刷新后的权威清单确认实例仅剩代码目录，"
                        "因此恢复缺失角色而不再生成停止动作"
                    )
                    if recovery_reason not in reason:
                        reason = "%s；%s" % (
                            reason.rstrip("；。"), recovery_reason,
                        )
                raw_changes.append({
                    "step_id": step.step_id,
                    "title": title,
                    "objective": objective,
                    "reason": reason,
                    "evidence_refs": evidence_refs,
                    "depends_on": list(step.depends_on),
                    "risk": step.risk,
                    "expected_changes": expected_changes,
                    "postconditions": [dict(item) for item in step.postconditions],
                })
                restored_steps.add(step_id)
                current_roots.add(root)

        existing_names = {
            str(item.get("name") or "") for item in raw_resources
            if isinstance(item, dict)
        }
        for resource in candidate_plan.resources:
            consumers = [str(item) for item in resource.consumers]
            if not any(
                item.split(".", 1)[0] in restored_steps for item in consumers
            ):
                continue
            role = str(resource.role or "")
            if role.endswith("_pid") or role == "pid":
                continue
            if resource.name in existing_names:
                continue
            raw_resources.append(resource.to_dict())
            existing_names.add(resource.name)

    @staticmethod
    def _deterministic_runtime_restart(
        goal: str,
        bundle: EvidenceBundle,
        intent_context: dict[str, Any] | None = None,
        candidate_plan: ChangePlan | None = None,
    ) -> dict[str, Any] | None:
        """Compile one runtime lifecycle intent without degrading into repair.

        Restarting a platform and adopting its live application roles under
        Screen management share one lifecycle contract: freeze each runtime
        identity, replace or start it once, and prove the resulting Screen and
        role health.  They must therefore use this one compiler rather than a
        free-form parallel planning path.
        """

        text = str(goal or "")
        lowered = text.lower()
        base_goal = str((intent_context or {}).get("base_goal") or text)
        base_lowered = base_goal.lower()
        structured_operation = str((intent_context or {}).get("operation") or "")
        if _requests_new_platform_deployment(base_goal):
            return None
        screen_lifecycle = _requests_screen_management_transition(base_goal)
        if (
            structured_operation != "restart"
            and not any(marker in lowered for marker in ("重启", "restart"))
            and not screen_lifecycle
        ):
            return None
        role_patterns = (
            ("master", r"(?<![A-Za-z0-9_])master(?![A-Za-z0-9_])"),
            ("celery", r"(?<![A-Za-z0-9_])celery(?![A-Za-z0-9_])"),
            ("web_terminal", r"web[_ -]?terminal|web终端"),
            ("worker", r"(?<![A-Za-z0-9_])worker(?![A-Za-z0-9_])"),
        )
        structured_roles = [
            str(role).strip().lower().replace("-", "_")
            for role in (intent_context or {}).get("components") or []
            if str(role).strip()
        ]
        all_roles_requested = _requests_all_runtime_roles(base_lowered)
        component_scoped = (
            str((intent_context or {}).get("scope") or "") == "component"
            and bool(structured_roles)
        )
        requested_roles = (
            structured_roles
            if component_scoped
            else [role for role, _pattern in role_patterns]
            if all_roles_requested
            else [
                role for role, pattern in role_patterns
                if re.search(pattern, lowered, re.I)
            ]
        )
        platform_wide = not component_scoped and (all_roles_requested or (
            not requested_roles and (
                str((intent_context or {}).get("scope") or "") == "platform"
                or bool(re.search(
                    r"平台|platform|整个|全部|all", base_lowered, re.I,
                ))
            )
        ))
        if platform_wide:
            requested_roles = [role for role, _pattern in role_patterns]
        if not requested_roles:
            return None
        resolved_root = str((intent_context or {}).get("resolved_project_root") or "")
        inventory = RuntimeInventory.from_bundle(bundle)
        all_instances_requested = (
            not bool((intent_context or {}).get("all_platform_expansion_child"))
            and
            bool(re.search(
                r"(?:全部|所有)(?:[^\n。！？]{0,24})?平台|"
                r"all(?:\s+current)?(?:\s+klonet)?\s+platforms?",
                base_lowered,
                re.I,
            ))
        )
        if all_instances_requested:
            # A decision about one failed instance constrains its recovery; it
            # cannot turn an all-platform base goal into a single-root goal.
            resolved_root = ""
        if not inventory.instances:
            return {
                "status": "need_evidence",
                "reason": "runtime_inventory_required_for_structured_restart",
                "probe_requests": [{
                    "probe": "running_platforms",
                    "args": {
                        **({"project_roots": [resolved_root]} if resolved_root else {}),
                        "allow_interactive_sudo": True,
                    },
                    "purpose": (
                        "按精确项目根目录取得所有目标实例、受管角色、现有端口和"
                        "运行身份，形成确定性 Screen 重启计划"
                    ),
                    "required_facts": [
                        "runtime instance project roots",
                        "managed component roles",
                        "configured component ports",
                        "per-role runtime identities",
                        "component startup contracts",
                    ],
                    "freshness": "refresh",
                    "gap_id": "gap-runtime-inventory",
                    "affected_steps": [],
                }],
            }
        candidates = (
            [
                item for item in inventory.instances
                if item.project_root == resolved_root
            ]
            if resolved_root
            else list(inventory.instances)
            if all_instances_requested
            else list(inventory.matching(text))
        )
        # During recovery the persisted predecessor plan, not a refreshed
        # all-platform inventory, owns the remaining target set.  The base
        # goal remains all-platform so that no user intent is narrowed, while
        # completed instance effects are excluded from this successor plan.
        # Code-only unfinished roots are restored below from the predecessor's
        # frozen resources by _restore_authoritative_recovery_targets().
        recovery_roots = {
            str(item).rstrip("/")
            for item in (intent_context or {}).get(
                "recovery_required_project_roots", []
            ) or []
            if str(item).startswith("/")
        }
        if (
            bool((intent_context or {}).get("recovery_scope_authoritative"))
            and recovery_roots
        ):
            observable_roots = {
                *(str(item.project_root).rstrip("/") for item in inventory.instances),
                *(str(item).rstrip("/") for item in inventory.code_only_roots),
            }
            disappeared = sorted(recovery_roots - observable_roots)
            if disappeared:
                raise ValueError(
                    "replan dropped authoritative recovery targets=%s"
                    % ",".join(disappeared)
                )
            candidates = [
                item for item in candidates
                if str(item.project_root).rstrip("/") in recovery_roots
            ]
        if len(candidates) > 1:
            return ChangePlannerAgent._combine_runtime_restart_instances(
                goal,
                bundle,
                candidates,
                intent_context=intent_context,
                candidate_plan=candidate_plan,
            )
        if len(candidates) != 1:
            return {
                "status": "need_evidence",
                "reason": "target_runtime_identity_unresolved",
                "probe_requests": [{
                    "probe": "running_platforms",
                    "args": {
                        **({"project_roots": [resolved_root]} if resolved_root else {}),
                        "allow_interactive_sudo": True,
                    },
                    "purpose": "刷新目标实例的精确项目根目录和运行角色",
                    "required_facts": [
                        "exact target project_root", "managed component roles",
                    ],
                    "freshness": "refresh",
                    "gap_id": "gap-target-runtime-identity",
                    "affected_steps": [],
                }],
            }
        selected = candidates[0]
        line = selected.raw_line
        root = selected.project_root
        frozen_identifiers = {
            str(key).rstrip("/"): str(value)
            for key, value in dict(
                (intent_context or {}).get(
                    "recovery_instance_identifiers_by_root", {}
                ) or {}
            ).items()
            if str(key).startswith("/") and str(value).strip()
        }
        alias = frozen_identifiers.get(root.rstrip("/"), selected.platform)
        completed_by_root = dict(
            (intent_context or {}).get(
                "recovery_completed_components_by_root", {}
            ) or {}
        )
        completed_roles = {
            str(role).strip().lower().replace("-", "_")
            for role in completed_by_root.get(root, [])
            if str(role).strip()
        }
        component_specs = _runtime_component_specs_from_line(
            line, evidence_ref=selected.evidence_id,
        )
        if platform_wide and component_specs:
            if screen_lifecycle and all_roles_requested:
                requested_roles = _ordered_managed_running_components(
                    component_specs, set(selected.roles),
                )
            else:
                requested_roles = _ordered_default_restart_components(component_specs)
        if screen_lifecycle and platform_wide and not all_roles_requested:
            optional_running = sorted(
                name for name, spec in component_specs.items()
                if spec.category == "application"
                and spec.managed
                and not spec.default_restart
                and name in set(selected.roles)
            )
            if optional_running:
                return {
                    "status": "blocked",
                    "reason": "optional_running_components_require_scope_decision",
                    "missing_decisions": [
                        "检测到非默认应用角色 %s 正在运行；请确认是否也将其收编到 Screen，或明确排除"
                        % "、".join(optional_running)
                    ],
                }
        if component_scoped and component_specs:
            requested_roles = [
                role for role in requested_roles
                if role in component_specs
                and component_specs[role].category == "application"
                and component_specs[role].managed
            ]
        # Recovery completion is an execution fact and therefore the final
        # scope floor. Apply it after manifest expansion so no Planner path can
        # reintroduce an already completed component.
        requested_roles = [
            role for role in requested_roles if role not in completed_roles
        ]
        if screen_lifecycle and selected.component_ownership:
            requested_roles = [
                role for role in requested_roles
                if not selected.component_is_fully_screen_managed(role)
            ]
        if screen_lifecycle and not requested_roles:
            return {
                "status": "achieved",
                "reason": (
                    "runtime inventory proves all requested application "
                    "components are already fully Screen-managed"
                ),
            }
        step_id = "restart-backend-roles"
        decision_text = "\n".join([
            base_goal,
            text,
            *[
                str(item)
                for item in (intent_context or {}).get("decision_history") or []
            ],
        ])
        port_overrides: dict[str, int] = {}
        inherited_overrides = dict(
            dict((intent_context or {}).get("runtime_port_overrides_by_root") or {})
            .get(root, {}) or {}
        )
        for role in requested_roles:
            port_key = role + "_port"
            existing_port = selected.configured_ports.get(port_key)
            raw_override = inherited_overrides.get(role)
            explicit_port = (
                int(raw_override)
                if str(raw_override or "").isdigit()
                else _explicit_runtime_port_decision(
                    decision_text,
                    project_root=root,
                    role=role,
                )
            )
            if explicit_port is None or explicit_port == existing_port:
                continue
            if not (1024 <= explicit_port <= 65535):
                return {
                    "status": "blocked",
                    "reason": "invalid_explicit_target_port=%s:%s" % (
                        role, explicit_port,
                    ),
                    "missing_decisions": [
                        "%s 的 %s 目标端口必须位于 1024-65535"
                        % (root, role)
                    ],
                }
            if _first_checked_free_port(bundle, [explicit_port]) != explicit_port:
                return {
                    "status": "need_evidence",
                    "reason": "explicit_target_port_requires_fresh_check=%s:%s"
                    % (role, explicit_port),
                    "probe_requests": [{
                        "probe": "ports",
                        "args": {"ports": [explicit_port]},
                        "purpose": (
                            "确认用户决定用于 %s 的 %s 目标端口 %s 当前未监听"
                            % (root, role, explicit_port)
                        ),
                        "required_facts": [
                            "target port listener state",
                            "target port is free before plan freezing",
                        ],
                        "freshness": "refresh",
                        "gap_id": "gap-%s-%s-explicit-target-port" % (
                            re.sub(r"[^A-Za-z0-9_.:-]+", "-", alias), role,
                        ),
                        "affected_steps": [step_id],
                    }],
                }
            port_overrides[role] = explicit_port
        skipped_conflict_roles: set[str] = set()
        observed_running_roles = set(selected.roles)
        # A code path is not a runtime identity.  For an already-running
        # backend role, require the configured port's actual listener binding
        # before producing an approvable stop/restart plan.  A missing role
        # uses the same destination contract: its configured port must be
        # proved free, or a foreign occupant must be preserved and replaced
        # with a checked-free destination.  Role presence never implies port
        # availability.  Legacy inventory rows without this typed field remain
        # compatible, but new rows may never fall back to the first PID or
        # smallest PGID.
        if "role_bindings_b64" in selected.fields:
            for role in requested_roles:
                if role not in {"master", "worker"}:
                    continue
                if role in port_overrides:
                    # The current listener remains stop/restart evidence.  The
                    # already checked user-selected destination is the sole
                    # target resource and must not be overwritten by it.
                    continue
                binding = selected.role_binding(role)
                if binding is None or binding.status in {
                    "owner_unavailable", "owner_ambiguous",
                }:
                    port = selected.configured_ports.get(role + "_port")
                    if port is None:
                        continue
                    return {
                        "status": "need_evidence",
                        "reason": "runtime_listener_identity_unresolved=%s" % role,
                        "probe_requests": [{
                            "probe": "port_owner",
                            "args": {
                                "ports": [port],
                                "allow_interactive_sudo": True,
                            },
                            "purpose": (
                                "确认 %s 的配置端口 %s 实际监听 PID、PGID、cwd "
                                "和运行时归属；不得按进程列表顺序猜测" % (role, port)
                            ),
                            "required_facts": [
                                "%s configured port listener pid" % role,
                                "%s configured port listener pgid" % role,
                                "%s listener runtime cwd" % role,
                                "%s listener role" % role,
                            ],
                            "freshness": "refresh",
                            "gap_id": "gap-%s-%s-listener-identity" % (
                                re.sub(r"[^A-Za-z0-9_.:-]+", "-", alias), role,
                            ),
                            "affected_steps": [step_id],
                        }],
                    }
                if binding.status == "confirmed":
                    observed_running_roles.add(role)
                    continue
                if binding.status in {"not_listening", "config_port_missing"}:
                    continue
                if binding.status in {"runtime_conflict", "role_conflict"}:
                    resolution = _runtime_port_conflict_resolution(
                        decision_text,
                        project_root=root,
                        role=role,
                        port=binding.configured_port,
                    )
                    if not resolution and _automatic_conflict_port_policy(
                        decision_text
                    ):
                        resolution = "reassign"
                    if resolution == "skip":
                        skipped_conflict_roles.add(role)
                        continue
                    if resolution == "reassign" and binding.configured_port is not None:
                        candidate_ports = _runtime_port_candidates(
                            binding.configured_port, inventory,
                        )
                        replacement = _first_checked_free_port(
                            bundle, candidate_ports,
                        )
                        if replacement is None:
                            return {
                                "status": "need_evidence",
                                "reason": "checked_free_replacement_port_required=%s" % role,
                                "probe_requests": [{
                                    "probe": "ports",
                                    "args": {"ports": candidate_ports},
                                    "purpose": (
                                        "为 %s 的 %s 选择一个经检查未监听的新端口；"
                                        "保留当前端口占用者" % (root, role)
                                    ),
                                    "required_facts": [
                                        "bounded candidate port listener states",
                                        "at least one checked free replacement port",
                                    ],
                                    "freshness": "refresh",
                                    "gap_id": "gap-%s-%s-replacement-port" % (
                                        re.sub(r"[^A-Za-z0-9_.:-]+", "-", alias), role,
                                    ),
                                    "affected_steps": [step_id],
                                }],
                            }
                        port_overrides[role] = replacement
                        continue
                    return {
                        "status": "blocked",
                        "reason": (
                            "%s configured port %s is owned by a different runtime: "
                            "listener_pid=%s listener_pgid=%s runtime_root=%s "
                            "code_root=%s; the existing owner will be preserved"
                            % (
                                role,
                                binding.configured_port or "unknown",
                                binding.listener_pid or "unknown",
                                binding.listener_pgid or "unknown",
                                binding.runtime_root,
                                binding.code_root,
                            )
                        ),
                        "missing_decisions": [
                            (
                                "冲突处理请选择其一：保留当前占用者，由系统查找空闲端口并"
                                "修改 %s 的 %s 后继续；或者保留当前占用者，本次跳过该角色。"
                                "系统不会停止当前占用者"
                            ) % (root, role)
                        ],
                    }
        if skipped_conflict_roles:
            requested_roles = [
                role for role in requested_roles
                if role not in skipped_conflict_roles
            ]
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
        for role in requested_roles:
            session = selected.screen_session(role)
            if not session:
                spec = component_specs.get(role)
                if spec is None:
                    continue
                session = "%s_%s" % (alias, spec.screen_suffix)
            resources.append({
                "name": _plan_resource_name(step_id, role, "screen_session"),
                "kind": "identifier", "status": "frozen",
                "role": "screen_session", "value": session,
                "source": "running_platforms",
                "consumers": [step_id + ".screen_session"],
            })
            if screen_lifecycle:
                resources.append({
                    "name": _plan_resource_name(
                        step_id, role, "screen_lifecycle_mode",
                    ),
                    "kind": "identifier", "status": "frozen",
                    "role": "runtime_component_lifecycle:%s" % role,
                    "value": "screen_adoption",
                    "source": "user_screen_management_goal",
                    "consumers": [
                        step_id + ".%s_lifecycle_mode" % role,
                    ],
                })
                orphan_pids = list(selected.component_orphan_pids(role))
                if orphan_pids:
                    resources.append({
                        "name": _plan_resource_name(
                            step_id, role, "orphan_group_leaders",
                        ),
                        "kind": "pid_set", "status": "frozen",
                        "role": "runtime_component_orphan_pids:%s" % role,
                        "value": orphan_pids,
                        "source": "running_platforms_component_ownership",
                        "consumers": [
                            step_id + ".%s_orphan_pids" % role,
                        ],
                    })
        expected = []
        postconditions = []
        running_roles = observed_running_roles
        configured_ports = dict(selected.configured_ports)
        identity_text = "\n".join(
            item.output for item in bundle.records if item.status == "available"
        )
        decisions = list((intent_context or {}).get("decision_history") or [])
        if decisions:
            identity_text += "\ndecision_history\n" + "\n".join(
                str(item) for item in decisions
            )
        for role in requested_roles:
            disposition = (
                "adopt running"
                if screen_lifecycle and role in running_roles
                else "restart requested" if role in running_roles
                else "start missing"
            )
            port_key = role + "_port"
            port_match = re.search(r"\b%s=(\d{1,5})" % port_key, line)
            existing_port = (
                int(port_match.group(1)) if port_match is not None
                else configured_ports.get(port_key)
            )
            port = port_overrides.get(role, existing_port)
            if role in {"master", "worker"}:
                if port is None:
                    return None
                if role in port_overrides and existing_port is not None:
                    expected.append(
                        "%s changes from %s to checked-free port %s while the "
                        "existing listener on %s is preserved"
                        % (port_key, existing_port, port, existing_port)
                    )
                expected.append(
                    "%s %s role at %s and backend health succeeds"
                    % (disposition, role, port)
                )
                postconditions.append({
                    "checker": "backend_health",
                    "args": {
                        "url": "http://127.0.0.1:%s/server_health/" % port,
                        "expected_code": 1,
                    },
                })
            elif role == "web_terminal":
                if port is None:
                    return None
                expected.append(
                    "%s web_terminal role at %s and listener readiness succeeds"
                    % (disposition, port)
                )
                postconditions.append({
                    "checker": "port_listening", "args": {"port": port},
                })
            elif role == "celery":
                expected.append(
                    "%s celery role and process readiness succeeds" % disposition
                )
                postconditions.append({
                    "checker": "process_running",
                    "args": {"pattern": "celery", "cwd": root},
                })
            else:
                spec = component_specs.get(role)
                if spec is None or not spec.managed:
                    return None
                expected.append(
                    "%s managed component %s and component readiness succeeds"
                    % (disposition, role)
                )
                postconditions.extend(list(spec.health_checks) or [{
                    "checker": "screen_session_exists",
                    "args": {"session": "%s_%s" % (alias, spec.screen_suffix)},
                }])
                resources.append({
                    "name": "component_%s_spec" % role,
                    "kind": "string", "status": "frozen",
                    "role": "runtime_component_spec:%s" % role,
                    "value": json.dumps(
                        _runtime_component_resource_payload(spec),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "source": "runtime_component_inventory",
                    "consumers": [step_id + ".component_spec"],
                })
            if port is not None:
                if role in port_overrides and existing_port is not None:
                    resources.append({
                        "name": "old_%s" % port_key,
                        "kind": "port", "status": "frozen",
                        "role": port_key, "value": existing_port,
                        "source": "conflicting_existing_listener",
                        "consumers": [step_id + ".old_" + port_key],
                    })
                resources.append({
                    "name": port_key, "kind": "port", "status": "frozen",
                    "role": port_key, "value": port,
                    "source": (
                        "checked_free_replacement"
                        if role in port_overrides else "existing_runtime"
                    ),
                    "consumers": [step_id + "." + port_key],
                })
            identity = _runtime_component_identity(
                role,
                project_root=root,
                inventory_line=line,
                evidence_text=identity_text,
                fallback_roles=(
                    tuple(component_specs[role].start_after)
                    if role in component_specs else ()
                ),
            )
            if identity is None:
                identity = _approved_component_identity(
                    candidate_plan,
                    project_root=root,
                    role=role,
                )
            if identity is None:
                spec = component_specs.get(role)
                command = list(spec.command_argv) if spec is not None else []
                current_uid = re.search(r"\bcurrent_uid=(\d+)\b", identity_text)
                if (
                    command
                    and str(command[0]).startswith("/")
                    and "python" in str(command[0]).lower()
                    and current_uid is not None
                ):
                    identity = (int(current_uid.group(1)), str(command[0]))
            if identity is None:
                if role not in running_roles:
                    return {
                        "status": "need_evidence",
                        "reason": "component_runtime_identity_unresolved=%s" % role,
                        "probe_requests": [{
                            "probe": "runtime_startup_identity",
                            "args": {
                                "project_root": root,
                                "component": role,
                            },
                            "purpose": (
                                "从目标实例的文件属主、受管组件依赖和现有启动环境"
                                "确定缺失 %s 的运行用户与 Python 解释器" % role
                            ),
                            "required_facts": [
                                "%s run_as_uid" % role,
                                "%s python executable" % role,
                                "%s startup cwd" % role,
                                "%s startup argv" % role,
                            ],
                            "freshness": "refresh",
                            "gap_id": "gap-%s-%s-runtime-identity" % (
                                re.sub(r"[^A-Za-z0-9_.:-]+", "-", alias), role,
                            ),
                            "affected_steps": [step_id],
                        }],
                    }
                role_keywords = {
                    "master": ["master_main"],
                    "worker": ["worker_main", "worker_gun.py"],
                    "celery": ["celery_worker", "celery"],
                    "web_terminal": ["web_terminal_main"],
                }.get(role, [role])
                return {
                    "status": "need_evidence",
                    "reason": "component_runtime_identity_unresolved=%s" % role,
                    "probe_requests": [{
                        "probe": "process_detail",
                        "args": {"process_keywords": role_keywords},
                        "purpose": (
                            "核对所有实例中 %s 进程的解释器、用户和工作目录，"
                            "以继承现有运行身份" % role
                        ),
                        "required_facts": [
                            "%s process pid" % role,
                            "%s process cwd" % role,
                            "%s process run_as_uid" % role,
                            "%s process python executable" % role,
                            "%s process full cmdline" % role,
                        ],
                        "freshness": "refresh",
                        "gap_id": "gap-%s-%s-runtime-identity" % (
                            re.sub(r"[^A-Za-z0-9_.:-]+", "-", alias), role,
                        ),
                        "affected_steps": [step_id],
                    }],
                }
            run_as_uid, python_executable = identity
            resources.extend([
                {
                    "name": "%s_uid" % role,
                    "kind": "identifier", "status": "frozen",
                    "role": "%s_uid" % role, "value": run_as_uid,
                    "source": "runtime_evidence",
                    "consumers": [step_id + ".run_as_uid"],
                },
                {
                    "name": "%s_python" % role,
                    "kind": "path", "status": "frozen",
                    "role": "%s_python_executable" % role,
                    "value": python_executable,
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
                "title": (
                    "收编 %s 的应用组件（仅非合规项）" % alias
                    if screen_lifecycle else "重启 %s 的应用组件" % alias
                ),
                "objective": (
                    "按项目根目录 %s %s %s%s"
                    % (
                        root,
                        "仅收编" if screen_lifecycle else "重启",
                        " 和 ".join(requested_roles),
                        (
                            "；修改端口 %s，保留冲突端口的当前占用者"
                            % ",".join(
                                "%s:%s→%s" % (
                                    role,
                                    configured_ports.get(role + "_port"),
                                    port,
                                )
                                for role, port in sorted(port_overrides.items())
                            )
                            if port_overrides else ""
                        ),
                    )
                ),
                "reason": (
                    "用户要求 Screen 收编；运行清单已区分 Screen 托管进程组和游离进程组"
                    if screen_lifecycle
                    else "用户明确要求重启，运行清单已绑定实例根目录和角色端口"
                ),
                "evidence_refs": [
                    selected.evidence_id,
                    *[
                        item.evidence_id for item in bundle.knowledge_records
                        if item.status == "available"
                    ],
                ],
                "depends_on": [],
                "risk": "medium",
                "expected_changes": expected,
                "postconditions": postconditions,
            }],
        }

    @staticmethod
    def _combine_runtime_restart_instances(
        goal: str,
        bundle: EvidenceBundle,
        candidates: list[Any],
        *,
        intent_context: dict[str, Any] | None = None,
        candidate_plan: ChangePlan | None = None,
    ) -> dict[str, Any]:
        """Expand one all-platform restart into per-instance semantic changes."""

        resources: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        used_slugs: set[str] = set()
        inventory = RuntimeInventory.from_bundle(bundle)
        decision_text = "\n".join([
            str((intent_context or {}).get("base_goal") or goal),
            str(goal or ""),
            *[
                str(item)
                for item in (intent_context or {}).get("decision_history") or []
            ],
        ])
        runtime_port_overrides_by_root: dict[str, dict[str, int]] = {
            str(root): {
                str(role): int(port)
                for role, port in dict(values or {}).items()
                if str(port).isdigit()
            }
            for root, values in dict(
                (intent_context or {}).get("runtime_port_overrides_by_root") or {}
            ).items()
        }

        # Freeze destination ports for the complete plan before compiling any
        # child.  Per-instance compilation cannot see a collision introduced
        # by a sibling instance, so allowing every child to retain its current
        # configured port would produce an internally impossible plan.
        desired_ports: dict[int, list[tuple[Any, str, bool]]] = {}
        for instance in candidates:
            root = str(instance.project_root)
            specs = _runtime_component_specs_from_line(
                instance.raw_line, evidence_ref=instance.evidence_id,
            )
            roles = (
                _ordered_default_restart_components(specs)
                if specs else ["master", "celery", "web_terminal", "worker"]
            )
            for role in roles:
                if role not in {"master", "worker", "web_terminal"}:
                    continue
                configured = instance.configured_ports.get(role + "_port")
                explicit = _explicit_runtime_port_decision(
                    decision_text, project_root=root, role=role,
                )
                inherited = runtime_port_overrides_by_root.get(root, {}).get(role)
                target = inherited or explicit or configured
                if target is None:
                    continue
                runtime_port_overrides_by_root.setdefault(root, {})
                if target != configured:
                    runtime_port_overrides_by_root[root][role] = int(target)
                desired_ports.setdefault(int(target), []).append((
                    instance, role, bool(inherited or explicit),
                ))

        additionally_reserved = set(desired_ports)
        for collision_port, occurrences in sorted(desired_ports.items()):
            if len(occurrences) < 2:
                continue
            # Preserve the already-running owner where one exists.  That is
            # evidence about what must remain safe, not permission to change
            # any sibling; every other target still requires the user's
            # automatic-conflict policy or an explicit destination.
            keeper = next((
                item for item in occurrences
                if item[1] in set(item[0].roles)
                and item[0].configured_ports.get(item[1] + "_port")
                == collision_port
            ), occurrences[0])
            for instance, role, explicit in occurrences:
                if instance is keeper[0] and role == keeper[1]:
                    continue
                root = str(instance.project_root)
                if explicit:
                    return {
                        "status": "blocked",
                        "reason": "explicit_target_port_collides=%s:%s:%s"
                        % (root, role, collision_port),
                        "missing_decisions": [
                            "用户指定给 %s %s 的端口 %s 与同一计划中的其他角色冲突；"
                            "请指定另一个端口，或授权对所有端口冲突自动选择空闲端口"
                            % (root, role, collision_port)
                        ],
                    }
                if not _automatic_conflict_port_policy(decision_text):
                    return {
                        "status": "blocked",
                        "reason": "plan_wide_port_collision=%s" % collision_port,
                        "missing_decisions": [
                            "端口 %s 被多个目标角色共同使用。是否保留当前运行者，"
                            "并为其余目标自动选择经检查的空闲端口？" % collision_port
                        ],
                    }
                candidates_for_role = _runtime_port_candidates(
                    collision_port,
                    inventory,
                    additionally_reserved=additionally_reserved,
                )
                replacement = _first_checked_free_port(
                    bundle, candidates_for_role,
                )
                if replacement is None:
                    return {
                        "status": "need_evidence",
                        "reason": "plan_wide_checked_free_port_required=%s:%s"
                        % (root, role),
                        "probe_requests": [{
                            "probe": "ports",
                            "args": {"ports": candidates_for_role},
                            "purpose": (
                                "端口 %s 在整份计划内冲突；为 %s 的 %s "
                                "选择经检查未监听的唯一目标端口"
                                % (collision_port, root, role)
                            ),
                            "required_facts": [
                                "bounded candidate port listener states",
                                "one unique checked-free destination port",
                            ],
                            "freshness": "refresh",
                            "gap_id": "gap-plan-wide-%s-%s-port" % (
                                re.sub(r"[^A-Za-z0-9_.:-]+", "-", root), role,
                            ),
                            "affected_steps": [],
                        }],
                    }
                runtime_port_overrides_by_root.setdefault(root, {})[role] = replacement
                additionally_reserved.add(replacement)
        frozen_identifiers = {
            str(key).rstrip("/"): str(value)
            for key, value in dict(
                (intent_context or {}).get(
                    "recovery_instance_identifiers_by_root", {}
                ) or {}
            ).items()
            if str(key).startswith("/") and str(value).strip()
        }
        for index, instance in enumerate(candidates, start=1):
            stable_alias = frozen_identifiers.get(
                str(instance.project_root).rstrip("/"), instance.platform,
            )
            slug = re.sub(
                r"[^a-z0-9]+", "-", str(stable_alias or "").lower()
            ).strip("-") or "instance-%d" % index
            if slug in used_slugs:
                slug = "%s-%d" % (slug, index)
            used_slugs.add(slug)
            old_step_id = "restart-backend-roles"
            new_step_id = "restart-%s-backend-roles" % slug
            child_context = dict(intent_context or {})
            child_context.update({
                "operation": "restart",
                "scope": "platform",
                "resolved_project_root": instance.project_root,
                "all_platform_expansion_child": True,
                "runtime_port_overrides_by_root": runtime_port_overrides_by_root,
            })
            child = ChangePlannerAgent._deterministic_runtime_restart(
                goal,
                bundle,
                intent_context=child_context,
                candidate_plan=candidate_plan,
            )
            if child is None:
                return {
                    "status": "blocked",
                    "reason": "runtime_restart_contract_unresolved=%s"
                    % instance.project_root,
                    "missing_decisions": [
                        "实例 %s 缺少形成 Screen 重启计划所需的运行身份或端口证据"
                        % instance.project_root,
                    ],
                }
            if child.get("status") == "achieved":
                continue
            if child.get("status") != "ready":
                reason = str(child.get("reason") or "runtime contract unresolved")
                child["reason"] = "%s; project_root=%s" % (
                    reason, instance.project_root,
                )
                for request in child.get("probe_requests") or []:
                    if not isinstance(request, dict):
                        continue
                    request["affected_steps"] = [
                        new_step_id if str(item) == old_step_id else str(item)
                        for item in request.get("affected_steps") or []
                    ]
                return child
            child_resources = ChangePlannerAgent._compact_runtime_identity_resources(
                child.get("resources") or [], old_step_id,
            )
            for raw_resource in child_resources:
                resource = dict(raw_resource)
                resource["name"] = _plan_resource_name(
                    slug, resource.get("name") or "resource",
                )
                resource["consumers"] = [
                    "%s%s" % (new_step_id, str(consumer)[len(old_step_id):])
                    if str(consumer).startswith(old_step_id)
                    else str(consumer)
                    for consumer in resource.get("consumers") or []
                ]
                resources.append(resource)
            for raw_change in child.get("changes") or []:
                change = dict(raw_change)
                change["step_id"] = new_step_id
                change["depends_on"] = [
                    new_step_id if item == old_step_id else item
                    for item in change.get("depends_on") or []
                ]
                changes.append(change)
        if not changes:
            return {
                "status": "achieved",
                "reason": (
                    "runtime inventory proves every requested application "
                    "component is already fully Screen-managed"
                ),
            }
        if len(changes) > 12 or len(resources) > 64:
            return {
                "status": "blocked",
                "reason": "all-platform restart exceeds one bounded plan",
                "missing_decisions": [
                    "目标包含 %d 个实例和 %d 个资源，请缩小单次审批范围"
                    % (len(changes), len(resources)),
                ],
            }
        return {
            "status": "ready",
            "goal": goal,
            "assumptions": [],
            "resources": resources,
            "changes": changes,
        }

    @staticmethod
    def _compact_runtime_identity_resources(
        raw_resources: list[dict[str, Any]],
        semantic_step_id: str,
    ) -> list[dict[str, Any]]:
        """Share identical runtime identity scalars inside one semantic step.

        Consumers remain role-qualified, so Binding can inject the common UID
        or interpreter only into the matching atomic component step.  This
        removes repeated facts without weakening per-role identity semantics.
        """

        result: list[dict[str, Any]] = []
        shared: dict[tuple[Any, ...], dict[str, Any]] = {}
        for raw in raw_resources:
            resource = dict(raw)
            match = re.fullmatch(
                r"(master|celery|web_terminal|worker)_(uid|python_executable)",
                str(resource.get("role") or ""),
            )
            if match is None:
                result.append(resource)
                continue
            component, identity_field = match.groups()
            arg_name = (
                "run_as_uid" if identity_field == "uid"
                else "python_executable"
            )
            consumer = "%s.%s_%s" % (
                semantic_step_id, component, arg_name,
            )
            key = (
                resource.get("kind"), resource.get("status"),
                resource.get("value"), resource.get("source"), arg_name,
            )
            existing = shared.get(key)
            if existing is None:
                resource["role"] = arg_name
                resource["consumers"] = [consumer]
                shared[key] = resource
                result.append(resource)
            else:
                existing["consumers"] = list(dict.fromkeys([
                    *existing.get("consumers", []), consumer,
                ]))
        return result

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
            "function": {"name": "submit_change_plan"},
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
        # The transport tool only selects the one accepted response channel.
        # The full ChangePlan contract is provider-independent and enforced by
        # _outcome(), whose bounded repair loop returns precise validation
        # errors.  Encoding that contract twice made Gemini compile an FST
        # larger than its provider-specific constraint limit.
        return {
            "type": "function",
            "function": {
                "name": "submit_change_plan",
                "description": (
                    "Submit one bounded semantic planning outcome. "
                    "need_evidence requires probe_requests; ready requires goal, "
                    "assumptions, resources, and changes; blocked requires reason."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["need_evidence", "ready", "blocked"],
                        },
                        "goal": {"type": "string"},
                        "reason": {"type": "string"},
                        "missing_decisions": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "probe_requests": {
                            "type": "array",
                            "items": {
                                "type": "object", "additionalProperties": True,
                            },
                        },
                        "assumptions": {
                            "type": "array", "items": {"type": "string"},
                        },
                        "resources": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "kind": {"type": "string"},
                                    "status": {"type": "string"},
                                },
                                "required": ["name", "kind", "status"],
                                "additionalProperties": True,
                            },
                        },
                        "changes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "step_id": {"type": "string"},
                                    "title": {"type": "string"},
                                    "objective": {"type": "string"},
                                },
                                "required": ["step_id", "title", "objective"],
                                "additionalProperties": True,
                            },
                        },
                    },
                    "required": ["status"],
                    "additionalProperties": True,
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
            if str(name or "") != "submit_change_plan":
                raise ValueError("unexpected planner function call")
            if isinstance(arguments, dict):
                data = arguments
            else:
                data = json.loads(str(arguments or ""))
            if not isinstance(data, dict):
                raise ValueError("planner function arguments must be an object")
            return data, json.dumps(data, ensure_ascii=False)

        # Compatibility for lightweight test doubles which predate tool calls.
        # The production request above always forces submit_change_plan.
        content = getattr(message, "content", None) or ""
        return parse_json_object(content), content

    def _outcome(
        self,
        data: dict[str, Any],
        goal: str,
        bundle: EvidenceBundle,
        *,
        intent_context: dict[str, Any] | None = None,
        candidate_plan: ChangePlan | None = None,
    ) -> GoalOutcome:
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
        if status == "achieved":
            return GoalOutcome(
                status="achieved",
                reason=str(data.get("reason") or "goal already satisfied"),
            )
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
                    "role_not_running for its project_root. Do not re-query the same "
                    "process absence. Screen ownership is a separate runtime fact and "
                    "must remain available to Binding when it is not already present."
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
            # ``candidate_plan`` is a predecessor supplied to preserve scope
            # and completed effects.  It is not a candidate produced by this
            # planning turn.  Returning it here used to make Coordinator call
            # finalize_candidate() after Discovery, bypassing the Planner that
            # requested the evidence and reviving stale resource decisions.
            # Only a partial plan compiled from *this* response may take the
            # evidence-only finalize path.
            partial_candidate = self._partial_candidate_plan(
                data, goal, bundle,
            )
            return GoalOutcome(
                status=status,
                evidence_requests=requests,
                candidate_plan=partial_candidate,
                replan_context={
                    "active_gap_ids": [item.need_key for item in requests],
                    "active_gap_affected_steps": sorted({
                        step
                        for item in requests
                        for step in item.affected_steps
                    }),
                },
            )
        if status == "blocked":
            missing = data.get("missing_decisions")
            missing_items = (
                [str(item) for item in missing]
                if isinstance(missing, list)
                else []
            )
            if any(
                _offloads_discoverable_implementation_detail(item)
                for item in missing_items
            ):
                raise ValueError(
                    "blocked cannot offload discoverable implementation details; "
                    "Discovery or Binding must resolve them"
                )
            return GoalOutcome(
                status=status,
                reason=str(data.get("reason") or "planning blocked"),
                missing_decisions=missing_items,
            )
        if status != "ready":
            raise ValueError("planner status must be need_evidence, ready, or blocked")
        self._merge_duplicate_resource_entries(data)
        self._normalize_instance_container_names(data)
        self._normalize_ungrounded_dependency_installs(data)
        self._normalize_semantic_dependencies(data)
        self._normalize_change_order(data)
        self._normalize_verification_changes(data)
        self._normalize_change_order(data)
        self._normalize_postcondition_args(data)
        self._normalize_port_resource_roles(data)
        self._normalize_runtime_stop_scope(data, goal, bundle)
        self._normalize_runtime_repair_coverage(
            data, bundle, intent_context=intent_context,
        )
        self._collapse_redundant_runtime_repair_changes(data)
        resources = [
            PlanResource.from_dict(item)
            for item in data.get("resources", [])
            if isinstance(item, dict)
        ]
        self._normalize_core_resource_consumers(data, resources)
        resources = self._normalize_missing_explicit_deployment_resources(
            data, resources, bundle, goal,
        )
        resources = self._normalize_derived_resources(data, resources)
        self._normalize_existing_config_paths(data, resources)
        self._normalize_resource_consumer_owners(data, resources)
        self._normalize_existing_runtime_ports(data, resources, bundle)
        self._normalize_occupied_host_ports(data, resources, bundle)
        self._normalize_backend_role_health_contracts(data, resources)
        self._normalize_nginx_postconditions(data, resources)
        contract_errors = self._ready_contract_errors(
            data,
            goal,
            resources,
            bundle,
        )
        contract_errors.extend(self._instance_root_contract_errors(data))
        contract_errors.extend(self._healthy_runtime_role_contract_errors(
            data, bundle, intent_context=intent_context,
        ))
        contract_errors.extend(self._recovery_scope_contract_errors(
            resources, intent_context=intent_context,
        ))
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
            candidate_plan = ChangePlan.new(
                goal=goal,
                risk=candidate_risk,
                steps=candidate_steps,
                resources=resources,
                assumptions=[str(item) for item in candidate_assumptions]
                if isinstance(candidate_assumptions, list)
                else [],
            )
            return GoalOutcome(
                status="need_evidence",
                candidate_plan=candidate_plan,
                evidence_requests=self._candidate_evidence_requests(
                    candidate_plan, bundle, unproven_ports
                ),
            )
        if contract_errors:
            raise ValueError("; ".join(contract_errors))
        steps = self._steps(data.get("changes"), bundle)
        risk = max(steps, key=lambda item: RISK_LEVELS.index(item.risk)).risk
        assumptions = data.get("assumptions")
        plan = ChangePlan.new(
            goal=goal,
            risk=risk,
            steps=steps,
            resources=resources,
            assumptions=[str(item) for item in assumptions]
            if isinstance(assumptions, list)
            else [],
        )
        active_gap_steps = {
            str(item)
            for item in (intent_context or {}).get(
                "active_gap_affected_steps", []
            )
            if str(item)
        }
        if (
            candidate_plan is not None
            and bool((intent_context or {}).get("recovery_scope_authoritative"))
        ):
            # A successor recovery plan intentionally omits already completed
            # predecessor effects.  They remain authoritative in the persisted
            # predecessor and must not be copied back into executable scope.
            active_gap_steps.update(
                str(item.step_id)
                for item in candidate_plan.steps
                if str(item.status or "pending") in {"completed", "skipped"}
            )
        self._validate_candidate_plan_preservation(
            candidate_plan,
            plan,
            allowed_steps=active_gap_steps,
        )
        if self._plan_needs_docker_images(plan) and not self._has_docker_images(bundle):
            return GoalOutcome(
                status="need_evidence",
                candidate_plan=plan,
                evidence_requests=[
                    ProbeRequest(
                        "docker_images",
                        {},
                        "select an already installed image for each new container",
                    )
                ],
            )
        return GoalOutcome(status="need_execution", plan=plan)

    @classmethod
    def _partial_candidate_plan(
        cls,
        data: dict[str, Any],
        goal: str,
        bundle: EvidenceBundle,
    ) -> ChangePlan | None:
        """Persist already-grounded semantics while evidence is collected."""

        if not isinstance(data.get("changes"), list) or not data.get("changes"):
            return None
        cls._merge_duplicate_resource_entries(data)
        steps = cls._steps(data.get("changes"), bundle)
        resources = [
            PlanResource.from_dict(item)
            for item in data.get("resources") or []
            if isinstance(item, dict)
        ]
        # Evidence collection must persist the same normalized resource
        # ownership that a ready outcome would use. Otherwise a candidate can
        # forget already-grounded container/config port relationships between
        # Planner rounds and ask Discovery for the same facts again.
        resources = cls._normalize_derived_resources(data, resources)
        risk = max(steps, key=lambda item: RISK_LEVELS.index(item.risk)).risk
        return ChangePlan.new(
            goal=goal,
            risk=risk,
            steps=steps,
            resources=resources,
            assumptions=[str(item) for item in data.get("assumptions") or []],
        )

    @staticmethod
    def _merge_duplicate_resource_entries(data: dict[str, Any]) -> None:
        """Make ChangePlan the sole resource manifest before Binding."""

        raw = data.get("resources")
        if not isinstance(raw, list):
            return
        merged: list[dict[str, Any]] = []
        by_name: dict[str, dict[str, Any]] = {}
        identity_fields = ("kind", "status", "role", "value")
        for item in raw:
            if not isinstance(item, dict):
                continue
            resource = dict(item)
            name = str(resource.get("name") or "")
            existing = by_name.get(name)
            if existing is None:
                resource["consumers"] = list(dict.fromkeys(
                    str(value) for value in resource.get("consumers") or []
                ))
                by_name[name] = resource
                merged.append(resource)
                continue
            if any(existing.get(field) != resource.get(field) for field in identity_fields):
                raise ValueError("conflicting duplicate plan resource name: %s" % name)
            existing["consumers"] = list(dict.fromkeys([
                *[str(value) for value in existing.get("consumers") or []],
                *[str(value) for value in resource.get("consumers") or []],
            ]))
        data["resources"] = merged

    @staticmethod
    def _recovery_scope_contract_errors(
        resources: list[PlanResource],
        *,
        intent_context: dict[str, Any] | None,
    ) -> list[str]:
        """Reject a Replan that silently drops an unfinished plan target."""

        context = intent_context or {}
        if not bool(context.get("recovery_scope_authoritative")):
            return []
        required = {
            str(item).rstrip("/")
            for item in context.get("recovery_required_project_roots") or []
            if str(item).startswith("/")
        }
        covered = {
            str(item.value or "").rstrip("/")
            for item in resources
            if str(item.role or "") == "instance_root"
        }
        missing = sorted(required - covered)
        unexpected = sorted(covered - required)
        errors: list[str] = []
        if missing:
            errors.append(
                "replan dropped authoritative recovery targets=%s"
                % ",".join(missing)
            )
        if unexpected:
            errors.append(
                "replan reintroduced completed recovery targets=%s"
                % ",".join(unexpected)
            )
        return errors

    @staticmethod
    def _validate_candidate_plan_preservation(
        candidate: ChangePlan | None,
        revised: ChangePlan,
        *,
        allowed_steps: set[str],
    ) -> None:
        if candidate is None:
            return
        previous = {item.step_id: item for item in candidate.steps}
        current = {item.step_id: item for item in revised.steps}

        def semantic(step: ChangeStep) -> dict[str, Any]:
            return {
                "step_id": step.step_id,
                "title": step.title,
                "objective": step.objective,
                "reason": step.reason,
                "depends_on": list(step.depends_on),
                "risk": step.risk,
                "expected_changes": list(step.expected_changes),
                "postconditions": list(step.postconditions),
            }

        changed = []
        for step_id, old in previous.items():
            if step_id in allowed_steps:
                continue
            new = current.get(step_id)
            if new is None or semantic(old) != semantic(new):
                changed.append(step_id)
        if changed:
            raise ValueError(
                "replan changed candidate steps outside active evidence gap: %s"
                % ",".join(sorted(changed))
            )

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

        ChangePlannerAgent._normalize_consumer_owners(resources)

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
                and ChangePlannerAgent._requires_host_port_availability(resource)
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
        for instance in RuntimeInventory.from_bundle(bundle).instances:
            occupied.update(instance.configured_ports.values())
        host_resources = [
            resource
            for resource in resources
            if ChangePlannerAgent._requires_host_port_availability(resource)
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
    def _normalize_existing_runtime_ports(
        data: dict[str, Any],
        resources: list[PlanResource],
        bundle: EvidenceBundle,
    ) -> None:
        """Bind current role ports through the frozen instance identity.

        Port ownership is a relation between Plan resources and RuntimeInventory;
        model prose is not an authority for deciding which instance owns a port.
        """

        changes = {
            str(item.get("step_id") or ""): item
            for item in data.get("changes", [])
            if isinstance(item, dict)
        }
        inventory = RuntimeInventory.from_bundle(bundle)
        instances = {item.project_root: item for item in inventory.instances}
        root_resources = [
            resource
            for resource in resources
            if resource.status == "frozen"
            and resource.kind == "path"
            and str(resource.role or "").lower()
            in {"instance_root", "target_root", "deployment_root"}
            and str(resource.value).rstrip("/") in instances
        ]

        def owners(resource: PlanResource) -> set[str]:
            return {
                str(consumer).partition(".")[0]
                for consumer in resource.consumers
            }

        def owned_root(resource: PlanResource) -> str:
            resource_owners = owners(resource)
            candidates = [
                str(root.value).rstrip("/")
                for root in root_resources
                if resource_owners.intersection(owners(root))
            ]
            if len(set(candidates)) == 1:
                return candidates[0]
            return ""

        replacements: dict[str, dict[int, int]] = {}
        for resource in resources:
            if (
                resource.status != "frozen"
                or resource.kind != "port"
            ):
                continue
            root = owned_root(resource)
            instance = instances.get(root)
            if instance is None:
                continue
            role = str(resource.role or "")
            if role not in instance.configured_ports:
                matched_roles = [
                    candidate_role
                    for candidate_role, current_port in instance.configured_ports.items()
                    if int(resource.value) == current_port
                ]
                if len(matched_roles) != 1:
                    continue
                role = matched_roles[0]
                resource.role = role
            expected = instance.configured_ports.get(role)
            if expected is None:
                continue
            old = int(resource.value)
            migration_text = " ".join(
                json.dumps(changes[owner], ensure_ascii=False)
                for owner in owners(resource)
                if owner in changes
            )
            explicit_port_migration = bool(re.search(
                r"\b(?:migrate|move|change|set|allocate)\w*\b"
                r"[^.!?。！？]{0,40}\bport\b|"
                r"(?:迁移|切换|修改|设置|分配)[^。！？]{0,30}端口",
                migration_text,
                re.I,
            ))
            if explicit_port_migration and old != expected:
                continue
            if old != expected:
                for owner in owners(resource):
                    if owner in changes:
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
                role_port = _frozen_step_role_port(
                    resources, step_id, role,
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
                role_candidate_ports = {
                    int(resource.value)
                    for resource in resources
                    if resource.status == "frozen"
                    and resource.kind == "port"
                    and resource.role == "%s_port" % role
                    and any(
                        str(consumer).partition(".")[0] == step_id
                        for consumer in resource.consumers
                    )
                    and str(resource.value).isdigit()
                }
                postconditions[:] = [
                    item for item in postconditions
                    if not (
                        isinstance(item, dict)
                        and item.get("checker") in {"http_status", "backend_health"}
                        and isinstance(item.get("args"), dict)
                        and any(
                            re.search(
                                r":%s(?:/|$)" % candidate_port,
                                str(item["args"].get("url") or ""),
                            )
                            for candidate_port in role_candidate_ports
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
    def _step_instance_roots(data: dict[str, Any]) -> dict[str, str]:
        """Resolve semantic-step ownership only from frozen root resources."""

        roots_by_step: dict[str, set[str]] = {}
        for resource in data.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            if str(resource.get("status") or "") != "frozen":
                continue
            if str(resource.get("kind") or "") != "path":
                continue
            if str(resource.get("role") or "") not in {
                "instance_root", "target_root", "deployment_root",
                "project_root", "platform_instance_root",
            }:
                continue
            value = str(resource.get("value") or "").rstrip("/") or "/"
            if not value.startswith("/"):
                continue
            for consumer in resource.get("consumers") or []:
                step_id = str(consumer).partition(".")[0]
                if step_id:
                    roots_by_step.setdefault(step_id, set()).add(value)
        return {
            step_id: next(iter(roots))
            for step_id, roots in roots_by_step.items()
            if len(roots) == 1
        }

    @staticmethod
    def _instance_root_contract_errors(data: dict[str, Any]) -> list[str]:
        roots_by_step: dict[str, set[str]] = {}
        for resource in data.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            if str(resource.get("status") or "") != "frozen":
                continue
            if str(resource.get("kind") or "") != "path":
                continue
            if str(resource.get("role") or "") != "instance_root":
                continue
            root = str(resource.get("value") or "").rstrip("/") or "/"
            for consumer in resource.get("consumers") or []:
                owner = str(consumer).partition(".")[0]
                if owner:
                    roots_by_step.setdefault(owner, set()).add(root)
        return [
            "change[%s] has multiple instance_root identities=%s"
            % (step_id, ",".join(sorted(roots)))
            for step_id, roots in sorted(roots_by_step.items())
            if len(roots) > 1
        ]

    @staticmethod
    def _normalize_runtime_repair_coverage(
        data: dict[str, Any],
        bundle: EvidenceBundle,
        *,
        intent_context: dict[str, Any] | None = None,
    ) -> None:
        """Compile unhealthy runtime roles into mandatory repair acceptance."""

        operation = str((intent_context or {}).get("operation") or "").lower()
        scope = str((intent_context or {}).get("scope") or "").lower()
        requested_components = {
            str(item).strip().lower().replace("-", "_")
            for item in (intent_context or {}).get("components") or []
            if str(item).strip()
        }
        completed_by_root = {
            str(root).rstrip("/"): {
                str(role).strip().lower().replace("-", "_")
                for role in roles or [] if str(role).strip()
            }
            for root, roles in dict(
                (intent_context or {}).get(
                    "recovery_completed_components_by_root", {}
                ) or {}
            ).items()
        }
        if operation == "restart" and scope == "component":
            # An explicit component restart is complete at that component's
            # boundary.  Inventory-wide health repair is a different goal.
            return

        unhealthy: dict[
            str,
            dict[str, tuple[int, str, list[int], dict[int, tuple[int, str]]]],
        ] = {}
        for instance in RuntimeInventory.from_bundle(bundle).abnormal:
                line = instance.raw_line
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
                    unhealthy[instance.project_root] = roles
        changes = data.get("changes")
        if not isinstance(changes, list):
            return
        roots_by_step = ChangePlannerAgent._step_instance_roots(data)
        for root, roles in unhealthy.items():
            roles = {
                role: value for role, value in roles.items()
                if role not in completed_by_root.get(root.rstrip("/"), set())
            }
            if not roles:
                continue
            if scope == "component" and requested_components:
                roles = {
                    role: value for role, value in roles.items()
                    if role in requested_components
                }
                if not roles:
                    continue
            candidates = [
                item for item in changes
                if isinstance(item, dict)
                and roots_by_step.get(str(item.get("step_id") or "")) == root
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
                planned_port = _frozen_step_role_port(
                    raw_resources, step_id, role,
                ) or observed_port
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
                # Evidence can refine acceptance but cannot create a second
                # transition.  If the goal-derived plan already starts or
                # restarts this role on the frozen destination, drop stale
                # health-repair decisions that still point at an observed old
                # port and retain the existing target transition.
                expected[:] = [
                    item for item in expected
                    if not (
                        re.search(
                            r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % role,
                            str(item), re.I,
                        )
                        and re.search(
                            r"\b(?:start|restart|restore|recover)\w*\b|"
                            r"启动|重启|恢复",
                            str(item), re.I,
                        )
                        and re.search(
                            r"\bat\s+%s\b|端口\s*%s\b"
                            % (observed_port, observed_port),
                            str(item), re.I,
                        )
                        and observed_port != planned_port
                    )
                ]
                target_transition_exists = any(
                    re.search(
                        r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % role,
                        str(item), re.I,
                    )
                    and re.search(
                        r"\b(?:start|restart|restore|recover)\w*\b|"
                        r"启动|重启|恢复",
                        str(item), re.I,
                    )
                    and re.search(
                        r"\bat\s+%s\b|端口\s*%s\b"
                        % (planned_port, planned_port),
                        str(item), re.I,
                    )
                    for item in expected
                )
                if not target_transition_exists:
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
                            ("identifier", run_as_uid, "run_as_uid"),
                            ("path", python_executable, "python_executable"),
                        )
                        for kind, value, arg_name in identity_resources:
                            qualified_consumer = "%s.%s_%s" % (
                                step_id, role, arg_name,
                            )
                            if any(
                                isinstance(item, dict)
                                and (
                                    (
                                        item.get("role") == arg_name
                                        and qualified_consumer
                                        in (item.get("consumers") or [])
                                    )
                                    or (
                                        item.get("role")
                                        == "%s_%s" % (
                                            role,
                                            "uid" if arg_name == "run_as_uid"
                                            else "python_executable",
                                        )
                                        and any(
                                            str(consumer).rsplit(".", 1)[0]
                                            == step_id
                                            for consumer in item.get("consumers") or []
                                        )
                                    )
                                )
                                for item in raw_resources
                            ):
                                continue
                            raw_resources.append({
                                "name": _plan_resource_name(
                                    step_id, role, arg_name,
                                ),
                                "kind": kind,
                                "status": "frozen",
                                "role": arg_name,
                                "value": value,
                                "source": "running_platforms",
                                "consumers": [qualified_consumer],
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
        roots_by_step = ChangePlannerAgent._step_instance_roots(data)
        owner_by_root: dict[str, dict[str, Any]] = {}
        replacements: dict[str, str] = {}
        kept = []
        for change in changes:
            if not isinstance(change, dict):
                kept.append(change)
                continue
            root = roots_by_step.get(str(change.get("step_id") or ""), "")
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
            root = roots_by_step.get(str(change.get("step_id") or ""), "")
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
    def _healthy_runtime_role_contract_errors(
        data: dict[str, Any],
        bundle: EvidenceBundle,
        *,
        intent_context: dict[str, Any] | None = None,
    ) -> list[str]:
        """Reject out-of-scope healthy-role mutation without rewriting it."""

        operation = str((intent_context or {}).get("operation") or "").lower()
        scope = str((intent_context or {}).get("scope") or "").lower()
        requested = {
            str(item).strip().lower().replace("-", "_")
            for item in (intent_context or {}).get("components") or []
            if str(item).strip()
        }
        if operation == "restart":
            return []

        states: dict[str, dict[str, tuple[str, int]]] = {}
        for instance in RuntimeInventory.from_bundle(bundle).instances:
            for role in ("master", "worker"):
                port = instance.configured_ports.get(role + "_port")
                endpoint = instance.endpoints.get(role)
                if endpoint is not None and port is not None:
                    states.setdefault(instance.project_root, {})[role] = (
                        endpoint, port,
                    )
        changes = data.get("changes")
        if not isinstance(changes, list):
            return []
        roots_by_step = ChangePlannerAgent._step_instance_roots(data)
        mutation_pattern = re.compile(
            r"\b(?:start|restart|restore|recover|launch)\w*\b|启动|重启|恢复",
            re.I,
        )
        errors: list[str] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            serialized = json.dumps(change, ensure_ascii=False)
            step_id = str(change.get("step_id") or "")
            root = roots_by_step.get(step_id, "")
            if not root or root not in states:
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
            if scope == "component" and requested:
                healthy_mutations.difference_update(requested)
            for role in sorted(healthy_mutations):
                errors.append(
                    "change[%s] mutates healthy out-of-scope role=%s at project_root=%s"
                    % (step_id or "unknown", role, root)
                )
        return errors

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
        roots_by_step: dict[str, str] = {}
        for root_resource in roots:
            normalized = str(root_resource.value).rstrip("/") or "/"
            for consumer in root_resource.consumers:
                owner = str(consumer).partition(".")[0]
                if owner:
                    roots_by_step[owner] = normalized
        for root_resource in roots:
            root = Path(str(root_resource.value))
            candidates = (
                root / "vemu_config" / "config.py",
                root / "vemu_uestc" / "vemu_config" / "config.py",
            )
            config = next((path for path in candidates if path.is_file()), None)
            if config is None:
                continue
            normalized_root = str(root).rstrip("/") or "/"
            owners = {
                step_id for step_id in changes
                if roots_by_step.get(step_id) == normalized_root
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
            inventory = RuntimeInventory.from_bundle(bundle)
            target_roots = {
                item.project_root for item in inventory.matching(goal)
            }
            for instance in inventory.instances:
                port = instance.configured_ports.get("worker_port")
                if port is not None and instance.project_root in target_roots and re.search(
                    r"(?<!\d)%s(?!\d)" % port, goal,
                ):
                    authoritative.append((instance.project_root, port))
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
                inventory = RuntimeInventory.from_bundle(bundle)
                target_instance = next(
                    (
                        instance
                        for instance in inventory.instances
                        if instance.project_root == root
                        and instance.configured_ports.get("worker_port") == port
                    ),
                    None,
                )
                binding = (
                    target_instance.role_binding("worker")
                    if target_instance is not None
                    else None
                )
                if (
                    binding is not None
                    and binding.status == "confirmed"
                    and binding.configured_port == port
                ):
                    owner_pid = binding.listener_pid
                for record in bundle.records:
                    if owner_pid is not None:
                        break
                    if record.status != "available" or record.request.probe != "port_owner":
                        continue
                    for line in record.output.splitlines():
                        cwd_match = re.search(r"\bcwd=([^\s]+)", line)
                        owner_cwd = (
                            str(cwd_match.group(1)).rstrip("/")
                            if cwd_match is not None
                            else ""
                        )
                        if not (
                            re.search(r"\bport=%s\b" % port, line)
                            and "worker_main:flask_app" in line
                            and owner_cwd in {root, root + "/mains"}
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
        candidate: ChangePlan,
        bundle: EvidenceBundle,
    ) -> GoalOutcome:
        unproven = ChangePlannerAgent._unproven_port_resources(
            candidate.resources,
            bundle,
        )
        if unproven:
            return GoalOutcome(
                status="need_evidence",
                candidate_plan=candidate,
                evidence_requests=ChangePlannerAgent._candidate_evidence_requests(
                    candidate, bundle, unproven
                ),
            )
        occupied = []
        for resource in candidate.resources:
            if not ChangePlannerAgent._requires_host_port_availability(resource):
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
            affected_steps = sorted({
                str(consumer).split(".", 1)[0]
                for resource in candidate.resources
                if ChangePlannerAgent._requires_host_port_availability(resource)
                and int(resource.value) in occupied
                for consumer in resource.consumers
                if str(consumer).split(".", 1)[0]
            })
            return GoalOutcome(
                status="blocked",
                candidate_plan=candidate,
                reason="candidate ports became occupied: %s"
                % ",".join(str(item) for item in occupied),
                replan_context={
                    "active_gap_affected_steps": affected_steps,
                },
            )
        if (
            ChangePlannerAgent._plan_needs_docker_images(candidate)
            and not ChangePlannerAgent._has_docker_images(bundle)
        ):
            return GoalOutcome(
                status="need_evidence",
                candidate_plan=candidate,
                evidence_requests=[
                    ProbeRequest(
                        "docker_images",
                        {},
                        "select an already installed image for each new container",
                    )
                ],
            )
        return GoalOutcome(status="need_execution", plan=candidate)

    @staticmethod
    def _plan_needs_docker_images(plan: ChangePlan) -> bool:
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
        candidate: ChangePlan,
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
            ChangePlannerAgent._plan_needs_docker_images(candidate)
            and not ChangePlannerAgent._has_docker_images(bundle)
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
    def _explicit_goal_boundary_errors(
        goal_text: str,
        changes: list[dict[str, Any]] | Any,
    ) -> list[str]:
        """Enforce explicit include/exclude boundaries deterministically.

        Provider prompts guide plan quality, but a negative scope decision such
        as "no Nginx" and named required effects must remain hard contracts.
        Only semantic change text is inspected so discovered Git metadata does
        not look like a requested clone operation.
        """

        semantic_changes = [
            item for item in (changes or []) if isinstance(item, dict)
        ] if isinstance(changes, list) else []
        payload = json.dumps(
            [
                {
                    "title": item.get("title", ""),
                    "objective": item.get("objective", ""),
                    "expected_changes": item.get("expected_changes", []),
                }
                for item in semantic_changes
            ],
            ensure_ascii=False,
        ).lower()
        goal = str(goal_text or "")
        errors: list[str] = []

        def forbidden(pattern: str) -> bool:
            return bool(
                re.search(
                    r"(?:不得|严禁|禁止|不要|不应|不准|暂不|无需|"
                    r"\bno\b|\bwithout\b|\bdo not\b|\bmust not\b)"
                    r".{0,28}(?:%s)" % pattern,
                    goal,
                    re.I,
                )
            )

        sync_required = "sync_directory" in goal or bool(re.search(
            r"(?:当前|current).{0,16}(?:工作树|working tree).{0,24}(?:复制|copy|sync)",
            goal,
            re.I,
        ))
        clone_forbidden = forbidden(r"git\s*clone|clone|git_operation")

        def positively_mentions(pattern: str) -> bool:
            for clause in re.split(r"[\n.!?。！？;；]", payload):
                for match in re.finditer(pattern, clause, re.I):
                    prefix = clause[:match.start()]
                    if not re.search(
                        r"(?:\bno\b|\bwithout\b|\bdo\s+not\b|\bmust\s+not\b|"
                        r"不得|严禁|禁止|不要|不应|不准|暂不|无需)"
                        r"[^\n.!?。！？;；]{0,32}$",
                        prefix,
                        re.I,
                    ):
                        return True
            return False

        if sync_required and "sync_directory" not in payload:
            errors.append("explicit goal requires sync_directory")
        if clone_forbidden and positively_mentions(r"\bgit_operation\b|\bclone\b|克隆"):
            errors.append("explicit goal forbids Git clone")
        if forbidden(r"nginx") and positively_mentions(r"nginx"):
            errors.append("explicit goal forbids Nginx")
        if forbidden(r"data[_ -]?server") and positively_mentions(r"data[_ -]?server"):
            errors.append("explicit goal forbids data_server")

        required_containers = set(re.findall(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.-]*-"
            r"(?:mysql|redis|rabbitmq))(?![A-Za-z0-9_.-])",
            goal,
            re.I,
        ))
        missing_containers = sorted(
            item for item in required_containers if item.lower() not in payload
        )
        if missing_containers:
            errors.append(
                "explicit goal missing containers=%s"
                % ",".join(missing_containers)
            )

        required_sessions = set(re.findall(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.-]*_"
            r"(?:m|c|web|w))(?![A-Za-z0-9_.-])",
            goal,
            re.I,
        ))
        missing_sessions = sorted(
            item for item in required_sessions if item.lower() not in payload
        )
        if missing_sessions:
            errors.append(
                "explicit goal missing Screen sessions=%s"
                % ",".join(missing_sessions)
            )
        return errors

    @staticmethod
    def _explicit_goal_paths(goal_text: str) -> set[str]:
        return {
            match.rstrip(".,;:，。；：")
            for match in re.findall(
                r"(?<![A-Za-z0-9_.-])/[A-Za-z0-9_./-]+",
                str(goal_text or ""),
            )
        }

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
                    errors.append("ChangeStep cannot be readonly")
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
        errors.extend(
            ChangePlannerAgent._explicit_goal_boundary_errors(
                goal_text,
                changes,
            )
        )
        deployment = _requests_new_platform_deployment(goal_text)
        if not deployment:
            return errors
        frozen = [item for item in resources if item.status == "frozen"]
        roles = {str(item.role or "").lower(): item for item in frozen}
        sync_deployment = "sync_directory" in goal_text or bool(re.search(
            r"(?:当前|current).{0,16}(?:工作树|working tree).{0,24}(?:复制|copy|sync)",
            goal_text,
            re.I,
        ))
        required_roles = {
            "instance_root": any(
                role in {"instance_root", "target_root", "deployment_root"}
                for role in roles
            ),
            (
                "source_directory" if sync_deployment else "source_remote"
            ): any(
                (
                    "source" in role
                    and ("directory" in role or "path" in role or "root" in role)
                )
                if sync_deployment
                else (
                    "source" in role and ("remote" in role or "url" in role)
                )
                for role in roles
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
                ChangePlannerAgent._requires_host_port_availability(item)
                for item in frozen
            ),
        }
        if not sync_deployment:
            required_roles["source_branch"] = any(
                "source" in role and "branch" in role for role in roles
            )
        missing_roles = [name for name, present in required_roles.items() if not present]
        if missing_roles:
            errors.append("missing frozen resources=%s" % ",".join(missing_roles))
        explicit_paths = ChangePlannerAgent._explicit_goal_paths(original_goal)
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
                used_ports = ChangePlannerAgent._used_ports_by_step(data).get(
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
        root_consumer_suffixes = (
            (".target_path", ".destination", ".project_root")
            if sync_deployment
            else (".repository",)
        )
        if root_resource is not None and not any(
            consumer.endswith(root_consumer_suffixes)
            for consumer in root_resource.consumers
        ):
            errors.append(
                "instance_root requires a sync target consumer"
                if sync_deployment
                else "instance_root requires a .repository consumer"
            )
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
        for port_resource in ChangePlannerAgent._unproven_port_resources(
            frozen,
            bundle,
        ):
            errors.append(
                "port resource lacks availability evidence=%s" % port_resource.name
            )
        for port_resource in (
            item
            for item in frozen
            if ChangePlannerAgent._requires_host_port_availability(item)
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
        isolation_payload = ChangePlannerAgent._strip_negated_reuse_claims(
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
                if ChangePlannerAgent._requires_host_port_availability(item)
            }
            used_ports_by_step = (
                ChangePlannerAgent._declared_listening_ports_by_step(data)
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
            ChangePlannerAgent._complete_klonet_contract_errors(data, resources)
        )
        return errors

    @staticmethod
    def _complete_klonet_contract_errors(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> list[str]:
        """Keep a requested complete platform from degrading into a partial start."""

        goal = str(data.get("goal") or "")
        complete_platform = bool(re.search(
            r"(?:\bcomplete\b|\bfull\b|fully\s+operational|完整|全量)"
            r"[^\n。！？.]{0,20}(?:klonet|platform|instance|平台|实例)",
            goal,
            re.I,
        ))
        if not complete_platform:
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
        nginx_forbidden = bool(re.search(
            r"(?:不得|严禁|禁止|不要|不应|不准|暂不|无需|"
            r"\bno\b|\bwithout\b|\bdo not\b|\bmust not\b)"
            r"[^\n。！？.]{0,28}nginx",
            goal,
            re.I,
        ))
        if master_port is not None and not nginx_forbidden and (
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
    def _normalize_missing_explicit_deployment_resources(
        data: dict[str, Any],
        resources: list[PlanResource],
        bundle: EvidenceBundle,
        goal_text: str,
    ) -> list[PlanResource]:
        """Freeze omitted deployment allocations from explicit checked evidence.

        The model still owns semantic decomposition.  This normalization only
        fills values that the user explicitly delegated to automatic selection
        and that Discovery has already proved available or installed.
        """

        normalized = list(resources)
        if not (
            _requests_new_platform_deployment(goal_text)
            and _automatic_conflict_port_policy(goal_text)
        ):
            return normalized

        changes = [
            item for item in data.get("changes", []) if isinstance(item, dict)
        ]

        def change_text(change: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "title": change.get("title", ""),
                    "objective": change.get("objective", ""),
                    "expected_changes": change.get("expected_changes", []),
                },
                ensure_ascii=False,
            ).lower()

        config_change = next((
            item for item in changes
            if re.search(r"wtxconfig|config|配置", change_text(item), re.I)
            and re.search(r"master_port|worker_port|mysql_port", change_text(item), re.I)
        ), None)
        container_change = next((
            item for item in changes
            if re.search(r"container|容器", change_text(item), re.I)
            and all(service in change_text(item) for service in ("mysql", "redis", "rabbitmq"))
        ), None)
        screen_change = next((
            item for item in changes
            if re.search(r"screen|会话", change_text(item), re.I)
            and re.search(r"start|launch|启动", change_text(item), re.I)
        ), None)
        if config_change is None:
            return normalized

        requested_roles = [
            role for role in (
                "master_port", "worker_port", "web_terminal_port", "public_port",
                "mysql_port", "redis_port", "rabbitmq_port",
            )
            if role in str(goal_text).lower()
        ]
        existing_roles = {
            str(item.role or item.name).lower()
            for item in normalized
            if item.status == "frozen" and item.kind == "port"
        }
        missing_roles = [role for role in requested_roles if role not in existing_roles]

        candidates: list[int] = []
        occupied: set[int] = set()
        for record in bundle.records:
            if record.status != "available" or record.request.probe != "ports":
                continue
            requested = record.request.args.get("ports", [])
            if not isinstance(requested, list):
                continue
            for raw_port in requested:
                try:
                    port = int(raw_port)
                except (TypeError, ValueError):
                    continue
                if not 1 <= port <= 65535:
                    continue
                if port not in candidates:
                    candidates.append(port)
                if re.search(r":%s\b" % port, record.output):
                    occupied.add(port)
        occupied.update(
            instance_port
            for instance in RuntimeInventory.from_bundle(bundle).instances
            for instance_port in instance.configured_ports.values()
        )
        reserved = {
            int(item.value)
            for item in normalized
            if ChangePlannerAgent._requires_host_port_availability(item)
        }
        free = [
            port for port in candidates
            if port not in occupied and port not in reserved
        ]

        config_id = str(config_change.get("step_id") or "")
        container_id = str((container_change or {}).get("step_id") or "")
        screen_id = str((screen_change or {}).get("step_id") or "")
        selected: dict[str, int] = {}
        if len(free) >= len(missing_roles):
            selected = dict(zip(missing_roles, free))
            for role, port in selected.items():
                consumers = ["%s.%s" % (config_id, role)]
                service = role.removesuffix("_port")
                if service in {"mysql", "redis", "rabbitmq"} and container_id:
                    consumers.append("%s.%s_host_port" % (container_id, service))
                if service in {"master", "worker", "web_terminal"} and screen_id:
                    consumers.append("%s.%s" % (screen_id, role))
                normalized.append(PlanResource(
                    name=role,
                    kind="port",
                    status="frozen",
                    role=role,
                    value=port,
                    source="compiler_selected_from_checked_free_candidates",
                    consumers=consumers,
                ))

        if selected:
            assignments = ", ".join(
                "%s=%s" % (role, selected[role])
                for role in requested_roles if role in selected
            )
            config_change["objective"] = "%s; frozen allocations: %s" % (
                str(config_change.get("objective") or "").rstrip("; "),
                assignments,
            )
            config_change.setdefault("expected_changes", []).append(
                "Frozen WtxConfig allocations: %s" % assignments
            )
            if container_change is not None and all(
                role in selected for role in ("mysql_port", "redis_port", "rabbitmq_port")
            ):
                mappings = "%s->3306, %s->6379, %s->5672" % (
                    selected["mysql_port"], selected["redis_port"],
                    selected["rabbitmq_port"],
                )
                container_change["objective"] = "%s; frozen port mappings: %s" % (
                    str(container_change.get("objective") or "").rstrip("; "),
                    mappings,
                )
                container_change.setdefault("expected_changes", []).append(
                    "Frozen container port mappings: %s" % mappings
                )
            if screen_change is not None:
                runtime_assignments = ", ".join(
                    "%s=%s" % (role, selected[role])
                    for role in ("master_port", "worker_port", "web_terminal_port")
                    if role in selected
                )
                if runtime_assignments:
                    screen_change["objective"] = "%s; frozen runtime ports: %s" % (
                        str(screen_change.get("objective") or "").rstrip("; "),
                        runtime_assignments,
                    )

        if container_change is not None:
            container_payload = change_text(container_change)
            existing_image_consumers = {
                consumer
                for item in normalized
                if item.status == "frozen" and item.role == "docker_image"
                for consumer in item.consumers
            }
            image_output = "\n".join(
                record.output
                for record in bundle.records
                if record.status == "available"
                and record.request.probe == "docker_images"
            )
            for service, allowed_tags in (
                ("mysql", ("latest",)),
                ("redis", ("7", "latest")),
                ("rabbitmq", ("latest",)),
            ):
                consumer = "%s.%s_image" % (container_id, service)
                if service not in container_payload or consumer in existing_image_consumers:
                    continue
                selected_image = next((
                    "%s:%s" % (service, tag)
                    for tag in allowed_tags
                    if re.search(
                        r"(?:\b%s\s+%s\b|\b%s:%s\b)"
                        % (re.escape(service), re.escape(tag), re.escape(service), re.escape(tag)),
                        image_output,
                        re.I,
                    )
                ), None)
                if selected_image is None:
                    continue
                normalized.append(PlanResource(
                    name="%s_image" % service,
                    kind="identifier",
                    status="frozen",
                    role="docker_image",
                    value=selected_image,
                    source="observed_docker_images",
                    consumers=[consumer],
                ))

        return normalized

    @staticmethod
    def _normalize_derived_resources(
        data: dict[str, Any],
        resources: list[PlanResource],
    ) -> list[PlanResource]:
        normalized = list(resources)
        by_port = {
            int(item.value): item
            for item in normalized
            if ChangePlannerAgent._requires_host_port_availability(item)
        }
        explicit_internal_ports = {
            int(item.value)
            for item in normalized
            if item.status == "frozen"
            and item.kind == "port"
            and not ChangePlannerAgent._requires_host_port_availability(item)
        }
        changes = data.get("changes")
        if isinstance(changes, list):
            config_change = next((
                change for change in changes
                if isinstance(change, dict)
                and re.search(
                    r"wtxconfig|config|配置",
                    "%s %s" % (
                        change.get("title", ""), change.get("objective", ""),
                    ),
                    re.I,
                )
                and re.search(
                    r"master_port|worker_port|web_terminal_port|"
                    r"mysql_port|redis_port|rabbitmq_port",
                    "%s %s" % (
                        change.get("title", ""), change.get("objective", ""),
                    ),
                    re.I,
                )
            ), None)
            config_id = str((config_change or {}).get("step_id") or "")
            derived_service_ports: dict[str, int] = {}
            for change in changes:
                if not config_id:
                    break
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
                for service in ("mysql", "redis", "rabbitmq"):
                    match = re.search(
                        r"\b%s\b[^)]{0,120}?host[_ ]?port\s*(?:=|:)?\s*([1-9]\d{1,4})"
                        % service,
                        text,
                        re.I,
                    )
                    if match is None:
                        continue
                    port = int(match.group(1))
                    if not 1 <= port <= 65535:
                        continue
                    container_consumer = "%s.%s_host_port" % (step_id, service)
                    config_consumer = "%s.%s_port" % (config_id, service)
                    existing = next((
                        item for item in normalized
                        if item.status == "frozen"
                        and item.kind == "port"
                        and int(item.value) == port
                        and service in "%s %s" % (item.name, item.role)
                    ), None)
                    if existing is None:
                        consumers = [container_consumer]
                        if config_id:
                            consumers.append(config_consumer)
                        existing = PlanResource(
                            name="%s_port" % service,
                            kind="port",
                            status="frozen",
                            role="%s_port" % service,
                            value=port,
                            source="derived_from_container_host_port_contract",
                            consumers=consumers,
                        )
                        normalized.append(existing)
                        by_port[port] = existing
                    else:
                        consumers = [container_consumer]
                        if config_id:
                            consumers.append(config_consumer)
                        for consumer in consumers:
                            if consumer and consumer not in existing.consumers:
                                existing.consumers.append(consumer)
                    derived_service_ports[service] = port
            if config_change is not None and derived_service_ports:
                assignments = ", ".join(
                    "%s_port=%s" % (service, derived_service_ports[service])
                    for service in ("mysql", "redis", "rabbitmq")
                    if service in derived_service_ports
                )
                objective = str(config_change.get("objective") or "")
                missing_assignments = [
                    item for item in assignments.split(", ") if item not in objective
                ]
                if missing_assignments:
                    config_change["objective"] = "%s; frozen service ports: %s" % (
                        objective.rstrip("; "), ", ".join(missing_assignments),
                    )
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
                            and not ChangePlannerAgent._requires_host_port_availability(item)
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
        declared_listeners = ChangePlannerAgent._declared_listening_ports_by_step(data)
        for step_id, ports in ChangePlannerAgent._used_ports_by_step(data).items():
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
            return ChangePlannerAgent._normalize_consumer_owners(normalized)
        changes = data.get("changes")
        if not isinstance(changes, list):
            return ChangePlannerAgent._normalize_consumer_owners(normalized)

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
            screen_step_ids = [
                str(change.get("step_id") or "")
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
            screen_consumers = [
                "%s.source_root" % step_id for step_id in screen_step_ids
            ]
            if not screen_consumers:
                continue
            screen_owners = set(screen_step_ids)
            root.consumers = [
                consumer
                for consumer in root.consumers
                if str(consumer).partition(".")[0] not in screen_owners
            ]
            root.consumers.extend(
                "%s.project_root" % step_id for step_id in screen_step_ids
            )
            root.consumers = list(dict.fromkeys(root.consumers))
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
        return ChangePlannerAgent._normalize_consumer_owners(normalized)

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
                    ChangePlannerAgent._consumer_owner_score(owner, field),
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
            if not ChangePlannerAgent._requires_host_port_availability(resource):
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
    def _steps(value: Any, bundle: EvidenceBundle) -> list[ChangeStep]:
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
            step = ChangeStep(
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
                    tuple(
                        str(value).strip()
                        for value in item.get("required_facts") or []
                        if str(value).strip()
                    ),
                    str(item.get("freshness") or "cached"),
                    str(item.get("gap_id") or ""),
                    tuple(
                        str(value).strip()
                        for value in item.get("affected_steps") or []
                        if str(value).strip()
                    ),
                )
            )
        return requests

    @staticmethod
    def _evidence_json(bundle: EvidenceBundle) -> str:
        latest: dict[str, Any] = {}
        for item in bundle.records:
            latest[item.request.cache_key] = item
        records = list(latest.values())
        per_record_limit = max(
            800,
            min(3000, 28000 // max(1, len(records))),
        )

        def compact(output: str) -> str:
            value = str(output or "")
            if len(value) <= per_record_limit:
                return value
            marker = "\n...[evidence compacted for Planner transport]...\n"
            half = max(1, (per_record_limit - len(marker)) // 2)
            return value[:half] + marker + value[-half:]

        return json.dumps(
            [
                {
                    "evidence_id": item.evidence_id,
                    "probe": item.request.probe,
                    "required_facts": list(item.request.required_facts),
                    "freshness": item.request.freshness,
                    "gap_id": item.request.need_key,
                    "affected_steps": list(item.request.affected_steps),
                    "status": item.status,
                    "output": compact(item.output),
                }
                for item in records
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
                "resolved_gaps": conclusion.resolved_gaps,
                "unresolved_gaps": conclusion.unresolved_gaps,
            },
            ensure_ascii=False,
        )
