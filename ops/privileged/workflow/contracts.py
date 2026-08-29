"""High-level contracts for the staged Ops-Privilege workflow."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from pathlib import Path
import re

from klonet_agent.ops.privileged.contracts import (
    ExecutionBinding,
    ExecutionEvidence,
    ImplementationPlan,
    PlanResource,
    RISK_LEVELS,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


FACT_COMPARISONS = {"equals", "contains", "contains_all", "present"}
FACT_OBSERVATION_STATUSES = {"confirmed", "contradicted", "unresolved"}


_DEPLOYMENT_PATH_PATTERN = (
    r"/[A-Za-z0-9_.+@%=-]+(?:/[A-Za-z0-9_.+@%=-]+)+"
)


def extract_labeled_deployment_paths(text: str) -> dict[str, str]:
    """Return deployment paths that the user explicitly assigned a role.

    This is the single ingestion boundary for source/target path wording.  The
    returned values are user decisions, not discoverable runtime facts.
    """

    value = str(text or "")
    labels = {
        "source_directory": (
            r"(?:源码模板(?:目录)?|源码(?:来源|路径|目录)|"
            r"source(?:\s+(?:template|directory|path))?)"
        ),
        "target_directory": (
            r"(?:目标(?:实例)?(?:根)?目录|部署目录|实例目录|"
            r"target(?:\s+(?:directory|path))?)"
        ),
    }
    result: dict[str, str] = {}
    assignment = (
        r"\s*(?:(?:固定\s*)?(?:使用|采用|选用|取自|来自|设为|为|是)?"
        r"\s*[:：=]?\s*)`?(" + _DEPLOYMENT_PATH_PATTERN + r")"
    )
    for role, label in labels.items():
        match = re.search(label + assignment, value, re.I)
        if match is not None:
            result[role] = match.group(1).rstrip("/.,;:，；。`").rstrip("/") or "/"

    if "source_directory" not in result:
        source_match = re.search(
            r"(?:从|from)\s*`?(" + _DEPLOYMENT_PATH_PATTERN + r")`?"
            r"[^\n。；;]{0,40}(?:复制|同步|copy|sync)",
            value,
            re.I,
        )
        if source_match is not None:
            result["source_directory"] = (
                source_match.group(1).rstrip("/.,;:，；。`").rstrip("/") or "/"
            )
    return result


def _stable_fact_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    if not normalized:
        normalized = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return "fact-" + normalized[:120]


@dataclass(frozen=True)
class EvidenceSubject:
    """The exact object an evidence gap is allowed to inspect."""

    kind: str
    value: Any

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", str(self.kind or "")):
            raise ValueError("invalid evidence subject kind")
        if self.value in (None, "", [], {}):
            raise ValueError("evidence subject value is required")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}

    @classmethod
    def from_value(cls, value: Any) -> "EvidenceSubject | None":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        kind = str(value.get("kind") or "").strip()
        subject_value = value.get("value")
        return cls(kind, subject_value) if kind and subject_value not in (None, "") else None


@dataclass(frozen=True)
class FactRequirement:
    """One stable, deterministically comparable fact required by a gap."""

    fact_id: str
    predicate: str
    expected: Any = True
    comparison: str = "equals"
    freshness: str = "cached"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"fact-[-A-Za-z0-9_.:]{3,160}", self.fact_id):
            raise ValueError("invalid fact id")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,95}", self.predicate):
            raise ValueError("invalid fact predicate")
        if self.comparison not in FACT_COMPARISONS:
            raise ValueError("invalid fact comparison")
        if self.freshness not in {"cached", "refresh"}:
            raise ValueError("invalid fact freshness")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "predicate": self.predicate,
            "expected": self.expected,
            "comparison": self.comparison,
            "freshness": self.freshness,
        }

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        freshness: str = "cached",
    ) -> "FactRequirement":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            predicate = str(value.get("predicate") or "").strip()
            raw_fact_id = str(value.get("fact_id") or "").strip()
            fact_id = raw_fact_id
            if not fact_id and predicate:
                fact_id = _stable_fact_id(predicate)
            elif not re.fullmatch(r"fact-[-A-Za-z0-9_.:]{3,160}", fact_id):
                # Normalize provider-generated identifiers once at ingress;
                # the runtime protocol and persisted form remain canonical.
                fact_id = _stable_fact_id(raw_fact_id or predicate)
            comparison = str(value.get("comparison") or "equals")
            expected = value.get("expected", True)
            if comparison == "contains_all" and isinstance(expected, str):
                expected = [
                    item.strip() for item in expected.split(",")
                    if item.strip()
                ]
            if predicate.startswith("port.") and isinstance(expected, list):
                expected = [
                    int(item) if str(item).isdigit() else item
                    for item in expected
                ]
            return cls(
                fact_id=fact_id,
                predicate=predicate,
                expected=expected,
                comparison=comparison,
                freshness=str(value.get("freshness") or freshness),
            )
        # Persisted pre-structured requests are normalized once at ingress.
        # Runtime code only sees FactRequirement; serializers never emit the
        # legacy string protocol again.
        text = str(value or "").strip()
        lowered = text.lower()
        if "port" in lowered and any(word in lowered for word in ("avail", "free", "空闲", "可用")):
            predicate = "port.available"
        elif any(word in lowered for word in ("source_directory", "source path", "源码路径", "path exists")):
            predicate = "path.exists"
        elif any(word in lowered for word in ("entry_files", "entry files", "入口文件")):
            predicate = "project.entry_files"
        elif lowered in {"cwd", "master cwd", "worker cwd", "process cwd"}:
            predicate = "process.cwd"
        elif any(word in lowered for word in ("run_as_uid", "process uid", "运行用户")) or lowered == "uid":
            predicate = "process.uid"
        elif "python executable" in lowered or "python解释器" in lowered:
            predicate = "process.python_executable"
        elif "cmdline" in lowered or "启动参数" in lowered:
            predicate = "process.cmdline"
        elif "platform_health" in lowered or "platform health" in lowered or "平台健康" in lowered:
            predicate = "platform.health"
        elif any(word in lowered for word in ("runtime instance", "project roots", "平台实例")):
            predicate = "platform.inventory"
        elif any(word in lowered for word in ("managed component roles", "runtime roles", "运行角色")):
            predicate = "runtime.roles"
        elif any(word in lowered for word in ("runtime identit", "进程身份")):
            predicate = "runtime.identity"
        elif any(word in lowered for word in ("configured component ports", "runtime ports", "配置端口")):
            predicate = "runtime.ports"
        elif any(word in lowered for word in ("startup contract", "启动合同")):
            predicate = "runtime.startup_contract"
        else:
            predicate = re.sub(r"[^a-z0-9]+", ".", lowered).strip(".")
        if not predicate or "." not in predicate:
            predicate = "legacy." + (predicate or "fact")
        return cls(
            fact_id=_stable_fact_id(text),
            predicate=predicate[:96],
            expected=True,
            comparison="present",
            freshness=freshness,
        )


@dataclass(frozen=True)
class FactObservation:
    fact_id: str
    status: str
    value: Any = None
    extractor: str = ""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"fact-[-A-Za-z0-9_.:]{3,160}", self.fact_id):
            raise ValueError("invalid observed fact id")
        if self.status not in FACT_OBSERVATION_STATUSES:
            raise ValueError("invalid fact observation status")
        if not str(self.extractor or "").strip():
            raise ValueError("fact observation requires extractor")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "status": self.status,
            "value": self.value,
            "extractor": self.extractor,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FactObservation":
        return cls(
            fact_id=str(value.get("fact_id") or ""),
            status=str(value.get("status") or "unresolved"),
            value=value.get("value"),
            extractor=str(value.get("extractor") or "persisted"),
        )


@dataclass(frozen=True)
class ProbeRequest:
    probe: str
    args: dict[str, Any]
    purpose: str
    required_facts: tuple[FactRequirement, ...] = ()
    freshness: str = "cached"
    gap_id: str = ""
    affected_steps: tuple[str, ...] = ()
    subject: EvidenceSubject | None = None
    scope: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.probe or "").strip():
            raise ValueError("probe is required")
        if not isinstance(self.args, dict):
            raise ValueError("probe args must be an object")
        if self.freshness not in {"cached", "refresh"}:
            raise ValueError("probe freshness must be cached or refresh")
        normalized_facts = tuple(
            FactRequirement.from_value(item, freshness=self.freshness)
            for item in self.required_facts
        )
        fact_ids = [item.fact_id for item in normalized_facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("required_facts must use unique fact ids")
        object.__setattr__(self, "required_facts", normalized_facts)
        normalized_subject = EvidenceSubject.from_value(self.subject)
        inferred_subject = infer_evidence_subject(self.probe, self.args)
        if normalized_subject is None:
            normalized_subject = inferred_subject
        elif inferred_subject is not None and normalized_subject != inferred_subject:
            raise ValueError("probe args do not inspect the declared evidence subject")
        if normalized_subject is not self.subject:
            object.__setattr__(self, "subject", normalized_subject)
        if self.gap_id and not re.fullmatch(r"gap-[-A-Za-z0-9_.:]{3,160}", self.gap_id):
            raise ValueError("invalid evidence gap id")
        if any(not str(item or "").strip() for item in self.affected_steps):
            raise ValueError("affected_steps cannot contain empty values")
        if any(not str(item or "").strip() for item in (*self.scope, *self.exclusions)):
            raise ValueError("evidence scope cannot contain empty values")

    @property
    def cache_key(self) -> str:
        payload = {
            "probe": self.probe.strip(),
            "args": self.args,
            "subject": self.subject.to_dict() if self.subject is not None else None,
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def need_key(self) -> str:
        """Identify one stable semantic gap independently of prompt wording."""

        return self.gap_id or "gap-" + self.cache_key[:24]

    @property
    def request_id(self) -> str:
        return "probe-" + self.cache_key[:20]

    @property
    def covers(self) -> tuple[str, ...]:
        return tuple(item.fact_id for item in self.required_facts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "probe": self.probe,
            "args": self.args,
            "purpose": self.purpose,
            "required_facts": [item.to_dict() for item in self.required_facts],
            "covers": list(self.covers),
            "freshness": self.freshness,
            "gap_id": self.gap_id,
            "affected_steps": list(self.affected_steps),
            "subject": self.subject.to_dict() if self.subject is not None else None,
            "scope": list(self.scope),
            "exclusions": list(self.exclusions),
        }


def normalize_probe_request(
    probe: str,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Route source/config paths to the bounded operational file reader."""

    normalized_probe = str(probe or "").strip()
    normalized_args = dict(args)
    for key in (
        "project_roots", "roots", "paths", "ports", "pids",
        "services", "modules", "keywords",
    ):
        raw = normalized_args.get(key)
        if raw in (None, "", [], {}):
            continue
        if not isinstance(raw, list):
            normalized_args[key] = [
                item.strip() for item in str(raw).split(",") if item.strip()
            ]
    path = str(normalized_args.get("path") or "").strip()
    if normalized_probe == "logs" and Path(path).suffix.lower() in {
        ".py", ".cfg", ".conf", ".ini", ".json", ".toml", ".yaml", ".yml",
    }:
        normalized_probe = "ops_file"
        normalized_args.setdefault("view", "head")
        normalized_args.setdefault("max_chars", 20000)
    return normalized_probe, normalized_args


