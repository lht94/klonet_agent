"""Shared action-contract validation for Ops-Privilege."""

from __future__ import annotations

import ast
import json
import re
import textwrap
from pathlib import Path
from typing import Any

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

MAX_PRIVILEGED_PLAN_STEPS = 8

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
        if not str(args.get("new_text") or ""):
            return [
                {
                    "checker": "file_not_contains",
                    "args": {
                        "path": args.get("path"),
                        "text": args.get("old_text"),
                    },
                }
            ]
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
        hashes = (
            args.get("entry_sha256s")
            if isinstance(args.get("entry_sha256s"), dict)
            else {}
        )
        return [
            {
                "checker": "file_sha256" if hashes.get(name) else "file_exists",
                "args": {
                    "path": str(root / name),
                    **({"sha256": hashes[name]} if hashes.get(name) else {}),
                },
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
    if action == "stop_klonet_component":
        return [
            {"checker": "process_pid_absent", "args": {"pid": args.get("pid")}},
            {"checker": "port_not_listening", "args": {"port": args.get("port")}},
        ]
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
        if action == "stop_platform_screens":
            contracts = args.get("component_contracts")
            if not isinstance(contracts, list):
                return []
            sessions = [
                str(item.get("screen_session") or "")
                for item in contracts if isinstance(item, dict)
            ]
            return [
                {"checker": checker, "args": {"session": session}}
                for session in sessions if session
            ]
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
        re.search(r"閮ㄧ讲|鏂板.{0,12}瀹炰緥|鏂?{0,8}骞冲彴|\bdeploy(?:ment)?\b|\bnew instance\b", text)
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
        r"鏂板.{0,12}(?:瀹炰緥|骞冲彴)|鏂?{0,8}(?:瀹炰緥|骞冲彴)|"
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
            r"澶嶅埗|鍏嬮殕|鍑嗗.{0,8}婧愮爜|"
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
            r"鍚姩.{0,16}(?:骞冲彴|缁勪欢|鏈嶅姟)|\bstart.{0,20}(?:platform|component|service)",
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
            re.search(r"nginx|鍙嶅悜浠ｇ悊|璺敱|proxy", step_text, re.IGNORECASE)
            and re.search(
                r"鍝嶅簲|鍙揪|accessible|responsive|curl|"
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
            return "grounding_failed=project_root_not_directory"
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

    if action in {
        "write_ops_file",
        "replace_text_in_file",
        "insert_text_before_anchor",
        "edit_text_file",
    } and "[REDACTED]" in json.dumps(args, ensure_ascii=False, default=str):
        return "action=%s redacted_placeholder_cannot_be_written" % action

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
            "clone", "clone_at_revision", "submodule_update", "reset", "revert", "restore", "tag",
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
    if action == "replace_text_in_file":
        old_text = str(args.get("old_text") or "")
        new_text = str(args.get("new_text") or "")
        if (
            not new_text
            and str(args.get("path") or "").endswith(".py")
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", old_text.strip())
        ):
            return "action=replace_text_in_file ungrounded_python_identifier_deletion"
        if not new_text and old_text.lstrip().startswith("def "):
            try:
                parsed = ast.parse(old_text)
            except SyntaxError:
                return "action=replace_text_in_file incomplete_python_function_deletion"
            if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in parsed.body):
                return "action=replace_text_in_file incomplete_python_function_deletion"
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
        suffix = suffixes.get(component) or str(args.get("screen_suffix") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", component):
            return "action=%s invalid_component=%s" % (action, component or "missing")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", suffix):
            return "action=%s invalid_screen_suffix" % action
        if component not in suffixes:
            command = args.get("command_argv")
            preflight = args.get("preflight_argv")
            if not _safe_component_argv(command):
                return "action=%s invalid_component_command" % action
            if preflight is not None and not _safe_component_argv(preflight):
                return "action=%s invalid_component_preflight" % action
        if platform and session != "%s_%s" % (platform, suffix):
            return "action=%s screen_session_mismatch" % action
        run_as_uid = str(args.get("run_as_uid") or "").strip()
        if run_as_uid and not re.fullmatch(r"[1-9]\d{0,9}", run_as_uid):
            return "action=%s invalid_run_as_uid" % action
    if action == "stop_klonet_component":
        component = str(args.get("component") or "").strip()
        if component not in {"master", "worker"}:
            return "action=stop_klonet_component invalid_component=%s" % (
                component or "missing"
            )
        try:
            pid = int(args.get("pid"))
            port = int(args.get("port"))
        except (TypeError, ValueError):
            return "action=stop_klonet_component invalid_pid_or_port"
        if pid <= 1 or not 1 <= port <= 65535:
            return "action=stop_klonet_component invalid_pid_or_port"
    if action == "manage_container" and operation == "set_restart_policy":
        if str(args.get("restart_policy") or "") not in {
            "no", "always", "unless-stopped", "on-failure",
        }:
            return "action=manage_container restart_policy_required"
    if action == "create_docker_container" and args.get("command"):
        command = args.get("command")
        name = str(args.get("name") or "").lower()
        image = str(args.get("image") or "").lower()
        if not (
            isinstance(command, list)
            and len(command) == 3
            and command[:2] == ["redis-server", "--requirepass"]
            and isinstance(command[2], str)
            and 8 <= len(command[2]) <= 128
            and not any(character.isspace() for character in command[2])
            and "redis" in name
            and "redis" in image
        ):
            return "action=create_docker_container invalid_container_command"
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
        if operation in {"clone", "clone_at_revision"} and not args.get("url"):
            return "action=git_operation url_required"
        if operation == "clone_at_revision" and not (
            args.get("ref") and args.get("revision")
        ):
            return "action=git_operation clone_at_revision_requires_ref_and_revision"
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


def _safe_component_argv(value: Any) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        return False
    for item in value:
        text = str(item)
        if not text or len(text) > 2000 or "\x00" in text or "\n" in text:
            return False
    executable = str(value[0])
    return bool(
        executable.startswith("/")
        or re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}", executable)
    )
