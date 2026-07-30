"""独立、无工具权限的高权限任务 Planner。"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from klonet_agent.ops.actions import (
    OpsActionRegistry,
    configured_ops_action_registry,
)
from klonet_agent.ops.command_policy import command_exists, decide_ops_command
from klonet_agent.ops.privileged.context import GroundedPlanContext
from klonet_agent.ops.privileged.action_runner import (
    DIRECT_PRIVILEGED_ACTIONS,
)
from klonet_agent.ops.privileged.planner_schema import REQUIRED_ACTION_ARGS
from klonet_agent.ops.privileged.contracts import (
    PrivilegedPlan,
    PrivilegedStep,
    RISK_LEVELS,
)


PLANNER_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Planner.
You plan operations for the Klonet project. An unqualified "平台" means the
Klonet platform unless the user or conversation explicitly names another
product. Never replace an unknown target with a generic Nginx/demo web stack.
Return one JSON object only. Decompose the requested operational goal into the
smallest auditable registered actions. Each step uses step_id, title, action and
args. Never return command, shell, script, or executable text. Include timeout,
expected_changes, preconditions, postconditions or rollback guidance only when
a non-default value is materially useful;
rollback must be descriptive and must never contain a command. Deterministic code
will add standard checks and defaults.
Use concise Chinese step titles. Normally return 3-6 steps and never more than
8. Merge related read-only probes into one auditable step instead of creating
one step per inventory command. If details can be discovered, start with the
smallest read-only discovery step and do not perform a broad machine inventory.
Never include passwords. Mark destructive operations honestly. Prefer explicit
postconditions that prove the requested state, not merely a zero exit code.
Use only action names present in the supplied Allowed action registry. Arguments
must be grounded in the supplied evidence; never invent paths, platform names,
services, hosts or ports. If required facts are absent, return only a safe
registered inspection/validation action when possible; otherwise return an
invalid plan rather than guessing. You have no tools and cannot execute actions.
Treat Structured environment facts as the authoritative resource model. Never
use source_repo_root as project_root when the model gives a different
platform_root. runtime_cwd is the only valid start project_root. Secret facts
contain metadata only and must never be expanded, requested, copied, or echoed.
""".strip()

MAX_PRIVILEGED_PLAN_STEPS = 8


