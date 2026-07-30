"""Agentic semantic Planner for Ops-Privilege."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from klonet_agent.ops.privileged.context import GroundedPlanContext
from klonet_agent.ops.privileged.contracts import (
    PrivilegedPlan,
    PrivilegedStep,
    RISK_LEVELS,
)
from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES


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

When ready, return:
{
 "status":"ready",
 "goal":"the actual user goal",
 "assumptions":[],
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
    ) -> None:
        self.llm = llm
        # Retained only as constructor compatibility. The semantic Planner
        # deliberately cannot inspect execution implementation names.
        del policy, action_registry
        self.probe_runner = probe_runner

    def plan(
        self,
        goal: str,
        *,
        environment_context: str = "",
        grounded_context: GroundedPlanContext | None = None,
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
                    "Goal:\n%s\n\nGrounded planning context:\n%s"
                    % (goal, planning_context)
                ),
            },
        ]
        data = None
        last_error = ""
        probe_history: list[dict[str, Any]] = []
        probe_rounds = 0
        invalid_repairs = 0
        while True:
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
                    if self.probe_runner is None:
                        raise ValueError(
                            "planner requested evidence but probe runner is unavailable"
                        )
                    evidence = self.probe_runner(requests)
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
                break
            except (KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
                if invalid_repairs >= 1:
                    raise ValueError(
                        "Planner did not return a valid semantic plan: %s"
                        % last_error
                    )
                invalid_repairs += 1
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
            risk = str(
                item.get("risk_suggestion") or "medium"
            ).strip().lower()
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
    if action == "manage_process":
        if str(args.get("signal") or "").upper() in {"TERM", "KILL", "INT"}:
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _action_risk(value: str) -> str:
    return {
        "normal": "readonly",
        "controlled": "medium",
        "privileged": "medium",
        "dangerous": "high",
    }.get(str(value or "").lower(), "medium")


def _highest_risk(steps: list[PrivilegedStep]) -> str:
    if not steps:
        return "readonly"
    return max(steps, key=lambda item: RISK_LEVELS.index(item.risk)).risk


def _effective_declared_risk(
    deterministic_floor: str,
    declared: Any,
) -> str:
    """Allow the Planner to raise risk, never lower the action-policy floor."""

    normalized = str(declared or "").strip().lower()
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
        "start_platform_screens",
        "restart_screen_component",
    }
    if action in project_actions:
        root = Path(str(args.get("project_root") or "")).expanduser()
        if not root.is_dir():
            return "grounding_failed=project_root_not_found"
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
        source = Path(str(args.get("source_path") or "")).expanduser()
        if not source.is_file():
            return "grounding_failed=nginx_source_not_file"
    if action == "extract_archive":
        archive = Path(str(args.get("archive_path") or "")).expanduser()
        if not archive.is_file():
            return "grounding_failed=archive_not_found"
    return ""


def _validate_action_semantics(action: str, args: dict[str, Any]) -> str:
    """Reject incomplete operation variants before they become confirmable plans."""

    operation = str(args.get("operation") or "").strip()
    allowed_operations = {
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
        "start_platform_screens",
        "restart_screen_component",
    }
    prepared_roots: set[str] = set()
    for step in steps:
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
            raise ValueError(
                "grounding_failed=project_root_not_in_environment_model"
            )
        if layout.get("readiness") == "invalid":
            raise ValueError(
                "grounding_failed=invalid_project_layout:%s"
                % ",".join(layout.get("violations") or ["unknown"])
            )
        if step.action == "start_platform_screens" and (
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