def infer_evidence_subject(
    probe: str,
    args: dict[str, Any],
) -> EvidenceSubject | None:
    """Derive a bounded subject from typed probe arguments when omitted."""

    mappings = (
        (("path", "project_root", "repository", "script_dir"), "path"),
        (("project_roots", "roots", "paths"), "path_set"),
        (("ports",), "port_set"),
        (("pids",), "pid_set"),
        (("session",), "screen_session"),
        (("services",), "service_set"),
    )
    for keys, kind in mappings:
        for key in keys:
            value = args.get(key)
            if value not in (None, "", [], {}):
                if kind.endswith("_set") and not isinstance(value, list):
                    value = [value]
                if kind == "path_set" and isinstance(value, list) and len(value) == 1:
                    return EvidenceSubject("path", value[0])
                return EvidenceSubject(kind, value)
    if str(probe or "").strip() == "running_platforms":
        return EvidenceSubject("host_runtime", "klonet_platforms")
    return None


def normalize_instance_alias(value: str) -> str:
    """Normalize display aliases without weakening project-root identity."""

    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return normalized[6:] if normalized.startswith("klonet") else normalized


@dataclass(frozen=True)
class ResolvedPlatformIdentity:
    project_root: str
    primary_alias: str
    aliases: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not Path(self.project_root).is_absolute():
            raise ValueError("resolved platform root must be absolute")
        if not self.primary_alias or not self.evidence_refs:
            raise ValueError("resolved platform identity requires alias and evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "primary_alias": self.primary_alias,
            "aliases": list(self.aliases),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    request: ProbeRequest
    output: str
    status: str = "available"
    collected_at: str = field(default_factory=_utc_now)
    observations: tuple[FactObservation, ...] = ()

    @classmethod
    def from_probe(
        cls,
        request: ProbeRequest,
        output: str,
        *,
        status: str = "available",
        observations: tuple[FactObservation, ...] | list[FactObservation] = (),
    ) -> "EvidenceRecord":
        return cls(
            evidence_id="ev-" + request.cache_key[:16],
            request=request,
            output=str(output or ""),
            status=status,
            observations=tuple(observations),
        )

    @property
    def unresolved_fact_ids(self) -> tuple[str, ...]:
        by_id = {item.fact_id: item.status for item in self.observations}
        return tuple(
            requirement.fact_id
            for requirement in self.request.required_facts
            if by_id.get(requirement.fact_id) not in {"confirmed", "contradicted"}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "request": self.request.to_dict(),
            "output": self.output,
            "status": self.status,
            "collected_at": self.collected_at,
            "observations": [item.to_dict() for item in self.observations],
        }


@dataclass(frozen=True)
class EvidenceGapResolution:
    gap_id: str
    confirmed_fact_ids: tuple[str, ...] = ()
    contradicted_fact_ids: tuple[str, ...] = ()
    unresolved_fact_ids: tuple[str, ...] = ()

    @property
    def progress_key(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return self.unresolved_fact_ids, self.contradicted_fact_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "confirmed_fact_ids": list(self.confirmed_fact_ids),
            "contradicted_fact_ids": list(self.contradicted_fact_ids),
            "unresolved_fact_ids": list(self.unresolved_fact_ids),
        }


@dataclass
class EvidenceBundle:
    goal: str
    records: list[EvidenceRecord] = field(default_factory=list)
    budget_exhausted: bool = False
    blocked_reason: str = ""

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        existing = next(
            (
                item
                for item in self.records
                if item.request.cache_key == record.request.cache_key
            ),
            None,
        )
        if existing is not None:
            return existing
        self.records.append(record)
        return record

    def refresh(self, record: EvidenceRecord) -> EvidenceRecord:
        """Replace one volatile observation without creating a parallel fact path."""

        for index, existing in enumerate(self.records):
            if existing.request.cache_key == record.request.cache_key:
                self.records[index] = record
                return record
        self.records.append(record)
        return record

    @property
    def evidence_ids(self) -> set[str]:
        return {item.evidence_id for item in self.records}

    @property
    def knowledge_records(self) -> list[EvidenceRecord]:
        return [
            item for item in self.records
            if item.request.probe == "klonet_knowledge"
        ]

    def resolve_gap(self, gap_id: str) -> EvidenceGapResolution:
        """Resolve one evidence gap by stable fact identity, never prose."""

        relevant = [
            item for item in self.records
            if item.request.need_key == str(gap_id or "")
        ]
        requirements: dict[str, FactRequirement] = {}
        latest: dict[str, FactObservation] = {}
        for record in relevant:
            requirements.update({
                item.fact_id: item for item in record.request.required_facts
            })
            for observation in record.observations:
                if observation.fact_id in record.request.covers:
                    latest[observation.fact_id] = observation
        confirmed = []
        contradicted = []
        unresolved = []
        for fact_id in requirements:
            status = latest.get(fact_id)
            if status is not None and status.status == "confirmed":
                confirmed.append(fact_id)
            elif status is not None and status.status == "contradicted":
                contradicted.append(fact_id)
            else:
                unresolved.append(fact_id)
        return EvidenceGapResolution(
            gap_id=str(gap_id or ""),
            confirmed_fact_ids=tuple(sorted(confirmed)),
            contradicted_fact_ids=tuple(sorted(contradicted)),
            unresolved_fact_ids=tuple(sorted(unresolved)),
        )

    def gap_resolutions(self) -> dict[str, EvidenceGapResolution]:
        gap_ids = {
            item.request.need_key for item in self.records
            if item.request.required_facts
        }
        return {gap_id: self.resolve_gap(gap_id) for gap_id in sorted(gap_ids)}


@dataclass(frozen=True)
class RuntimeComponentSpec:
    name: str
    category: str = "application"
    managed: bool = True
    default_restart: bool = True
    screen_suffix: str = ""
    command_argv: tuple[str, ...] = ()
    preflight_argv: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()
    health_checks: tuple[dict[str, Any], ...] = ()
    start_after: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", self.name):
            raise ValueError("invalid runtime component name")
        if self.category not in {"application", "shared_dependency"}:
            raise ValueError("invalid runtime component category")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", self.screen_suffix,
        ):
            raise ValueError("invalid runtime component screen suffix")
        if any(not 1 <= int(port) <= 65535 for port in self.ports):
            raise ValueError("invalid runtime component port")

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, evidence_refs: tuple[str, ...] = (),
    ) -> "RuntimeComponentSpec":
        return cls(
            name=str(data.get("name") or ""),
            category=str(data.get("category") or "application"),
            managed=bool(data.get("managed", True)),
            default_restart=bool(data.get("default_restart", True)),
            screen_suffix=str(data.get("screen_suffix") or data.get("name") or ""),
            command_argv=tuple(str(item) for item in data.get("command_argv") or []),
            preflight_argv=tuple(str(item) for item in data.get("preflight_argv") or []),
            ports=tuple(int(item) for item in data.get("ports") or []),
            health_checks=tuple(
                dict(item) for item in data.get("health_checks") or []
                if isinstance(item, dict)
            ),
            start_after=tuple(str(item) for item in data.get("start_after") or []),
            evidence_refs=evidence_refs,
        )


