"""Bounded read-only evidence collection for Ops-Privilege."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable

from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.workflow.contracts import (
    DiscoveryBudget,
    DiscoveryBudgetExceeded,
    EvidenceBundle,
    EvidenceRecord,
    ProbeRequest,
    normalize_probe_request,
)
from klonet_agent.ops.privileged.workflow.runtime_inventory import (
    RuntimeInventory, looks_like_runtime_goal, runtime_inventory_answers_goal,
)


DISCOVERY_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Discovery Agent.
You may request only registered read-only probes. Never propose actions, shell,
configuration changes, plans, or user confirmation.

Return one JSON object with status `need_evidence`, `ready`, or `blocked`.
For need_evidence return at most four probe_requests with probe, args, purpose.
Request only facts materially needed for the current goal. Do not repeat an
identical probe and arguments after evidence was returned.

When the user identifies a source instance by a Screen session name or prefix,
treat matching `screen_runtime` cwd/git_roots evidence as authoritative for
that source. Do not replace it with an unrelated path found by a broad runtime
or process probe. If a candidate path is not a Git repository, try the matching
Screen git_root or cwd ancestors before returning blocked.
Once a matching `screen_git_repositories` section reports
inside_work_tree=true with remote and branch, source discovery is complete.
Do not request screen_session or broad process probes to rediscover that source.

Registered probes:
%s
""".strip()


def parse_json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("response does not contain JSON")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("response JSON must be an object")
    return value


def _knowledge_task_type(goal: str) -> str:
    lowered = str(goal or "").lower()
    if any(marker in lowered for marker in (
        "失败", "故障", "异常", "报错", "修复", "恢复", "诊断", "排查",
        "failed", "error", "recover", "diagnose", "troubleshoot",
    )):
        return "troubleshooting"
    return "operation_guide"


