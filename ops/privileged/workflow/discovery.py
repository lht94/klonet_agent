"""Bounded read-only evidence collection for Ops-Privilege."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import re
import shlex
from typing import Any, Callable

from klonet_agent.ops.privileged.probes import DEFAULT_READONLY_PROBES
from klonet_agent.ops.privileged.context import klonet_domain_context
from klonet_agent.ops.privileged.workflow.contracts import (
    DiscoveryBudget,
    DiscoveryBudgetExceeded,
    EvidenceSubject,
    EvidenceBundle,
    EvidenceRecord,
    FactObservation,
    FactRequirement,
    ProbeRequest,
    extract_labeled_deployment_paths,
    infer_evidence_subject,
    normalize_probe_request,
)
from klonet_agent.ops.privileged.workflow.runtime_inventory import (
    RuntimeInventory, looks_like_runtime_goal, runtime_inventory_answers_goal,
)


DISCOVERY_SYSTEM_PROMPT = """
You are the Klonet Ops-Privilege Discovery Agent.
Prefer registered read-only probes. For a long-tail fact that the catalog does
not cover, you may name the intended read-only capability and precisely state
required_facts; the deterministic Discovery boundary will bind it to a safe
read-only command. Never propose mutations, plans, or user confirmation.

Return one JSON object with status `need_evidence`, `ready`, or `blocked`.
For need_evidence return at most four probe_requests with probe, args, purpose,
required_facts, subject, and optional freshness (`cached` or `refresh`).
Every required_facts item must contain stable fact_id, predicate, expected,
comparison, and freshness. Reuse the same fact_id for the same fact. The
subject must identify the exact path, port set, process, session, service, or
runtime being inspected. Probe args must inspect that same subject.
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

When the goal explicitly says to copy or synchronize the current working tree,
project_layout evidence for that exact source is sufficient. Do not request Git
remote, branch, revision, or cleanliness facts unless the user asked for Git
semantics or the planned source operation is git_operation.

Registered probes:
%s
""".strip()


READONLY_FALLBACK_SYSTEM_PROMPT = """
You bind one unresolved evidence request to a deterministic read-only command.
The command is only a candidate: policy will parse it into argv and reject shell
evaluation or mutations. Never use pipes, redirects, command substitution,
background execution, sudo password input, network access, or write operations.
Inspect prior_fallback_attempts. Never return an identical command that already
failed to add the requested facts; choose a materially different read-only
inspection or return blocked.

Return exactly one JSON object:
- {"status":"satisfied","reason":"..."} when the supplied registered-probe
  output already contains every required fact;
- {"status":"command","command":"one non-interactive read-only command",
   "covers":["fact-id"], "subject":{"kind":"...","value":"..."},
   "extractors":[{"fact_id":"...","kind":"output_contains|output_regex|output_nonempty",
   "expected":"..."}], "scope_expansion_reason":"", "reason":"..."}
  when a supplemental command can obtain the missing facts;
- {"status":"blocked","reason":"..."} only when no safe command can do so.
The command must preserve the supplied subject, covers and exclusions. Do not
expand to unrelated paths or components. If the exact subject is genuinely
insufficient, state a non-empty scope_expansion_reason; policy may still reject
the expansion.
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


def _comparison_status(requirement: FactRequirement, actual: Any) -> str:
    if actual is None:
        return "unresolved"
    if requirement.comparison == "present":
        return "confirmed" if actual not in (None, "", [], {}) else "contradicted"
    if requirement.comparison == "equals":
        return "confirmed" if actual == requirement.expected else "contradicted"
    if requirement.comparison == "contains":
        try:
            matched = requirement.expected in actual
        except TypeError:
            matched = False
        return "confirmed" if matched else "contradicted"
    if requirement.comparison == "contains_all":
        expected = requirement.expected
        if not isinstance(expected, (list, tuple, set)):
            return "unresolved"
        try:
            matched = set(expected).issubset(set(actual))
        except TypeError:
            matched = False
        return "confirmed" if matched else "contradicted"
    return "unresolved"