@dataclass(frozen=True)
class EvidenceClaim:
    text: str
    evidence_refs: list[str]

    def __post_init__(self) -> None:
        if not str(self.text or "").strip():
            raise ValueError("evidence claim text is required")
        if not self.evidence_refs:
            raise ValueError("evidence claim requires evidence_refs")


DIAGNOSIS_STATUSES = {
    "not_applicable",
    "incomplete",
    "symptom_confirmed",
    "cause_confirmed",
    "no_failure_confirmed",
}


@dataclass(frozen=True)
class DiagnosisAssessment:
    """Structured diagnostic progress; completion is never inferred from prose."""

    status: str = "not_applicable"
    symptom: str = ""
    failure_point: str = ""
    root_cause: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in DIAGNOSIS_STATUSES:
            raise ValueError("invalid diagnosis status: %s" % self.status)
        if self.status == "symptom_confirmed" and not self.symptom.strip():
            raise ValueError("symptom_confirmed requires symptom")
        if self.status == "cause_confirmed":
            if not self.symptom.strip():
                raise ValueError("cause_confirmed requires symptom")
            if not self.failure_point.strip():
                raise ValueError("cause_confirmed requires failure_point")
            if not self.root_cause.strip():
                raise ValueError("cause_confirmed requires root_cause")
        if self.status in {
            "symptom_confirmed", "cause_confirmed", "no_failure_confirmed",
        } and not self.evidence_refs:
            raise ValueError("confirmed diagnosis requires evidence_refs")