class PrivilegedPlannerAgent:
    def __init__(
        self,
        llm: Any,
        policy: Any | None = None,
        action_registry: OpsActionRegistry | None = None,
    ) -> None:
        self.llm = llm
        # policy is retained as a compatibility argument. Action metadata, not
        # model-authored command text, is now the deterministic risk source.
        self.policy = policy
        self.action_registry = action_registry or configured_ops_action_registry()

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
                "## Read-only server evidence\n%s\n\n## Allowed action registry\n%s"
                % (environment_context or "(none)", self._action_catalog())
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
        planner_source = "llm"
        for attempt in range(2):
            response = self._complete(messages)
            content = response.choices[0].message.content or ""
            try:
                data = _parse_json_object(content)
                steps = self._build_steps(
                    data,
                    enforce_host_facts=grounded_context is not None,
                )
                if grounded_context is not None:
                    _validate_environment_model(steps, grounded_context)
                _validate_goal_action_compatibility(goal, steps)
                break
            except (KeyError, TypeError, ValueError) as exc:
                last_error = str(exc)
                if attempt == 0:
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Repair the invalid response. Return one valid JSON object "
                                "matching the required schema. Error: %s" % last_error
                            ),
                        }
                    )
        else:
            steps = _deterministic_grounded_steps(goal, grounded_context)
            if not steps:
                raise ValueError(
                    "Planner did not return a valid privileged plan: %s"
                    % last_error
                )
            data = {"goal": goal}
            planner_source = "deterministic_grounded_resolver"
            if grounded_context is not None:
                _validate_environment_model(steps, grounded_context)

        partial_verification = False
        for step in steps:
            if not step.postconditions:
                step.postconditions = _default_action_postconditions(
                    step.action,
                    step.args,
                )
            if not step.postconditions:
                step.postconditions = [{"checker": "exit_code_zero", "args": {}}]
            if step.risk != "readonly" and all(
                item.get("checker") == "exit_code_zero"
                for item in step.postconditions
            ):
                partial_verification = True

        risk = _effective_declared_risk(
            _highest_risk(steps),
            data.get("risk"),
        )
        plan = PrivilegedPlan(
            plan_id="priv-" + uuid.uuid4().hex[:10],
            goal=str(data.get("goal") or goal).strip(),
            risk=risk,
            steps=steps,
            verification_level=(
                "partial" if partial_verification else "full"
            ),
            status="awaiting_confirmation",
            grounding=_grounding_summary(grounded_context, planner_source),
        )
        if risk == "readonly":
            plan.authorize()
            for step in plan.steps:
                step.status = "approved"
        return plan

    def _complete(self, messages: list[dict[str, str]]) -> Any:
        try:
            return self.llm.complete(
                messages=messages,
                tools=None,
                reasoning_effort="low",
                temperature=0,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
        except TypeError:
            return self.llm.complete(messages=messages, tools=None)

    def _build_steps(
        self,
        data: dict[str, Any],
        *,
        enforce_host_facts: bool = False,
    ) -> list[PrivilegedStep]:
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("steps must be a non-empty array")
        if len(raw_steps) > MAX_PRIVILEGED_PLAN_STEPS:
            raise ValueError(
                "steps exceeds maximum of %s" % MAX_PRIVILEGED_PLAN_STEPS
            )
        steps = []
        seen = set()
        seen_actions = set()
        for index, item in enumerate(raw_steps, start=1):
            if not isinstance(item, dict):
                raise ValueError("step must be an object")
            step_id = str(item.get("step_id") or "step-%s" % index).strip()
            if any(key in item for key in ("command", "shell", "script", "executable")):
                raise ValueError("step must use action + args, not command or shell")
            rollback = str(item.get("rollback") or "").strip()
            if _looks_like_command(rollback):
                raise ValueError("rollback must be descriptive, not executable text")
            requested_action = str(item.get("action") or "").strip()
            spec = self.action_registry.get(requested_action)
            if (
                spec is None
                or spec.name not in DIRECT_PRIVILEGED_ACTIONS
            ):
                raise ValueError("action_not_allowlisted=%s" % (requested_action or "missing"))
            args = item.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("step args must be an object")
            missing = [
                key
                for key in REQUIRED_ACTION_ARGS.get(spec.name, ())
                if not args.get(key)
            ]
            if missing:
                raise ValueError(
                    "action=%s missing_required_args=%s"
                    % (spec.name, ",".join(missing))
                )
            problem = self.action_registry.validate_args(spec, args)
            if problem:
                raise ValueError(problem)
            problem = _validate_action_semantics(spec.name, args)
            if problem:
                raise ValueError(problem)
            if enforce_host_facts:
                problem = _validate_host_facts(spec.name, args)
                if problem:
                    raise ValueError(problem)
            command_decision = (
                decide_ops_command(args)
                if spec.name == "run_ops_command"
                else None
            )
            if command_decision is not None:
                if not command_decision.allowed:
                    raise ValueError(
                        "controlled_argv_not_allowed=%s"
                        % command_decision.reason
                    )
                if enforce_host_facts and not command_exists(
                    command_decision.program
                ):
                    raise ValueError(
                        "controlled_argv_program_not_found=%s"
                        % command_decision.program
                    )
            action_identity = (
                spec.name,
                json.dumps(args, ensure_ascii=False, sort_keys=True),
            )
            if action_identity in seen_actions:
                raise ValueError("duplicate action with identical args: %s" % spec.name)
            seen_actions.add(action_identity)
            if step_id in seen:
                raise ValueError("duplicate step_id: %s" % step_id)
            seen.add(step_id)
            steps.append(
                PrivilegedStep(
                    step_id=step_id,
                    title=str(item.get("title") or step_id).strip(),
                    action=spec.name,
                    args=_clean_args(args),
                    risk=_effective_declared_risk(
                        _action_risk(
                            command_decision.risk
                            if command_decision is not None
                            else spec.risk
                        ),
                        item.get("risk"),
                    ),
                    approval_scope=(
                        "step"
                        if (
                            spec.confirmation_scope == "step"
                            or (
                                command_decision is not None
                                and command_decision.requires_step_confirmation
                            )
                        )
                        else "plan"
                    ),
                    timeout=int(item.get("timeout") or 120),
                    expected_changes=_string_list(item.get("expected_changes")),
                    preconditions=_check_list(item.get("preconditions")),
                    postconditions=_check_list(item.get("postconditions")),
                    rollback=rollback,
                    status=(
                        "awaiting_confirmation"
                        if (
                            spec.confirmation_scope == "step"
                            or (
                                command_decision is not None
                                and command_decision.requires_step_confirmation
                            )
                        )
                        else "pending"
                    ),
                )
            )
        return steps

    def _action_catalog(self) -> str:
        lines = []
        for spec in self.action_registry.describe():
            if spec.name not in DIRECT_PRIVILEGED_ACTIONS:
                continue
            lines.append(
                "- action=%s category=%s risk=%s description=%s "
                "required_args=%s path_args=%s preconditions=%s effects=%s "
                "postconditions=%s backends=%s"
                % (
                    spec.name,
                    spec.category,
                    spec.risk,
                    spec.description or spec.name,
                    ",".join(REQUIRED_ACTION_ARGS.get(spec.name, ())) or "none",
                    ",".join(spec.path_args) or "none",
                    ",".join(spec.preconditions) or "none",
                    ",".join(spec.effects) or "none",
                    ",".join(spec.postconditions) or "none",
                    ",".join(spec.backends),
                )
            )
        return "\n".join(lines)


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


def _check_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and item.get("checker")]