def _registered_observations(
    request: ProbeRequest,
    output: str,
    *,
    status: str,
) -> tuple[FactObservation, ...]:
    """Map typed Probe output to fact ids without another model judgement."""

    observations: list[FactObservation] = []
    if not request.required_facts:
        return ()
    if status != "available":
        return tuple(
            FactObservation(item.fact_id, "unresolved", None, "probe.unavailable")
            for item in request.required_facts
        )
    spec = DEFAULT_READONLY_PROBES.get(request.probe)
    supported = set(spec.supported_predicates if spec is not None else ())
    actual_by_predicate: dict[str, Any] = {}
    if request.probe == "ports":
        for label, predicate in (
            ("available_ports", "port.available"),
            ("occupied_ports", "port.occupied"),
        ):
            match = re.search(r"(?m)^%s=([^\n]+)$" % label, output)
            if match is not None:
                raw = match.group(1).strip()
                actual_by_predicate[predicate] = [] if raw == "none" else [
                    int(item) for item in raw.split(",") if item.strip().isdigit()
                ]
        actual_by_predicate["port.in_use"] = actual_by_predicate.get(
            "port.occupied", []
        )
        actual_by_predicate["port.listening"] = actual_by_predicate.get(
            "port.occupied", []
        )
        actual_by_predicate["port.listener"] = output if "LISTEN" in output else ""
    elif request.probe == "project_layout":
        try:
            # Production wraps probe output in a recovery section header.
            # Evidence extraction must not depend on that UI wrapper.
            data = parse_json_object(output)
        except (TypeError, ValueError):
            data = {}
        projects = data.get("projects") if isinstance(data, dict) else []
        projects = projects if isinstance(projects, list) else []
        subject_values = []
        if request.subject is not None:
            value = request.subject.value
            subject_values = value if isinstance(value, list) else [value]
        selected = [
            item for item in projects if isinstance(item, dict)
            and (
                not subject_values
                or str(item.get("candidate_root") or "") in {
                    str(value) for value in subject_values
                }
            )
        ]
        actual_by_predicate["path.exists"] = bool(selected)
        if selected:
            project = selected[0]
            actual_by_predicate["project.layout"] = project.get("layout_kind")
            actual_by_predicate["project.readiness"] = project.get("readiness")
            actual_by_predicate["project.runnable"] = (
                project.get("readiness") == "runnable"
            )
            actual_by_predicate["project.runtime_cwd"] = project.get("runtime_cwd")
            actual_by_predicate["project.source_root"] = project.get("source_repo_root")
            violations = set(project.get("violations") or [])
            actual_by_predicate["project.entry_files"] = (
                [
                    "gun.py", "master_main.py", "celery_worker.py",
                    "web_terminal_main.py", "worker_gun.py", "worker_main.py",
                ]
                if "entry_sources_missing" not in violations else []
            )
    elif request.probe == "screen":
        sessions = [
            match.group(1)
            for match in re.finditer(
                r"(?m)^\s*\d+\.([A-Za-z0-9_.-]{1,128})\s+", output,
            )
        ]
        actual_by_predicate["screen.sessions"] = sessions
        requested = str(request.args.get("session") or "").strip()
        if requested:
            actual_by_predicate["screen.session_exists"] = requested in sessions
            actual_by_predicate["screen.session_available"] = requested not in sessions
    elif request.probe == "git_repository":
        inside = re.search(r"\binside_work_tree=([^\s]+)", output)
        revision = re.search(r"\brevision=([^\s]+)", output)
        status_match = re.search(r"(?m)^status=(.*)$", output)
        remotes_match = re.search(r"(?ms)^remotes=(.*)$", output)
        if inside is not None:
            actual_by_predicate["git.inside_work_tree"] = (
                inside.group(1).strip().lower() == "true"
            )
        if revision is not None and revision.group(1) not in {"unknown", "none"}:
            actual_by_predicate["git.revision"] = revision.group(1).strip()
        if status_match is not None:
            status_text = status_match.group(1).strip()
            actual_by_predicate["git.status"] = status_text
            branch = re.search(r"^##\s+([^\.\s]+)", status_text)
            if branch is not None:
                actual_by_predicate["git.branch"] = branch.group(1)
        if remotes_match is not None:
            remotes = remotes_match.group(1).strip()
            if remotes and remotes != "none":
                actual_by_predicate["git.remote"] = remotes
    elif request.probe == "disk":
        body = output.split("inspect_disk", 1)[-1].strip()
        if body and "command_" not in body:
            actual_by_predicate["disk.capacity"] = body
            actual_by_predicate["disk.filesystems"] = body
    elif request.probe == "path_permissions":
        rows = re.findall(r"(?m)^path=([^\s]+)\s+([^\n]+)$", output)
        if rows:
            existence = ["exists=true" in details for _, details in rows]
            actual_by_predicate["path.exists"] = all(existence)
            actual_by_predicate["path.permissions"] = "\n".join(
                "path=%s %s" % row for row in rows
            )
            uids = re.findall(r"\buid=(\d+)", output)
            gids = re.findall(r"\bgid=(\d+)", output)
            if uids:
                actual_by_predicate["path.uid"] = [int(item) for item in uids]
            if gids:
                actual_by_predicate["path.gid"] = [int(item) for item in gids]
    elif request.probe == "process_detail":
        patterns = {
            "process.cwd": r"\bcwd=([^\s]+)",
            "process.uid": r"\b(?:run_as_uid|uid)=(\d+)",
            "process.python_executable": r"\bpython_executable=([^\s]+)",
            "process.cmdline": r"\bcmdline=(.+)$",
        }
        for predicate, pattern in patterns.items():
            match = re.search(pattern, output, re.M)
            if match is not None and match.group(1).strip() not in {
                "unknown", "unchecked", "truncated",
            }:
                actual_by_predicate[predicate] = match.group(1).strip()
        actual_by_predicate["process.identity"] = bool(
            actual_by_predicate.get("process.cwd")
            and actual_by_predicate.get("process.cmdline")
        )
    elif request.probe in {"running_platforms", "platform_health"}:
        # The domain probes already emit root-bound structured records. Their
        # contents remain raw evidence; presence is enough only for predicates
        # whose requirement asks for a present result.
        for predicate in supported:
            actual_by_predicate[predicate] = bool(output.strip())

    for requirement in request.required_facts:
        actual = actual_by_predicate.get(requirement.predicate)
        observation_status = (
            _comparison_status(requirement, actual)
            if requirement.predicate in supported
            else "unresolved"
        )
        observations.append(FactObservation(
            requirement.fact_id,
            observation_status,
            actual,
            "%s.%s" % (request.probe, requirement.predicate),
        ))
    return tuple(observations)