class DiscoveryAgent:
    def __init__(
        self,
        llm: Any,
        *,
        probe_runner: Callable[[list[dict[str, Any]]], str] | None,
        readonly_command_runner: Callable[[str], str] | None = None,
        budget_factory: Callable[[], DiscoveryBudget] = DiscoveryBudget,
        on_progress: Callable[[str], None] | None = None,
        knowledge_search: Callable[..., str] | None = None,
    ) -> None:
        self.llm = llm
        self.probe_runner = probe_runner
        self.readonly_command_runner = readonly_command_runner
        self.budget_factory = budget_factory
        self.on_progress = on_progress
        self.knowledge_search = knowledge_search

    def collect_knowledge(self, goal: str, bundle: EvidenceBundle) -> None:
        """Collect one reusable, provenance-bearing Klonet knowledge record."""

        if self.knowledge_search is None:
            return

        task_type = _knowledge_task_type(goal)
        request = ProbeRequest(
            "klonet_knowledge",
            {"query": str(goal or "").strip(), "task_type": task_type},
            "检索目标相关的 Klonet 流程、目录和安全操作约束",
        )
        if any(item.request.cache_key == request.cache_key for item in bundle.records):
            return
        if self.on_progress is not None:
            self.on_progress("正在检索 Klonet 知识库…")
        try:
            output = self.knowledge_search(
                str(goal or "").strip(), top_k=6, task_type=task_type,
            )
            status = "available"
        except Exception as exc:
            output = "Klonet knowledge retrieval unavailable: %s" % type(exc).__name__
            status = "unavailable"
        bundle.add(EvidenceRecord.from_probe(request, output, status=status))
        if self.on_progress is not None:
            source = _knowledge_source_summary(output) if status == "available" else ""
            self.on_progress(
                "关键证据：Klonet 知识检索%s%s。"
                % (
                    "完成" if status == "available" else "不可用",
                    "（%s）" % source if source else "",
                )
            )
    def begin_probe_session(self) -> None:
        owner = getattr(self.probe_runner, "__self__", None)
        begin = getattr(owner, "begin_probe_session", None)
        if begin is not None:
            begin()

    def end_probe_session(self) -> None:
        owner = getattr(self.probe_runner, "__self__", None)
        end = getattr(owner, "end_probe_session", None)
        if end is not None:
            end()

    def collect_requests(
        self,
        requests: list[ProbeRequest],
        bundle: EvidenceBundle,
        *,
        derive_traceback_sources: bool = True,
    ) -> EvidenceBundle:
        """Collect a bounded Planner-requested evidence increment."""

        known_keys = {item.request.cache_key for item in bundle.records}
        fresh: list[ProbeRequest] = []
        for request in requests:
            if request.cache_key in known_keys:
                continue
            if DEFAULT_READONLY_PROBES.get(request.probe) is None:
                bundle.add(
                    EvidenceRecord.from_probe(
                        request,
                        "probe refused: probe_not_registered=%s" % request.probe,
                        status="unavailable",
                    )
                )
                known_keys.add(request.cache_key)
                continue
            fresh.append(request)
            known_keys.add(request.cache_key)
        if len(fresh) > 4:
            bundle.budget_exhausted = True
            fresh = fresh[:4]
        for request in fresh:
            if self.on_progress is not None:
                self.on_progress("正在收集只读证据：%s" % request.probe)
            try:
                output = (
                    self.probe_runner(
                        [{"probe": request.probe, "args": request.args, "purpose": request.purpose}]
                    )
                    if self.probe_runner is not None
                    else "probe runner unavailable"
                )
                status = "available" if self.probe_runner is not None else "unavailable"
            except Exception as exc:
                output = "probe failed: %s" % type(exc).__name__
                status = "unavailable"
            record = bundle.add(
                EvidenceRecord.from_probe(request, output, status=status)
            )
            self._add_derived_screen_source(bundle, record)
        if derive_traceback_sources and any(
            request.probe in {"logs", "process_logs"} for request in fresh
        ):
            self.collect_traceback_source_evidence(bundle)
        if any(request.probe == "running_platforms" for request in fresh):
            process_log_requests = _selected_master_process_log_requests(bundle)
            if process_log_requests:
                self.collect_requests(process_log_requests, bundle)
            entry_requests = _selected_master_entry_source_requests(bundle)
            if entry_requests:
                self.collect_requests(
                    entry_requests,
                    bundle,
                    derive_traceback_sources=False,
                )
        return bundle

    def collect_traceback_source_evidence(
        self,
        bundle: EvidenceBundle,
    ) -> EvidenceBundle:
        requests = _traceback_source_requests(bundle)
        if not requests:
            return bundle
        return self.collect_requests(
            requests,
            bundle,
            derive_traceback_sources=False,
        )

    def collect(
        self,
        goal: str,
        *,
        command: str = "",
        conversation_context: str = "",
        seed_bundle: EvidenceBundle | None = None,
        preload_capabilities: bool = False,
    ) -> EvidenceBundle:
        bundle = EvidenceBundle(
            goal=goal,
            records=list(getattr(seed_bundle, "records", []) or []),
        )
        self.collect_knowledge(goal, bundle)
        if preload_capabilities and not any(
            item.request.probe == "privilege_capabilities"
            for item in bundle.records
        ):
            self.collect_requests(
                [ProbeRequest(
                    "privilege_capabilities",
                    {},
                    "verify controlled privilege channels before operational discovery",
                )],
                bundle,
            )
        if _requires_running_platform_inventory(goal):
            self.collect_requests(
                [
                    ProbeRequest(
                        "running_platforms",
                        {},
                        "deterministically count only runtime roots whose master and worker health APIs are usable",
                    )
                ],
                bundle,
            )
            if _running_platform_target_choice_required(goal, bundle):
                return bundle
            if runtime_inventory_answers_goal(
                goal, RuntimeInventory.from_bundle(bundle),
            ):
                return bundle
            if _deterministic_restart_inventory_sufficient(goal, bundle):
                return bundle

        # A classifier-proposed command is a generic fallback, not authority
        # to bypass a typed domain probe.  Runtime inventory must be collected
        # first so an incidental `screen -ls` cannot replace root-bound health
        # evidence for a platform question.
        if command.strip():
            request = ProbeRequest(
                "readonly_command",
                {"command": command.strip()},
                "execute classifier-provided deterministic read-only command",
            )
            try:
                output = (
                    self.readonly_command_runner(command.strip())
                    if self.readonly_command_runner is not None
                    else "read-only command boundary unavailable"
                )
                status = (
                    "available"
                    if self.readonly_command_runner is not None
                    else "unavailable"
                )
            except Exception as exc:
                output = "read-only command failed: %s" % type(exc).__name__
                status = "unavailable"
            bundle.add(EvidenceRecord.from_probe(request, output, status=status))
            return bundle

        budget = self.budget_factory()
        messages = [
            {
                "role": "system",
                "content": DISCOVERY_SYSTEM_PROMPT % DEFAULT_READONLY_PROBES.render(),
            },
            {
                "role": "user",
                "content": "Recent conversation:\n%s\n\nCurrent goal:\n%s"
                % (conversation_context or "(none)", goal),
            },
        ]
        if bundle.records:
            messages.append(
                {
                    "role": "user",
                    "content": "Deterministic evidence already collected:\n%s"
                    % "\n\n".join(
                        "%s (%s):\n%s"
                        % (
                            record.evidence_id,
                            record.request.probe,
                            record.output[:7000],
                        )
                        for record in bundle.records
                    ),
                }
            )
        invalid_repairs = 0
        while True:
            try:
                data = parse_json_object(self._complete(messages))
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                if invalid_repairs >= 1:
                    bundle.blocked_reason = "discovery output invalid: %s" % exc
                    return bundle
                invalid_repairs += 1
                messages.append(
                    {
                        "role": "user",
                        "content": "Return one valid Discovery JSON object. Error: %s"
                        % exc,
                    }
                )
                continue
            status = str(data.get("status") or "").strip().lower()
            if status == "ready":
                return bundle
            if status == "blocked":
                bundle.blocked_reason = str(data.get("reason") or "discovery blocked")
                return bundle
            if status != "need_evidence":
                messages.append(
                    {
                        "role": "user",
                        "content": "status must be need_evidence, ready, or blocked",
                    }
                )
                invalid_repairs += 1
                if invalid_repairs > 1:
                    bundle.blocked_reason = "invalid discovery status"
                    return bundle
                continue
            requests = self._requests(data.get("probe_requests"))
            try:
                fresh = budget.register_round(requests)
            except DiscoveryBudgetExceeded:
                bundle.budget_exhausted = True
                return bundle
            evidence_sections = []
            for request in fresh:
                if self.on_progress is not None:
                    self.on_progress("正在执行只读检查：%s" % request.probe)
                try:
                    output = (
                        self.probe_runner(
                            [
                                {
                                    "probe": request.probe,
                                    "args": request.args,
                                    "purpose": request.purpose,
                                }
                            ]
                        )
                        if self.probe_runner is not None
                        else "probe runner unavailable"
                    )
                    status_value = (
                        "available" if self.probe_runner is not None else "unavailable"
                    )
                except Exception as exc:
                    output = "probe failed: %s" % type(exc).__name__
                    status_value = "unavailable"
                record = bundle.add(
                    EvidenceRecord.from_probe(
                        request,
                        output,
                        status=status_value,
                    )
                )
                self._add_derived_screen_source(bundle, record)
                evidence_sections.append(
                    "%s (%s):\n%s"
                    % (record.evidence_id, request.probe, record.output[:7000])
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Read-only evidence for this round:\n%s\n\n"
                        "Continue discovery. Reuse previously returned evidence and "
                        "return ready when facts are sufficient."
                    )
                    % ("\n\n".join(evidence_sections) or "No new evidence; duplicates were reused."),
                }
            )

    @staticmethod
    def _add_derived_screen_source(
        bundle: EvidenceBundle,
        record: EvidenceRecord,
    ) -> None:
        if (
            record.status != "available"
            or record.request.probe != "screen"
            or "screen" not in str(bundle.goal or "").lower()
            or "remotes=origin" not in record.output
        ):
            return
        roots = {
            match.group(1)
            for match in re.finditer(
                r"\bpath=(/[^\s]+)\s+inside_work_tree=true",
                record.output,
            )
        }
        if len(roots) != 1:
            return
        root = next(iter(roots))
        request = ProbeRequest(
            "git_repository",
            {"repository": root},
            "derived authoritative Screen source Git repository",
        )
        if any(item.request.cache_key == request.cache_key for item in bundle.records):
            return
        bundle.add(
            EvidenceRecord.from_probe(
                request,
                record.output,
                status="available",
            )
        )

    def _complete(self, messages: list[dict[str, str]]) -> str:
        response = self.llm.complete(
            messages=messages,
            tools=None,
            reasoning_effort="low",
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return response.choices[0].message.content or ""

    @staticmethod
    def _requests(value: Any) -> list[ProbeRequest]:
        if not isinstance(value, list):
            return []
        requests = []
        known_keys: set[str] = set()
        aliases = {
            "screen_runtime": "screen",
            "screen_git_repositories": "screen",
        }
        for item in value:
            if not isinstance(item, dict):
                continue
            requested_probe = str(item.get("probe") or "").strip()
            normalized_probe, normalized_args = normalize_probe_request(
                aliases.get(requested_probe, requested_probe),
                item.get("args") if isinstance(item.get("args"), dict) else {},
            )
            request = ProbeRequest(
                probe=normalized_probe,
                args=normalized_args,
                purpose=str(item.get("purpose") or "collect required fact"),
            )
            if request.cache_key in known_keys:
                continue
            known_keys.add(request.cache_key)
            requests.append(request)
        return requests


def _requires_running_platform_inventory(goal: str) -> bool:
    text = str(goal or "").lower()
    platform_requested = bool(
        "平台" in text
        or "platform" in text
        or "实例" in text
        or "instance" in text
        or re.search(r"/[A-Za-z0-9._/-]*vemu_uestc\b", text)
        or (
            any(role in text for role in ("master", "worker"))
            and bool(re.search(r"\b[A-Za-z][A-Za-z0-9_.-]*\b", text))
        )
    )
    health_requested = any(
        marker in text
        for marker in ("正常运行", "运行的平台", "后端接口", "healthy", "running platform")
    )
    inventory_requested = any(
        marker in text for marker in ("多少", "几个", "数量", "哪些", "how many", "which")
    )
    repair_requested = any(
        marker in text for marker in (
            "修复", "恢复", "排查", "重启", "启动", "停止",
            "fix", "repair", "recover", "restart", "start", "stop",
        )
    )
    return (
        platform_requested and (health_requested or repair_requested) and (
            inventory_requested or repair_requested
        )
    ) or looks_like_runtime_goal(goal)


def _knowledge_source_summary(output: str) -> str:
    status = re.search(r"\bretrieval_status\s*[:=]\s*([^\s,]+)", output, re.I)
    source = re.search(
        r"(?:^|\n)\s*(?:-\s*)?(?:path|source)\s*[:=]\s*([^\s]+)",
        output,
        re.I,
    )
    parts = []
    if status:
        parts.append(status.group(1))
    if source:
        parts.append(Path(source.group(1)).name)
    return "；".join(parts)


def _deterministic_restart_inventory_sufficient(
    goal: str,
    bundle: EvidenceBundle,
) -> bool:
    """Stop Discovery once an explicit restart target has one complete runtime row."""

    text = str(goal or "")
    lowered = text.lower()
    if not any(marker in lowered for marker in ("重启", "restart")):
        return False
    candidates = [
        item for item in RuntimeInventory.from_bundle(bundle).matching(text)
        if all(
            item.fields.get(name)
            for name in (
                "configured_ports", "component_specs_b64", "runtime_identities",
            )
        )
    ]
    return len(candidates) == 1


def _running_platform_target_choice_required(
    goal: str,
    bundle: EvidenceBundle,
) -> bool:
    text = str(goal or "")
    lowered = text.lower()
    if not any(
        marker in lowered for marker in ("修复", "恢复", "排查", "fix", "repair", "recover")
    ):
        return False
    inventory = RuntimeInventory.from_bundle(bundle)
    roots = [item.project_root for item in inventory.abnormal]
    if (
        len(roots) < 2
        or bool(inventory.matching(text))
    ):
        return False
    return not any(
        marker in lowered for marker in ("全部", "所有", "all of", "all abnormal")
    )


def _traceback_source_requests(bundle: EvidenceBundle) -> list[ProbeRequest]:
    goal = str(bundle.goal or "")
    roots = []
    for raw in re.findall(r"/[A-Za-z0-9._/-]+", goal):
        root = Path(raw.rstrip("/"))
        if root not in roots:
            roots.append(root)
    inventory = RuntimeInventory.from_bundle(bundle)
    for item in inventory.matching(goal):
        root = Path(item.project_root)
        if root not in roots:
            roots.append(root)
    if not roots and len(inventory.instances) == 1:
        roots.append(Path(inventory.instances[0].project_root))
    if not roots:
        return []
    candidates = []
    for record in bundle.records:
        if record.request.probe not in {"logs", "process_logs"} or record.status != "available":
            continue
        for raw in re.findall(r'File "(/[^"]+\.py)", line \d+', record.output):
            path = Path(raw)
            if not any(path == root or root in path.parents for root in roots):
                continue
            if path not in candidates:
                candidates.append(path)
    return [
        ProbeRequest(
            "ops_file",
            {"path": str(path), "view": "head", "max_chars": 20000},
            "inspect the target-owned Python source named by the startup traceback",
        )
        for path in candidates[:2]
    ]


def _selected_master_process_log_requests(bundle: EvidenceBundle) -> list[ProbeRequest]:
    goal = str(bundle.goal or "")
    lowered = goal.lower()
    if not any(
        marker in lowered
        for marker in (
            "启动", "日志", "报错", "异常", "故障", "修复",
            "startup", "boot", "log", "error", "failure", "repair",
        )
    ):
        return []
    requests = []
    inventory = RuntimeInventory.from_bundle(bundle)
    for item in inventory.matching(goal):
        if item.backend_status == "healthy":
            continue
        raw_pids = item.fields.get("master_pids", "")
        if not re.fullmatch(r"[0-9,]+", raw_pids):
            continue
        pids = [int(value) for value in raw_pids.split(",")[:8]]
        requests.append(ProbeRequest(
            "process_logs",
            {"pids": pids, "project_root": item.project_root},
            "read the selected master process stdout/stderr logs after rechecking cwd",
        ))
    return requests[:1]


def _selected_master_entry_source_requests(
    bundle: EvidenceBundle,
) -> list[ProbeRequest]:
    """Read the selected failing master entry without log-path guesswork."""

    goal = str(bundle.goal or "")
    lowered = goal.lower()
    if not any(
        marker in lowered
        for marker in (
            "启动", "新增", "代码", "入口", "日志", "故障", "修复",
            "startup", "boot", "code", "entry", "log", "failure", "repair",
        )
    ):
        return []
    requests: list[ProbeRequest] = []
    inventory = RuntimeInventory.from_bundle(bundle)
    for item in inventory.matching(goal):
        if item.backend_status == "healthy" or item.endpoints.get("master") == "healthy":
            continue
        root = Path(item.project_root)
        candidates = (
            root / "mains" / "master_main.py",
            root / "master_main.py",
            root / "vemu_uestc" / "mains" / "master_main.py",
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
        requests.append(ProbeRequest(
            "ops_file",
            {"path": str(path), "view": "head", "max_chars": 20000},
            "inspect the selected failing master startup entry",
        ))
    return requests[:1]