def _clean_args(value: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, item in list(value.items())[:40]:
        normalized = str(key or "").strip()[:80]
        if not normalized or item is None:
            continue
        if isinstance(item, list):
            result[normalized] = [str(part)[:300] for part in item[:40]]
        elif isinstance(item, dict):
            result[normalized] = {
                str(part_key)[:80]: str(part_value)[:300]
                for part_key, part_value in list(item.items())[:40]
            }
        else:
            result[normalized] = str(item)[:20000 if normalized == "content" else 300]
    return result


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


def _looks_like_command(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    command_markers = (
        "sudo ",
        "systemctl ",
        "docker ",
        "rm ",
        "cp ",
        "mv ",
        "bash ",
        "sh ",
        "&&",
        "||",
        "|",
        ";",
        "\n",
    )
    return any(marker in lowered for marker in command_markers)


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


def _validate_goal_action_compatibility(
    goal: str,
    steps: list[PrivilegedStep],
) -> None:
    """Reject action families that contradict the user's requested direction."""

    lowered = str(goal or "").lower()
    deploy_goal = "部署" in lowered or "deploy" in lowered or "安装平台" in lowered
    if not deploy_goal:
        return
    forbidden = {
        "stop_screen_component",
        "stop_platform_screens",
        "remove_python_package_entries",
    }
    conflicting = [step.action for step in steps if step.action in forbidden]
    if conflicting:
        raise ValueError(
            "deployment plan contains contradictory destructive actions: %s"
            % ",".join(conflicting)
        )


def _deterministic_grounded_steps(
    goal: str,
    context: GroundedPlanContext | None,
) -> list[PrivilegedStep]:
    """Resolve the common Klonet deployment path without trusting model output."""

    if context is None:
        return []
    lowered = str(goal or "").lower()
    if "先处理已诊断的失败原因" in lowered or "修复能力要求" in lowered:
        # This resolver only knows the normal deployment happy path. Using it
        # during recovery would silently recreate the plan that just failed.
        return []
    if not ("部署" in lowered or "deploy" in lowered or "安装平台" in lowered):
        return []
    model = context.facts.get("environment_model")
    projects = (
        model.get("projects", [])
        if isinstance(model, dict)
        else []
    )
    usable = [
        item for item in projects
        if isinstance(item, dict)
        and item.get("readiness") in {"preparable", "runnable"}
    ]
    if len(usable) == 1:
        layout = usable[0]
        root = Path(str(layout["platform_root"])).expanduser()
        entry_source = str(layout.get("entry_source_root") or "")
        platform = Path(
            str(layout.get("source_repo_root") or root)
        ).name
    else:
        roots = list(context.facts.get("candidate_project_roots") or [])
        if len(roots) != 1:
            return []
        root = Path(str(roots[0])).expanduser()
        entry_source = str(root / "mains")
        platform = root.name
    if _validate_host_facts("validate_project_files", {"project_root": str(root)}):
        return []
    steps = [
        PrivilegedStep(
            step_id="validate-project",
            title="校验 Klonet 项目文件",
            action="validate_project_files",
            args={"project_root": str(root)},
            risk="readonly",
        )
    ]
    required = (
        "gun.py",
        "master_main.py",
        "celery_worker.py",
        "web_terminal_main.py",
        "worker_gun.py",
        "worker_main.py",
    )
    if any(not (root / name).is_file() for name in required):
        steps.append(
            PrivilegedStep(
                step_id="prepare-project",
                title="准备 Klonet 项目入口文件",
                action="prepare_project_files",
                args={
                    "project_root": str(root),
                    "source_root": entry_source,
                },
                risk="medium",
            )
        )
    steps.append(
        PrivilegedStep(
            step_id="start-platform",
            title="启动 Klonet 平台组件",
            action="start_platform_screens",
            args={"platform": platform, "project_root": str(root)},
            risk="medium",
        )
    )
    return steps


def _grounding_summary(
    context: GroundedPlanContext | None,
    planner_source: str,
) -> dict[str, Any]:
    summary = (
        context.audit_summary()
        if context is not None
        else {"context_policy": "caller_environment+action_registry"}
    )
    summary["planner_source"] = planner_source
    return summary