def _registered_probe_contract_errors(
    requests: list[ProbeRequest],
) -> list[str]:
    """Explain typed predicate mismatches before any registered probe runs."""

    errors = []
    for request in requests:
        if not request.required_facts:
            continue
        spec = DEFAULT_READONLY_PROBES.get(request.probe)
        if spec is None:
            continue
        supported = set(spec.supported_predicates)
        unsupported = sorted({
            item.predicate for item in request.required_facts
            if item.predicate not in supported
        })
        if not unsupported:
            continue
        errors.append(
            "registered probe %s does not cover predicates=%s; supported=%s"
            % (
                request.probe,
                ",".join(unsupported),
                ",".join(sorted(supported)) or "none",
            )
        )
    return errors


def _subject_paths(subject: EvidenceSubject | None) -> tuple[Path, ...]:
    if subject is None or subject.kind not in {"path", "path_set"}:
        return ()
    values = subject.value if isinstance(subject.value, list) else [subject.value]
    result = []
    for value in values:
        path = Path(str(value or ""))
        if path.is_absolute():
            result.append(path.resolve(strict=False))
    return tuple(result)


def _path_within(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.resolve(strict=False).relative_to(root)
            return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def _validate_shell_fallback_contract(
    request: ProbeRequest,
    data: dict[str, Any],
    *,
    unresolved_fact_ids: set[str],
) -> tuple[str, tuple[str, ...], list[dict[str, Any]], str]:
    command = str(data.get("command") or "").strip()
    if not command:
        raise ValueError("empty read-only command candidate")
    covers = tuple(str(item) for item in data.get("covers") or [])
    if (
        not covers
        or len(covers) != len(set(covers))
        or not set(covers).issubset(unresolved_fact_ids)
    ):
        raise ValueError("read-only fallback covers facts outside the active gap")
    supplied_subject = EvidenceSubject.from_value(data.get("subject"))
    if request.subject is not None and supplied_subject != request.subject:
        raise ValueError("read-only fallback changed the evidence subject")
    extractors = [
        dict(item) for item in data.get("extractors") or []
        if isinstance(item, dict)
    ]
    extractor_ids = {str(item.get("fact_id") or "") for item in extractors}
    if set(covers) != extractor_ids:
        raise ValueError("read-only fallback extractor coverage is incomplete")
    if any(
        str(item.get("kind") or "")
        not in {"output_contains", "output_regex", "output_nonempty"}
        for item in extractors
    ):
        raise ValueError("unsupported deterministic read-only extractor")
    requirements = {item.fact_id: item for item in request.required_facts}
    for extractor in extractors:
        requirement = requirements[str(extractor.get("fact_id") or "")]
        kind = str(extractor.get("kind") or "")
        expected = extractor.get("expected")
        if kind == "output_nonempty" and requirement.comparison != "present":
            raise ValueError(
                "output_nonempty can only resolve a present fact requirement"
            )
        if kind == "output_contains":
            if requirement.comparison not in {"contains", "present"}:
                raise ValueError(
                    "output_contains does not match the fact comparison"
                )
            if requirement.comparison == "contains" and expected != requirement.expected:
                raise ValueError(
                    "output_contains changed the expected fact value"
                )
        if kind == "output_regex":
            pattern = str(extractor.get("expected") or "")
            if not pattern or len(pattern) > 500:
                raise ValueError("output_regex requires a bounded pattern")

    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError("invalid read-only command quoting") from exc
    subject_roots = _subject_paths(request.subject)
    scope_roots = tuple(
        Path(item).resolve(strict=False)
        for item in request.scope if Path(item).is_absolute()
    )
    exclusion_roots = tuple(
        Path(item).resolve(strict=False)
        for item in request.exclusions if Path(item).is_absolute()
    )
    argument_paths = [
        Path(token).resolve(strict=False)
        for token in argv[1:] if token.startswith("/")
    ]
    if any(_path_within(path, exclusion_roots) for path in argument_paths):
        raise ValueError("read-only fallback queried an excluded path")
    excluded_components = {
        item.lower() for item in request.exclusions
        if not str(item).startswith("/")
    }
    if "nginx" in excluded_components and re.search(
        r"(?:^|[\s/])nginx(?:[\s/]|$)", command, re.I,
    ):
        raise ValueError("read-only fallback queried excluded component nginx")
    outside = [
        path for path in argument_paths
        if subject_roots and not _path_within(path, subject_roots + scope_roots)
    ]
    expansion_reason = str(data.get("scope_expansion_reason") or "").strip()
    if not subject_roots and argument_paths and not expansion_reason:
        raise ValueError("read-only fallback requires a scope expansion reason")
    if outside and not expansion_reason:
        raise ValueError("read-only fallback expanded path scope without reason")
    # A frozen path subject is authoritative. Expansion must be proposed back
    # to Planner/user by changing the gap scope, never smuggled into Shell.
    if outside and request.subject is not None and request.subject.kind == "path":
        raise ValueError("read-only fallback exceeded the frozen path subject")
    return command, covers, extractors, expansion_reason


def _shell_observations(
    request: ProbeRequest,
    output: str,
    extractors: list[dict[str, Any]],
) -> tuple[FactObservation, ...]:
    observations = []
    requirements = {item.fact_id: item for item in request.required_facts}
    for extractor in extractors:
        fact_id = str(extractor.get("fact_id") or "")
        requirement = requirements[fact_id]
        kind = str(extractor.get("kind") or "")
        expected = str(extractor.get("expected") or "")
        if kind == "output_nonempty":
            actual: Any = str(output or "").strip()
        elif kind == "output_contains":
            actual = (
                str(output or "")
                if expected and expected in str(output or "") else ""
            )
        else:
            try:
                match = re.search(expected, str(output or ""))
            except re.error:
                match = None
            actual = (
                match.group(1)
                if match is not None and match.lastindex
                else match.group(0)
                if match is not None
                else ""
            )
        observations.append(FactObservation(
            fact_id,
            _comparison_status(requirement, actual),
            actual,
            "readonly_command.%s" % kind,
        ))
    return tuple(observations)


def _evidence_user_summary(record: EvidenceRecord) -> str:
    """Summarize evidence without copying raw command output to the UI."""

    by_status = {"confirmed": [], "contradicted": [], "unresolved": []}
    for observation in record.observations:
        by_status.setdefault(observation.status, []).append(observation.fact_id)
    facts = "；".join(
        "%s=%s" % (status, ",".join(by_status[status]) or "none")
        for status in ("confirmed", "contradicted", "unresolved")
        if by_status[status]
    ) or "无结构化 fact 结论"
    return "证据摘要：probe=%s status=%s；%s；原始输出已保存为 %s" % (
        record.request.probe,
        record.status,
        facts,
        record.evidence_id,
    )


def _knowledge_task_type(goal: str) -> str:
    lowered = str(goal or "").lower()
    if any(marker in lowered for marker in (
        "失败", "故障", "异常", "报错", "修复", "恢复", "诊断", "排查",
        "failed", "error", "recover", "diagnose", "troubleshoot",
    )):
        return "troubleshooting"
    return "operation_guide"


_ABSOLUTE_PATH_RE = r"/[A-Za-z0-9_.+@%=-]+(?:/[A-Za-z0-9_.+@%=-]+)+"


def _explicit_deployment_boundaries(goal: str) -> dict[str, str]:
    """Extract only paths explicitly labelled by the user."""

    return extract_labeled_deployment_paths(goal)


def _goal_evidence_exclusions(goal: str) -> tuple[str, ...]:
    text = str(goal or "")
    exclusions = []
    if re.search(
        r"(?:不(?:配置|使用|修改|读取|处理|启动)?|无需|排除)"
        r"[^\n。！？;；]{0,20}\bnginx\b",
        text,
        re.I,
    ):
        exclusions.append("nginx")
    return tuple(exclusions)


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
        self._active_evidence_bundle: EvidenceBundle | None = None

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

    def _seed_explicit_deployment_boundaries(
        self,
        goal: str,
        bundle: EvidenceBundle,
    ) -> None:
        boundaries = _explicit_deployment_boundaries(goal)
        exclusions = _goal_evidence_exclusions(goal)
        for role in ("source_directory", "target_directory"):
            path = boundaries.get(role)
            if not path:
                continue
            fact_id = "fact-user-%s" % role.replace("_", "-")
            request = ProbeRequest(
                "user_decision",
                {role: path},
                "冻结用户明确提供的部署边界",
                ({
                    "fact_id": fact_id,
                    "predicate": "deployment.%s" % role,
                    "expected": path,
                    "comparison": "equals",
                },),
                gap_id="gap-user-deployment-boundaries",
                subject={"kind": "path", "value": path},
                scope=(path,),
                exclusions=exclusions,
            )
            if any(
                record.request.cache_key == request.cache_key
                for record in bundle.records
            ):
                continue
            bundle.add(EvidenceRecord.from_probe(
                request,
                json.dumps({role: path}, ensure_ascii=False),
                observations=(FactObservation(
                    fact_id,
                    "confirmed",
                    path,
                    "user_decision.%s" % role,
                ),),
            ))

        owner = getattr(self.probe_runner, "__self__", None)
        register_user_root = getattr(
            owner, "register_user_decided_project_root", None,
        )
        if callable(register_user_root):
            for path in boundaries.values():
                register_user_root(path)

        source = boundaries.get("source_directory")
        if not source or self.probe_runner is None:
            return
        layout_request = ProbeRequest(
            "project_layout",
            {"project_roots": [source]},
            "检查用户明确提供的源码目录及入口文件",
            (
                {
                    "fact_id": "fact-explicit-source-exists",
                    "predicate": "path.exists",
                    "expected": True,
                    "comparison": "equals",
                },
                {
                    "fact_id": "fact-explicit-source-entry-files",
                    "predicate": "project.entry_files",
                    "expected": [
                        "master_main.py", "worker_main.py",
                        "celery_worker.py", "web_terminal_main.py",
                    ],
                    "comparison": "contains_all",
                },
            ),
            gap_id="gap-explicit-source-layout",
            affected_steps=("prepare-source",),
            subject={"kind": "path", "value": source},
            scope=(source,),
            exclusions=exclusions,
        )
        self.collect_requests([layout_request], bundle)
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

        bounded = list(requests[:4])
        if len(requests) > 4:
            bundle.budget_exhausted = True
        collected: list[EvidenceRecord] = []
        for request in bounded:
            collected.extend(self._collect_request(request, bundle))
        if derive_traceback_sources and any(
            request.probe in {"logs", "process_logs"} for request in bounded
        ):
            self.collect_traceback_source_evidence(bundle)
        if any(request.probe == "running_platforms" for request in bounded):
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

    def run_ad_hoc_requests(self, values: list[dict[str, Any]]) -> str:
        """Expose the same Discovery boundary to Binder and step Verifier."""

        requests = self._requests(values)
        bundle = self._active_evidence_bundle or EvidenceBundle(
            goal="补齐实施或验证所需的只读事实"
        )
        self.collect_requests(requests, bundle)
        return "\n\n".join(
            "evidence_id=%s probe=%s status=%s required_facts=%s\n%s"
            % (
                record.evidence_id,
                record.request.probe,
                record.status,
                ",".join(record.request.covers) or "none",
                record.output[:7000],
            )
            for record in bundle.records
        ) or "no read-only evidence was produced"

    @contextmanager
    def evidence_scope(self, bundle: EvidenceBundle):
        """Make Binder/Verifier probes contribute to the workflow evidence."""

        previous = self._active_evidence_bundle
        self._active_evidence_bundle = bundle
        try:
            yield
        finally:
            self._active_evidence_bundle = previous

    def _collect_request(
        self,
        request: ProbeRequest,
        bundle: EvidenceBundle,
    ) -> list[EvidenceRecord]:
        existing = next(
            (
                item for item in bundle.records
                if item.request.cache_key == request.cache_key
            ),
            None,
        )
        registered = DEFAULT_READONLY_PROBES.get(request.probe) is not None
        records: list[EvidenceRecord] = []
        if registered and (existing is None or request.freshness == "refresh"):
            if self.on_progress is not None:
                self.on_progress("正在收集只读证据：%s" % request.probe)
            try:
                output = (
                    self.probe_runner([{
                        "probe": request.probe,
                        "args": request.args,
                        "purpose": request.purpose,
                    }])
                    if self.probe_runner is not None
                    else "probe runner unavailable"
                )
                status = "available" if self.probe_runner is not None else "unavailable"
            except Exception as exc:
                output = "probe failed: %s" % type(exc).__name__
                status = "unavailable"
            record = EvidenceRecord.from_probe(
                request,
                output,
                status=status,
                observations=_registered_observations(
                    request, output, status=status,
                ),
            )
            record = (
                bundle.refresh(record)
                if request.freshness == "refresh"
                else bundle.add(record)
            )
            self._add_derived_screen_source(bundle, record)
            records.append(record)
            existing = record
        elif existing is not None:
            # The same raw probe result may answer a newly narrowed fact set.
            # Rebind it to the current request and run the same deterministic
            # extractor instead of asking an LLM whether it is sufficient.
            existing = EvidenceRecord(
                evidence_id=existing.evidence_id,
                request=request,
                output=existing.output,
                status=existing.status,
                collected_at=existing.collected_at,
                observations=_registered_observations(
                    request, existing.output, status=existing.status,
                ),
            )
            bundle.refresh(existing)
            records.append(existing)

        unresolved = set(request.covers)
        if existing is not None:
            unresolved = set(existing.unresolved_fact_ids)
        if (
            not registered
            or (existing is not None and existing.status != "available")
            or bool(unresolved)
        ):
            fallback = self._collect_readonly_fallback(
                request,
                bundle,
                prior_record=existing,
                registered=registered,
            )
            if fallback is not None:
                records.append(fallback)
        if self.on_progress is not None:
            for item in records:
                self.on_progress(_evidence_user_summary(item))
        return records

    def _collect_readonly_fallback(
        self,
        request: ProbeRequest,
        bundle: EvidenceBundle,
        *,
        prior_record: EvidenceRecord | None,
        registered: bool,
    ) -> EvidenceRecord | None:
        if self.on_progress is not None:
            self.on_progress(
                "注册 Probe %s%s，正在评估安全只读命令补证。"
                % (
                    request.probe,
                    "未覆盖所需事实" if registered else "不存在",
                )
            )
        prior = (
            prior_record.output[:9000]
            if prior_record is not None
            else "No registered probe output is available."
        )
        prior_fallbacks = [
            {
                "command": str(item.request.args.get("command") or ""),
                "status": item.status,
                "output": item.output[:3000],
            }
            for item in bundle.records
            if item.request.probe == "readonly_command"
            and str(item.request.args.get("source_need_key") or "")
            == request.need_key
        ]
        payload = {
            "requested_capability": request.probe,
            "args": request.args,
            "purpose": request.purpose,
            "gap_id": request.need_key,
            "subject": (
                request.subject.to_dict() if request.subject is not None else None
            ),
            "required_facts": [
                item.to_dict() for item in request.required_facts
                if item.fact_id in set(
                    prior_record.unresolved_fact_ids
                    if prior_record is not None else request.covers
                )
            ],
            "scope": list(request.scope),
            "exclusions": list(request.exclusions),
            "registered_probe_output": prior,
            "prior_fallback_attempts": prior_fallbacks,
        }
        try:
            data = parse_json_object(self._complete([
                {
                    "role": "system",
                    "content": READONLY_FALLBACK_SYSTEM_PROMPT
                    + "\n\n" + klonet_domain_context("discovery"),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                },
            ]))
            status = str(data.get("status") or "").strip().lower()
            if status == "satisfied" and prior_record is not None:
                if not prior_record.unresolved_fact_ids:
                    return None
                raise ValueError(
                    "model claimed satisfied while required facts remain unresolved"
                )
            if status != "command":
                raise ValueError(str(data.get("reason") or "no safe read-only command"))
            unresolved = set(
                prior_record.unresolved_fact_ids
                if prior_record is not None else request.covers
            )
            command, covers, extractors, expansion_reason = (
                _validate_shell_fallback_contract(
                    request,
                    data,
                    unresolved_fact_ids=unresolved,
                )
            )
            if command in {
                str(item.get("command") or "") for item in prior_fallbacks
            }:
                raise ValueError(
                    "read-only fallback repeated a command that made no progress"
                )
            fallback_request = ProbeRequest(
                "readonly_command",
                {
                    "command": command,
                    "source_probe": request.probe,
                    "source_need_key": request.need_key,
                    "covers": list(covers),
                    "extractors": extractors,
                },
                "补齐未被注册 Probe 覆盖的事实：%s" % request.purpose,
                tuple(
                    item for item in request.required_facts
                    if item.fact_id in set(covers)
                ),
                request.freshness,
                request.need_key,
                request.affected_steps,
                request.subject,
                request.scope,
                request.exclusions,
            )
            existing = next(
                (
                    item for item in bundle.records
                    if item.request.cache_key == fallback_request.cache_key
                ),
                None,
            )
            if existing is not None and request.freshness != "refresh":
                return existing
            if self.readonly_command_runner is None:
                raise RuntimeError("read-only command boundary unavailable")
            if self.on_progress is not None:
                if expansion_reason:
                    self.on_progress(
                        "只读补证需要扩大查询范围：%s" % expansion_reason
                    )
                self.on_progress("只读命令候选已生成，正在进行确定性安全校验并执行。")
            output = self.readonly_command_runner(command)
            record = EvidenceRecord.from_probe(
                fallback_request,
                output,
                status="available",
                observations=_shell_observations(
                    fallback_request, output, extractors,
                ),
            )
            return (
                bundle.refresh(record)
                if request.freshness == "refresh"
                else bundle.add(record)
            )
        except Exception as exc:
            failed_request = ProbeRequest(
                "readonly_command",
                {
                    "source_probe": request.probe,
                    "source_need_key": request.need_key,
                },
                "无法安全绑定只读补证：%s" % request.purpose,
                request.required_facts,
                request.freshness,
                request.need_key,
                request.affected_steps,
                request.subject,
                request.scope,
                request.exclusions,
            )
            record = EvidenceRecord.from_probe(
                failed_request,
                "read-only fallback unavailable: %s: %s"
                % (type(exc).__name__, str(exc)[:500]),
                status="unavailable",
            )
            return bundle.add(record)

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
        self._seed_explicit_deployment_boundaries(goal, bundle)
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
                "content": (
                    DISCOVERY_SYSTEM_PROMPT % DEFAULT_READONLY_PROBES.render()
                ) + "\n\n" + klonet_domain_context("discovery"),
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
        predicate_repairs = 0
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
            try:
                requests = self._requests(data.get("probe_requests"))
            except (TypeError, ValueError) as exc:
                if invalid_repairs >= 1:
                    bundle.blocked_reason = "discovery output invalid: %s" % exc
                    return bundle
                invalid_repairs += 1
                messages.append({
                    "role": "user",
                    "content": (
                        "Return one valid Discovery JSON object whose fact "
                        "comparisons are equals, contains, contains_all, or "
                        "present. Error: %s" % exc
                    ),
                })
                continue
            contract_errors = _registered_probe_contract_errors(requests)
            if contract_errors and predicate_repairs < 1:
                predicate_repairs += 1
                messages.append({
                    "role": "user",
                    "content": (
                        "Correct the Probe predicate contract before execution. "
                        "If the required semantic is equivalent to a supported "
                        "predicate, reuse that exact predicate. If it is genuinely "
                        "long-tail, request a clearly named long-tail capability so "
                        "Discovery can bind a read-only Shell implementation.\n"
                        + "\n".join(contract_errors)
                    ),
                })
                continue
            exclusions = _goal_evidence_exclusions(goal)
            if exclusions:
                requests = [
                    replace(
                        request,
                        exclusions=tuple(dict.fromkeys(
                            (*request.exclusions, *exclusions)
                        )),
                    )
                    for request in requests
                ]
            try:
                fresh = budget.register_round(requests)
            except DiscoveryBudgetExceeded:
                bundle.budget_exhausted = True
                return bundle
            evidence_sections = []
            for request in fresh:
                if self.on_progress is not None:
                    self.on_progress("正在执行只读检查：%s" % request.probe)
                for record in self._collect_request(request, bundle):
                    evidence_sections.append(
                        "%s (%s, %s):\n%s"
                        % (
                            record.evidence_id,
                            record.request.probe,
                            record.status,
                            record.output[:7000],
                        )
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
            subject = (
                EvidenceSubject.from_value(item.get("subject"))
                or infer_evidence_subject(normalized_probe, normalized_args)
            )
            request = ProbeRequest(
                probe=normalized_probe,
                args=normalized_args,
                purpose=str(item.get("purpose") or "collect required fact"),
                required_facts=tuple(item.get("required_facts") or []),
                freshness=str(item.get("freshness") or "cached"),
                gap_id=str(item.get("gap_id") or ""),
                affected_steps=tuple(
                    str(value).strip()
                    for value in item.get("affected_steps") or []
                    if str(value).strip()
                ),
                subject=subject,
                scope=tuple(
                    str(value).strip()
                    for value in item.get("scope") or []
                    if str(value).strip()
                ),
                exclusions=tuple(
                    str(value).strip()
                    for value in item.get("exclusions") or []
                    if str(value).strip()
                ),
            )
            if request.need_key in known_keys:
                continue
            known_keys.add(request.need_key)
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