@dataclass
class EvidenceConclusion:
    confirmed_facts: list[EvidenceClaim] = field(default_factory=list)
    uncertainties: list[EvidenceClaim] = field(default_factory=list)
    missing_decisions: list[str] = field(default_factory=list)
    diagnosis: DiagnosisAssessment = field(default_factory=DiagnosisAssessment)
    resolved_gaps: dict[str, list[str]] = field(default_factory=dict)
    unresolved_gaps: list[str] = field(default_factory=list)

    def validate_against(self, bundle: EvidenceBundle) -> None:
        known = bundle.evidence_ids
        for claim in [*self.confirmed_facts, *self.uncertainties]:
            unknown = [ref for ref in claim.evidence_refs if ref not in known]
            if unknown:
                raise ValueError(
                    "unknown evidence reference: %s" % ", ".join(unknown)
                )
        unknown_diagnosis_refs = [
            ref for ref in self.diagnosis.evidence_refs if ref not in known
        ]
        if unknown_diagnosis_refs:
            raise ValueError(
                "unknown diagnosis evidence reference: %s"
                % ", ".join(unknown_diagnosis_refs)
            )
        for gap_id, refs in self.resolved_gaps.items():
            if not str(gap_id).startswith("gap-"):
                raise ValueError("invalid resolved evidence gap id")
            unknown = [ref for ref in refs if ref not in known]
            if unknown:
                raise ValueError(
                    "unknown resolved gap evidence reference: %s"
                    % ", ".join(unknown)
                )


GOAL_OUTCOME_STATUSES = {
    "achieved",
    "need_evidence",
    "need_execution",
    "need_replan",
    "needs_user_decision",
    "blocked",
}


@dataclass(frozen=True)
class GoalOutcome:
    """The sole goal-level completion and next-transition contract."""

    status: str
    reason: str = ""
    evidence_requests: list[ProbeRequest] = field(default_factory=list)
    user_question: str = ""
    failed_criteria: list[str] = field(default_factory=list)
    next_objective: str = ""
    plan: ChangePlan | None = None
    candidate_plan: ChangePlan | None = None
    missing_decisions: list[str] = field(default_factory=list)
    replan_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in GOAL_OUTCOME_STATUSES:
            raise ValueError("invalid goal outcome status: %s" % self.status)
        if self.status == "need_evidence" and not self.evidence_requests:
            raise ValueError("need_evidence requires evidence_requests")
        if self.status == "needs_user_decision" and not self.user_question:
            raise ValueError("needs_user_decision requires user_question")
        if self.status == "need_execution" and self.plan is None:
            raise ValueError("need_execution requires plan")


class DiscoveryBudgetExceeded(RuntimeError):
    """A bounded Discovery workflow exhausted its safe request budget."""


@dataclass
class DiscoveryBudget:
    max_rounds: int = 3
    max_per_round: int = 4
    max_total_probes: int = 10
    rounds_used: int = 0
    seen_keys: set[str] = field(default_factory=set)

    def register_round(self, requests: list[ProbeRequest]) -> list[ProbeRequest]:
        if self.rounds_used >= self.max_rounds:
            raise DiscoveryBudgetExceeded("discovery round budget exhausted")
        unique: list[ProbeRequest] = []
        round_keys: set[str] = set()
        for request in requests:
            key = request.need_key
            if key in self.seen_keys or key in round_keys:
                continue
            round_keys.add(key)
            unique.append(request)
        if len(unique) > self.max_per_round:
            raise DiscoveryBudgetExceeded("per-round probe budget exceeded")
        if len(self.seen_keys) + len(unique) > self.max_total_probes:
            raise DiscoveryBudgetExceeded("total probe budget exceeded")
        self.rounds_used += 1
        self.seen_keys.update(round_keys)
        return unique


@dataclass
class ChangeStep:
    step_id: str
    title: str
    objective: str
    risk: str
    expected_changes: list[str]
    postconditions: list[dict[str, Any]]
    reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    execution_binding: ExecutionBinding | None = None
    implementation_plan: ImplementationPlan | None = None
    status: str = "pending"
    observation: str = ""
    execution_attempts: int = 0
    evidence: ExecutionEvidence | None = None

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValueError("invalid risk: %s" % self.risk)
        if self.risk == "readonly":
            raise ValueError("ChangeStep cannot be readonly")
        if not self.expected_changes:
            raise ValueError("ChangeStep requires expected_changes")
        if not self.postconditions:
            raise ValueError("ChangeStep requires postconditions")
        if self.execution_binding is not None and self.implementation_plan is not None:
            raise ValueError(
                "ChangeStep cannot have both a direct binding and implementation plan"
            )

    def executable_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "objective": self.objective,
            "reason": self.reason,
            "evidence_refs": self.evidence_refs,
            "depends_on": self.depends_on,
            "risk": self.risk,
            "expected_changes": self.expected_changes,
            "postconditions": self.postconditions,
            "execution_binding": self.execution_binding.executable_dict()
            if self.execution_binding is not None
            else None,
            "implementation_plan": self.implementation_plan.executable_dict()
            if self.implementation_plan is not None
            else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.executable_dict(),
            "execution_binding": self.execution_binding.to_dict()
            if self.execution_binding is not None
            else None,
            "implementation_plan": self.implementation_plan.to_dict()
            if self.implementation_plan is not None
            else None,
            "status": self.status,
            "observation": self.observation,
            "execution_attempts": self.execution_attempts,
            "evidence": self.evidence.to_dict() if self.evidence is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangeStep":
        values = dict(data)
        values["execution_binding"] = ExecutionBinding.from_dict(
            values.get("execution_binding")
        )
        values["implementation_plan"] = ImplementationPlan.from_dict(
            values.get("implementation_plan")
        )
        values["evidence"] = ExecutionEvidence.from_dict(values.get("evidence"))
        return cls(**values)


CHANGE_PLAN_STATUSES = {
    "draft",
    "awaiting_confirmation",
    "approved",
    "executing",
    "verifying",
    "completed",
    "paused",
    "blocked",
    "failed",
    "aborted",
}


@dataclass(frozen=True)
class RecoveryOption:
    option_id: str
    label: str
    description: str
    action: str
    recommended: bool = False
    requires_new_approval: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", self.option_id):
            raise ValueError("invalid recovery option id")
        if self.action not in {
            "continue_current_goal", "provide_direction", "cancel",
        }:
            raise ValueError("invalid recovery option action")
        if not self.label.strip() or not self.description.strip():
            raise ValueError("recovery option requires label and description")

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "description": self.description,
            "action": self.action,
            "recommended": self.recommended,
            "requires_new_approval": self.requires_new_approval,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecoveryOption":
        return cls(
            option_id=str(data.get("option_id") or ""),
            label=str(data.get("label") or ""),
            description=str(data.get("description") or ""),
            action=str(data.get("action") or ""),
            recommended=bool(data.get("recommended", False)),
            requires_new_approval=bool(data.get("requires_new_approval", True)),
        )


@dataclass
class FailureRecord:
    failure_id: str
    stage: str
    category: str
    summary: str
    technical_reason: str
    environment_changed: str = "false"
    failed_step: str = ""
    completed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    failed_checks: list[str] = field(default_factory=list)
    attempted_recoveries: list[str] = field(default_factory=list)
    automatic_recovery_exhausted: bool = True
    options: list[RecoveryOption] = field(default_factory=list)
    selected_option_id: str = ""
    user_direction: str = ""
    created_at: str = field(default_factory=_utc_now)
    goal: str = ""
    goal_kind: str = ""
    plan_id: str = ""
    evidence_requests: list[ProbeRequest] = field(default_factory=list)
    missing_decisions: list[str] = field(default_factory=list)
    semantic_step_id: str = ""
    atomic_step_index: int = -1

    def __post_init__(self) -> None:
        if not re.fullmatch(r"failure-[A-Za-z0-9_-]{1,64}", self.failure_id):
            raise ValueError("invalid failure id")
        if self.stage not in {
            "discovery", "synthesis", "planning", "binding", "execution",
            "verification",
        }:
            raise ValueError("invalid failure stage")
        if self.environment_changed not in {"true", "false", "unknown"}:
            raise ValueError("invalid environment_changed state")
        if self.goal_kind not in {"execution", "health_check", "causal_diagnosis"}:
            raise ValueError("invalid failure goal kind")
        if not self.summary.strip() or not self.technical_reason.strip():
            raise ValueError("failure outcome requires reasons")
        if not self.options:
            raise ValueError("failure outcome requires recovery options")
        if self.atomic_step_index < -1:
            raise ValueError("invalid failure atomic step index")
        if self.semantic_step_id and self.stage != "binding":
            raise ValueError("binding cursor can only be reported at binding stage")

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_id": self.failure_id,
            "stage": self.stage,
            "category": self.category,
            "summary": self.summary,
            "technical_reason": self.technical_reason,
            "environment_changed": self.environment_changed,
            "failed_step": self.failed_step,
            "completed_steps": list(self.completed_steps),
            "skipped_steps": list(self.skipped_steps),
            "failed_checks": list(self.failed_checks),
            "attempted_recoveries": list(self.attempted_recoveries),
            "automatic_recovery_exhausted": self.automatic_recovery_exhausted,
            "options": [item.to_dict() for item in self.options],
            "selected_option_id": self.selected_option_id,
            "user_direction": self.user_direction,
            "created_at": self.created_at,
            "goal": self.goal,
            "goal_kind": self.goal_kind,
            "plan_id": self.plan_id,
            "evidence_requests": [
                item.to_dict()
                for item in self.evidence_requests
            ],
            "missing_decisions": list(self.missing_decisions),
            "semantic_step_id": self.semantic_step_id,
            "atomic_step_index": self.atomic_step_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FailureRecord | None":
        if not isinstance(data, dict):
            return None
        return cls(
            failure_id=str(data.get("failure_id") or ""),
            stage=str(data.get("stage") or ""),
            category=str(data.get("category") or ""),
            summary=str(data.get("summary") or ""),
            technical_reason=str(data.get("technical_reason") or ""),
            environment_changed=str(data.get("environment_changed") or "false"),
            failed_step=str(data.get("failed_step") or ""),
            completed_steps=[str(item) for item in data.get("completed_steps") or []],
            skipped_steps=[str(item) for item in data.get("skipped_steps") or []],
            failed_checks=[str(item) for item in data.get("failed_checks") or []],
            attempted_recoveries=[
                str(item) for item in data.get("attempted_recoveries") or []
            ],
            automatic_recovery_exhausted=bool(
                data.get("automatic_recovery_exhausted", True)
            ),
            options=[
                RecoveryOption.from_dict(item) for item in data.get("options") or []
                if isinstance(item, dict)
            ],
            selected_option_id=str(data.get("selected_option_id") or ""),
            user_direction=str(data.get("user_direction") or ""),
            created_at=str(data.get("created_at") or _utc_now()),
            goal=str(data.get("goal") or ""),
            goal_kind=str(data.get("goal_kind") or ""),
            plan_id=str(data.get("plan_id") or ""),
            evidence_requests=[
                ProbeRequest(
                    str(item.get("probe") or ""),
                    dict(item.get("args") or {}),
                    str(item.get("purpose") or "补齐失败恢复所需事实"),
                    tuple(item.get("required_facts") or []),
                    str(item.get("freshness") or "cached"),
                    str(item.get("gap_id") or ""),
                    tuple(str(value) for value in item.get("affected_steps") or []),
                    EvidenceSubject.from_value(item.get("subject")),
                    tuple(str(value) for value in item.get("scope") or []),
                    tuple(str(value) for value in item.get("exclusions") or []),
                )
                for item in data.get("evidence_requests") or []
                if isinstance(item, dict)
            ],
            missing_decisions=[
                str(item) for item in data.get("missing_decisions") or []
                if str(item).strip()
            ],
            semantic_step_id=str(data.get("semantic_step_id") or ""),
            atomic_step_index=int(
                data.get("atomic_step_index")
                if data.get("atomic_step_index") is not None else -1
            ),
        )


@dataclass
class ChangePlan:
    plan_id: str
    goal: str
    risk: str
    steps: list[ChangeStep]
    resources: list[PlanResource] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    schema_version: int = 4
    status: str = "draft"
    authorized_hash: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    failure: FailureRecord | None = None
    binding_cursor: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.plan_id.startswith("priv-ops-"):
            raise ValueError("change plan id must start with priv-ops-")
        if self.schema_version != 4:
            raise ValueError("change plan schema_version must be 4")
        if self.risk not in RISK_LEVELS or self.risk == "readonly":
            raise ValueError("change plan requires mutating risk")
        if not self.steps:
            raise ValueError("change plan requires steps")
        if self.status not in CHANGE_PLAN_STATUSES:
            raise ValueError("invalid change plan status: %s" % self.status)
        if self.binding_cursor and str(
            self.binding_cursor.get("phase") or ""
        ) != "binding":
            raise ValueError("invalid change plan binding cursor")
        if self.binding_cursor:
            if not str(self.binding_cursor.get("semantic_step_id") or ""):
                raise ValueError("binding cursor requires semantic_step_id")
            if int(self.binding_cursor.get("atomic_step_index", -1)) < 0:
                raise ValueError("binding cursor requires atomic_step_index")
        # The semantic ChangePlan is the sole owner of its resource manifest.
        # Normalize byte-for-byte equivalent declarations here so persisted
        # drafts and every caller observe the same invariant before Binding.
        canonical: list[PlanResource] = []
        by_name: dict[str, PlanResource] = {}
        identity_fields = (
            "kind", "status", "role", "value", "source", "reason",
            "resolve_before",
        )
        for resource in self.resources:
            existing = by_name.get(resource.name)
            if existing is None:
                resource.consumers = list(dict.fromkeys(resource.consumers))
                by_name[resource.name] = resource
                canonical.append(resource)
                continue
            if any(
                getattr(existing, field) != getattr(resource, field)
                for field in identity_fields
            ):
                raise ValueError(
                    "conflicting duplicate plan resource name: %s"
                    % resource.name
                )
            existing.consumers = list(dict.fromkeys([
                *existing.consumers,
                *resource.consumers,
            ]))
        self.resources = canonical
        owners: dict[str, str] = {}
        for resource in self.resources:
            for consumer in resource.consumers:
                previous = owners.setdefault(consumer, resource.name)
                if previous != resource.name:
                    raise ValueError(
                        "plan resource consumer has multiple owners: %s"
                        % consumer
                    )

    @classmethod
    def new(
        cls,
        *,
        goal: str,
        risk: str,
        steps: list[ChangeStep],
        resources: list[PlanResource] | None = None,
        assumptions: list[str] | None = None,
    ) -> "ChangePlan":
        return cls(
            plan_id="priv-ops-" + uuid.uuid4().hex[:10],
            goal=goal,
            risk=risk,
            steps=steps,
            resources=list(resources or []),
            assumptions=list(assumptions or []),
        )

    @property
    def content_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "risk": self.risk,
            "resources": [item.to_dict() for item in self.resources],
            "assumptions": self.assumptions,
            "steps": [item.executable_dict() for item in self.steps],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def is_authorized(self) -> bool:
        return bool(self.authorized_hash) and self.authorized_hash == self.content_hash

    @property
    def completion_gaps(self) -> list[str]:
        """Return the incomplete nodes in the one authoritative plan tree."""

        gaps: list[str] = []
        for change in self.steps:
            if change.status == "skipped":
                continue
            if change.status != "completed":
                gaps.append("change:%s=%s" % (change.step_id, change.status))
            implementation = change.implementation_plan
            if implementation is None:
                continue
            if implementation.status != "completed":
                gaps.append(
                    "implementation:%s=%s"
                    % (implementation.implementation_id, implementation.status)
                )
            for step in implementation.steps:
                if step.status not in {"completed", "skipped"}:
                    gaps.append("step:%s=%s" % (step.step_id, step.status))
        return gaps

    @property
    def execution_is_complete(self) -> bool:
        """Whether every non-skipped node in the approved plan is complete."""

        return not self.completion_gaps

    def authorize(self) -> None:
        self.authorized_hash = self.content_hash
        self.status = "approved"
        self.updated_at = _utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "goal": self.goal,
            "risk": self.risk,
            "steps": [item.to_dict() for item in self.steps],
            "resources": [item.to_dict() for item in self.resources],
            "assumptions": self.assumptions,
            "status": self.status,
            "authorized_hash": self.authorized_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "failure": self.failure.to_dict() if self.failure is not None else None,
            "binding_cursor": dict(self.binding_cursor),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangePlan":
        return cls(
            plan_id=str(data.get("plan_id") or ""),
            goal=str(data.get("goal") or ""),
            risk=str(data.get("risk") or ""),
            steps=[
                ChangeStep.from_dict(item)
                for item in data.get("steps", [])
                if isinstance(item, dict)
            ],
            resources=[
                PlanResource.from_dict(item)
                for item in data.get("resources", [])
                if isinstance(item, dict)
            ],
            assumptions=[str(item) for item in data.get("assumptions", [])],
            schema_version=int(data.get("schema_version") or 4),
            status=str(data.get("status") or "draft"),
            authorized_hash=str(data.get("authorized_hash") or ""),
            created_at=str(data.get("created_at") or _utc_now()),
            updated_at=str(data.get("updated_at") or _utc_now()),
            failure=FailureRecord.from_dict(data.get("failure")),
            binding_cursor=dict(data.get("binding_cursor") or {}),
        )
