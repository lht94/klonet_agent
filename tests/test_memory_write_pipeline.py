"""阶段 3（受控写入管线）的测试。

三段：

* **策略与解析**（纯离线）：敏感信息、作用域、稳定性、来源白名单、候选合同解析。
  这部分在任何机器上都能跑，也是"敏感数据不落库"这条验收标准的主要证明。
* **管线逻辑**（离线，用内存假仓库）：五种决策、幂等、补扫、失败不影响回合。
* **真库**（没有 DSN 时整块 skip）：候选审计与正式记忆在真库里的落地情况。

跑法（真库部分）：

    ./scripts/pg_local.sh up
    export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
    python -m pytest tests/test_memory_write_pipeline.py -q
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from klonet_agent.memory.candidate_extractor import (
    CandidateExtractor,
    CandidateProposal,
    ExtractionError,
    RawSource,
    TurnDigest,
    build_extraction_messages,
    extract_json_payload,
    parse_proposals,
)
from klonet_agent.memory.database import MemoryDatabase, temporary_database
from klonet_agent.memory.domain import (
    MemoryCandidate,
    MemoryRecord,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryVersion,
    Scope,
    SourceType,
    Tenant,
    WriteDecision,
    content_hash,
    normalize_content,
)
from klonet_agent.memory.repository import (
    ActiveSubjectConflictError,
    CandidateNotFoundError,
    DuplicateVersionError,
    NewRecordCommand,
    NewVersionCommand,
    RecordNotFoundError,
    RecordNotActiveError,
)
from klonet_agent.memory.versioning import plan_consolidation
from klonet_agent.memory.write_pipeline import (
    LegacyMemoryToolBridge,
    MemoryWritePipeline,
    NullWriteTracer,
    range_key,
)
from klonet_agent.memory.write_policy import (
    MemoryWritePolicy,
    RejectionReason,
    TurnContext,
    allowed_source_ids,
    redact_text,
    safe_for_trace,
    scan_sensitive,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"

ALICE = Tenant(user_id="alice", project_id="demo")
SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz012345"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _context(
    *,
    allowed: tuple[tuple[str, str], ...] = (("user_statement", "rows-1"),),
    project_id: str | None = "demo",
    allow_shared_ops: bool = False,
) -> TurnContext:
    return TurnContext(
        tenant=ALICE,
        observed_at=_now(),
        allowed_sources=frozenset(allowed),
        project_id=project_id,
        allow_shared_ops=allow_shared_ops,
    )


def _proposal(**overrides: object) -> CandidateProposal:
    fields: dict[str, object] = {
        "memory_type": "fact",
        "scope": "project",
        "content": "Klonet 后端运行时要求 Python 3.11",
        "subject": "klonet",
        "attribute": "runtime_version",
        "importance": "high",
        "confidence": 0.9,
        "proposed_decision": "add",
        "sources": (RawSource("user_statement", "rows-1", "用户明确说明"),),
    }
    fields.update(overrides)
    return CandidateProposal(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 敏感信息：扫描与脱敏
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        (f"OPENAI_API_KEY={SECRET}", True),
        ("password: hunter2xyz", True),
        (f"Authorization: Bearer {SECRET}", True),
        ("Cookie: session=abcdef123456", True),
        ("postgresql://klonet_app:s3cr3t@10.0.0.5:5432/klonet_memory", True),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", True),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop", True),
        ("AKIAIOSFODNN7EXAMPLE", True),
        ("ghp_" + "a" * 24, True),
        ("集群部署目标是 10.0.0.5，运行 Python 3.11", False),
    ],
)
def test_scan_sensitive_detects_credential_shapes(text: str, expected: bool) -> None:
    assert bool(scan_sensitive(text)) is expected


def test_redaction_removes_secret_but_keeps_context() -> None:
    text = f"项目用 PostgreSQL，DSN 是 postgresql://user:{SECRET}@10.0.0.5:5432/db"
    redacted, findings = redact_text(text)

    assert SECRET not in redacted
    assert "postgresql://" not in redacted or "***" in redacted
    # 与秘密无关的上下文必须留下：这条候选的价值在"项目用 PostgreSQL"。
    assert "PostgreSQL" in redacted
    assert findings and all(SECRET not in item.preview for item in findings)


def test_env_file_body_is_redacted_line_by_line() -> None:
    text = "配置片段：\nCHAT_LLM_API_KEY=abcdef123456\n普通说明行"
    redacted, findings = redact_text(text)
    assert "abcdef123456" not in redacted
    assert "普通说明行" in redacted
    assert any(item.kind.value == "env_file_body" for item in findings)


def test_safe_for_trace_never_returns_the_secret_and_is_bounded() -> None:
    text = f"token={SECRET} " + "长文本" * 200
    rendered = safe_for_trace(text)
    assert SECRET not in rendered
    assert len(rendered) <= 201  # 200 + 省略号


# --------------------------------------------------------------------------- #
# 候选合同：解析
# --------------------------------------------------------------------------- #


def test_parse_proposals_accepts_a_well_formed_payload() -> None:
    payload = {
        "candidates": [
            {
                "memory_type": "fact",
                "scope": "project",
                "entity": "klonet",
                "attribute": "runtime_version",
                "content": "运行时要求 Python 3.11",
                "importance": "high",
                "confidence": 0.8,
                "proposed_decision": "add",
                "sources": [
                    {"source_type": "user_statement", "source_id": "rows-1", "excerpt": "说明"}
                ],
            }
        ]
    }
    proposals, rejected = parse_proposals(payload)
    assert rejected == ()
    assert len(proposals) == 1
    assert proposals[0].memory_type == "fact"
    assert proposals[0].sources[0].source_id == "rows-1"


def test_one_bad_candidate_does_not_kill_the_others() -> None:
    payload = {
        "candidates": [
            {"memory_type": "nonsense", "scope": "project", "content": "x"},
            {"memory_type": "fact", "scope": "project", "entity": "a", "attribute": "b",
             "content": "这条是好的"},
            {"memory_type": "fact", "scope": "project", "content": ""},
        ]
    }
    proposals, rejected = parse_proposals(payload)
    assert [item.content for item in proposals] == ["这条是好的"]
    assert len(rejected) == 2
    assert any("memory_type" in item for item in rejected)


def test_parse_proposals_rejects_oversized_content_instead_of_truncating() -> None:
    payload = {
        "candidates": [
            {"memory_type": "fact", "scope": "project", "entity": "a", "attribute": "b",
             "content": "很长的内容" * 500}
        ]
    }
    proposals, rejected = parse_proposals(payload)
    assert proposals == ()
    assert any("超过" in item for item in rejected)


def test_parse_proposals_tolerates_code_fences_and_prose() -> None:
    raw = (
        "好的，这是提取结果：\n```json\n"
        + json.dumps({"candidates": [
            {"memory_type": "fact", "scope": "user", "entity": "u", "attribute": "a",
             "content": "用户偏好中文", "sources": [
                 {"source_type": "user_statement", "source_id": "rows-2"}]}
        ]}, ensure_ascii=False)
        + "\n```\n希望有帮助。"
    )
    proposals, rejected = parse_proposals(raw)
    assert rejected == ()
    assert len(proposals) == 1


def test_extract_json_payload_raises_on_garbage() -> None:
    with pytest.raises(ExtractionError):
        extract_json_payload("完全不是 JSON")
    with pytest.raises(ExtractionError):
        extract_json_payload("")


def test_missing_candidates_key_is_reported() -> None:
    proposals, rejected = parse_proposals({"other": []})
    assert proposals == ()
    assert rejected == ("缺少 candidates 字段",)


def test_extractor_raises_when_model_fails() -> None:
    def boom(_messages):
        raise RuntimeError("upstream 500")

    extractor = CandidateExtractor(complete=boom)
    with pytest.raises(ExtractionError):
        extractor.extract(_digest())

    assert CandidateExtractor().is_available is False
    with pytest.raises(ExtractionError):
        CandidateExtractor().extract(_digest())


def test_extraction_prompt_lists_allowed_evidence() -> None:
    messages = build_extraction_messages(_digest())
    body = messages[-1]["content"]
    assert "rows-1" in body
    assert "tool-9" in body
    assert "candidates" in body


def _digest(range_end: str = "rows-3") -> TurnDigest:
    return TurnDigest(
        source_event_range={"start": "rows-1", "end": range_end, "kind": "turn"},
        user_input="项目到底用哪个 Python 版本？",
        assistant_reply="看 config.py，运行时是 3.11。",
        events=(
            {"role": "user", "content": "项目到底用哪个 Python 版本？", "event_id": "rows-1"},
            {"role": "assistant", "content": "运行时是 3.11。", "event_id": "rows-2"},
        ),
        source_ids=(("history_event", "rows-1"), ("tool_result", "tool-9")),
        project_id="demo",
    )


def test_allowed_source_ids_marks_user_rows_as_user_statements() -> None:
    allowed = allowed_source_ids(
        [
            {"role": "user", "event_id": "rows-1"},
            {"role": "tool", "event_id": "rows-2", "tool_call_id": "tool-9"},
            {"role": "assistant", "event_id": "rows-3"},
    ]
    )
    assert ("user_statement", "rows-1") in allowed
    assert ("tool_result", "tool-9") in allowed
    assert ("history_event", "rows-3") in allowed


# --------------------------------------------------------------------------- #
# 策略层
# --------------------------------------------------------------------------- #


def test_policy_accepts_a_clean_candidate() -> None:
    verdict = MemoryWritePolicy().review(_proposal(), _context())
    assert verdict.approved is True
    assert verdict.candidate is not None
    assert verdict.candidate.subject_key == "fact:project:klonet:runtime_version"
    assert verdict.candidate.project_id == "demo"


def test_policy_rejects_candidate_without_sources() -> None:
    verdict = MemoryWritePolicy().review(_proposal(sources=()), _context())
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.NO_SOURCE


def test_policy_rejects_fabricated_source() -> None:
    """模型引用本回合不存在的证据 → 整条拒绝（不能只丢那一条来源）。"""

    verdict = MemoryWritePolicy().review(
        _proposal(sources=(RawSource("user_statement", "rows-999", ""),)), _context()
    )
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.FABRICATED_SOURCE


def test_policy_rejects_pure_secret() -> None:
    verdict = MemoryWritePolicy().review(
        _proposal(content=f"OPENAI_API_KEY={SECRET}"), _context()
    )
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.SENSITIVE_ONLY


def test_policy_rejects_candidate_that_is_mostly_secret() -> None:
    """正文几乎就是一条连接串时，脱敏后所剩无几，按敏感内容本身拒绝。"""

    verdict = MemoryWritePolicy().review(
        _proposal(
            content=f"项目记忆库 DSN 是 postgresql://user:{SECRET}@10.0.0.5:5432/klonet_memory"
        ),
        _context(),
    )
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.SENSITIVE_ONLY


def test_policy_keeps_useful_content_and_drops_the_secret() -> None:
    verdict = MemoryWritePolicy().review(
        _proposal(
            content="记忆库用 PostgreSQL 16 部署在 10.0.0.5，口令见 password=hunter2xyz"
        ),
        _context(),
    )
    assert verdict.approved is True
    assert verdict.candidate is not None
    assert "hunter2xyz" not in verdict.candidate.content
    assert "PostgreSQL 16" in verdict.candidate.content


def test_policy_can_be_configured_to_reject_any_sensitive_hit() -> None:
    policy = MemoryWritePolicy(redact_sensitive=False)
    verdict = policy.review(
        _proposal(content=f"DSN 里含 token={SECRET}，库名 klonet_memory"), _context()
    )
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.SENSITIVE_ONLY


def test_policy_rejects_transient_preference_but_keeps_stable_one() -> None:
    policy = MemoryWritePolicy()
    transient = _proposal(
        memory_type="preference",
        scope="user",
        subject="user",
        attribute="answer_style",
        content="这次先用简短回答就行",
        sources=(RawSource("user_statement", "rows-1", ""),),
    )
    verdict = policy.review(transient, _context(project_id=None))
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.TRANSIENT_PREFERENCE

    stable = _proposal(
        memory_type="preference",
        scope="user",
        subject="user",
        attribute="answer_style",
        content="以后回答都用简体中文",
        sources=(RawSource("user_statement", "rows-1", ""),),
    )
    assert policy.review(stable, _context(project_id=None)).approved is True


def test_policy_enforces_scope_rules() -> None:
    project = _proposal()
    verdict = MemoryWritePolicy().review(project, _context(project_id=None))
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.MISSING_PROJECT

    shared = _proposal(
        scope="shared_ops",
        subject="cluster",
        attribute="deploy_target",
        content="共享部署目标是 10.0.0.5",
    )
    assert (
        MemoryWritePolicy().review(shared, _context()).reason_code
        is RejectionReason.SCOPE_NOT_ALLOWED
    )
    assert (
        MemoryWritePolicy().review(shared, _context(allow_shared_ops=True)).approved
        is True
    )


def test_policy_downgrades_unfounded_verified_claim() -> None:
    verdict = MemoryWritePolicy().review(
        _proposal(
            verified=True,
            sources=(RawSource("history_event", "rows-1", ""),),
        ),
        _context(allowed=(("history_event", "rows-1"),)),
    )
    assert verdict.approved is True
    assert verdict.candidate is not None
    assert verdict.candidate.verified is False
    assert any("降级" in item for item in verdict.warnings)


def test_policy_caps_confidence_by_source_grade() -> None:
    verdict = MemoryWritePolicy().review(
        _proposal(
            confidence=0.99,
            sources=(RawSource("history_event", "rows-1", ""),),
        ),
        _context(allowed=(("history_event", "rows-1"),)),
    )
    assert verdict.candidate is not None
    # 对话事件的来源上限是 0.6。
    assert verdict.candidate.confidence == pytest.approx(0.6)


def test_policy_honours_verification_from_user_statement() -> None:
    verdict = MemoryWritePolicy().review(_proposal(verified=True), _context())
    assert verdict.candidate is not None
    assert verdict.candidate.verified is True


def test_policy_rejects_episode_without_event_identity() -> None:
    verdict = MemoryWritePolicy().review(
        _proposal(memory_type="episode", subject="", attribute=None), _context()
    )
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.MALFORMED


def test_policy_rejects_any_source_when_whitelist_is_empty() -> None:
    """白名单为空 = 本回合没有可引用证据，此时任何来源都不可核对。

    刻意不做"白名单为空就放行"的兜底：那样运行时忘了传上下文，
    "模型不能编造来源"这条保证会静默失效。
    """

    verdict = MemoryWritePolicy().review(_proposal(), _context(allowed=()))
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.FABRICATED_SOURCE


def test_policy_rejects_too_short_content() -> None:
    verdict = MemoryWritePolicy().review(_proposal(content="好"), _context())
    assert verdict.approved is False
    assert verdict.reason_code is RejectionReason.TOO_SHORT


# --------------------------------------------------------------------------- #
# 内存假仓库（离线验证管线逻辑）
# --------------------------------------------------------------------------- #


class FakeMemoryRepository:
    """够用的内存仓库：只为离线跑通管线，不实现检索。

    刻意**不**复用 PostgreSQL 实现：这一层的价值就是"没有真库也能验证编排逻辑"，
    否则管线测试会被 DSN 卡住。语义上对齐真实现的关键约束：
    同一 subject 只能有一条 active 记忆；同一记忆内容不可重复。
    """

    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}
        self.active_versions: dict[str, MemoryVersion] = {}
        self.versions: dict[str, list[MemoryVersion]] = {}
        self.sources: dict[str, list[MemorySource]] = {}
        self.candidates: dict[str, dict[str, object]] = {}

    # --- 写入 ---

    def add_candidate_payload(
        self,
        payload,
        *,
        user_id,
        project_id,
        idempotency_key,
        source_event_range,
    ) -> str:
        existing = self.candidates.get(idempotency_key)
        if existing is not None:
            return str(existing["id"])
        candidate_id = str(uuid4())
        self.candidates[idempotency_key] = {
            "id": candidate_id,
            "payload": dict(payload),
            "source_event_range": dict(source_event_range),
            "decision": None,
            "decision_reason": "",
        }
        return candidate_id

    def add_candidate(self, candidate, *, idempotency_key, source_event_range) -> str:
        return self.add_candidate_payload(
            {"content": candidate.content, "subject_key": candidate.subject_key},
            user_id=candidate.user_id,
            project_id=candidate.project_id,
            idempotency_key=idempotency_key,
            source_event_range=source_event_range,
        )

    def processed_source_ranges(self, *, limit: int = 1000):
        return [dict(item["source_event_range"]) for item in self.candidates.values()][:limit]

    def record_decision(self, candidate_id, decision, *, reason, processed_at=None) -> None:
        for item in self.candidates.values():
            if item["id"] == candidate_id:
                if item["decision"] is not None:
                    # 与真实现一致：候选只允许决策一次（真库那边是
                    # `WHERE ... AND decision IS NULL`）。
                    raise CandidateNotFoundError(candidate_id)
                item["decision"] = WriteDecision(decision).value
                item["decision_reason"] = reason
                return
        raise RecordNotFoundError(candidate_id)

    def add_record(self, command: NewRecordCommand) -> MemoryRecord:
        if self.find_active_by_subject(command.subject_key) is not None:
            raise ActiveSubjectConflictError(command.subject_key, "")
        record_id = command.record_id or str(uuid4())
        version_id = command.version_id or str(uuid4())
        content = normalize_content(command.content)
        version = MemoryVersion(
            id=version_id,
            memory_id=record_id,
            version=1,
            content=content,
            observed_at=command.observed_at or _now(),
            valid_from=command.valid_from or command.observed_at or _now(),
            content_hash=content_hash(content),
            verified=bool(command.verified),
            sources=tuple(command.sources),
        )
        record = MemoryRecord(
            id=record_id,
            user_id=command.user_id,
            scope=command.scope,
            memory_type=command.memory_type,
            subject_key=command.subject_key,
            project_id=command.project_id,
            active_version_id=version_id,
            confidence=command.confidence,
            importance=command.importance,
            active_version=version,
        )
        self.records[record_id] = record
        self.active_versions[record_id] = version
        self.versions[record_id] = [version]
        self.sources[version_id] = list(command.sources)
        return record

    def add_version(self, command: NewVersionCommand) -> MemoryVersion:
        record = self.records.get(command.memory_id)
        if record is None:
            raise RecordNotFoundError(command.memory_id)
        if record.status is not MemoryStatus.ACTIVE:
            raise RecordNotActiveError(command.memory_id)
        content = normalize_content(command.content)
        digest = content_hash(content)
        existing = self.versions[command.memory_id]
        if any(item.content_hash == digest for item in existing):
            raise DuplicateVersionError(existing[-1].id, existing[-1].version)
        previous = self.active_versions[command.memory_id]
        started = command.valid_from or _now()
        closed = MemoryVersion(
            id=previous.id,
            memory_id=previous.memory_id,
            version=previous.version,
            content=previous.content,
            observed_at=previous.observed_at,
            valid_from=previous.valid_from,
            content_hash=previous.content_hash,
            valid_to=max(started, previous.valid_from + timedelta(microseconds=1)),
            verified=previous.verified,
        )
        version = MemoryVersion(
            id=command.version_id or str(uuid4()),
            memory_id=command.memory_id,
            version=previous.version + 1,
            content=content,
            observed_at=command.observed_at or _now(),
            valid_from=started,
            content_hash=digest,
            verified=bool(command.verified),
            sources=tuple(command.sources),
        )
        self.versions[command.memory_id] = [
            closed if item.id == previous.id else item for item in existing
        ] + [version]
        self.active_versions[command.memory_id] = version
        self.sources[version.id] = list(command.sources)
        self.records[command.memory_id] = MemoryRecord(
            id=record.id,
            user_id=record.user_id,
            scope=record.scope,
            memory_type=record.memory_type,
            subject_key=record.subject_key,
            status=record.status,
            project_id=record.project_id,
            active_version_id=version.id,
            confidence=max(record.confidence, command.confidence or 0.0),
            importance=record.importance,
            active_version=version,
        )
        return version

    def attach_sources(
        self, memory_id: str, sources, *, confidence=None, verified: bool = False
    ) -> MemoryVersion:
        """内容等价时"只补来源"的 UPDATE 形态。

        不新增版本：版本表上有 (memory_id, content_hash) 唯一约束，
        同一段正文在一个记忆里只存一份（与真实现一致）。
        """

        record = self.records.get(memory_id)
        if record is None:
            raise RecordNotFoundError(memory_id)
        if record.status is not MemoryStatus.ACTIVE:
            raise RecordNotActiveError(memory_id)
        version = self.active_versions[memory_id]
        existing = self.sources.setdefault(version.id, [])
        for source in sources:
            if all(
                (item.source_type, item.source_id)
                != (source.source_type, source.source_id)
                for item in existing
            ):
                existing.append(source)
        if confidence is not None:
            self.records[memory_id] = MemoryRecord(
                id=record.id,
                user_id=record.user_id,
                scope=record.scope,
                memory_type=record.memory_type,
                subject_key=record.subject_key,
                status=record.status,
                project_id=record.project_id,
                active_version_id=record.active_version_id,
                confidence=max(record.confidence, confidence),
                importance=record.importance,
                active_version=version,
            )
        return version

    def replace_active(
        self, old_memory_id: str, command: NewRecordCommand, *, relation_confidence=1.0, reason=None
    ) -> MemoryRecord:
        old = self.records.get(old_memory_id)
        if old is None:
            raise RecordNotFoundError(old_memory_id)
        if old.status is not MemoryStatus.ACTIVE:
            raise RecordNotActiveError(old_memory_id)
        if old.subject_key != command.subject_key:
            raise RecordNotFoundError("subject 不一致")
        previous = self.active_versions[old_memory_id]
        started = command.valid_from or _now()
        self.versions[old_memory_id] = [
            MemoryVersion(
                id=item.id,
                memory_id=item.memory_id,
                version=item.version,
                content=item.content,
                observed_at=item.observed_at,
                valid_from=item.valid_from,
                content_hash=item.content_hash,
                valid_to=max(started, item.valid_from + timedelta(microseconds=1)),
                verified=item.verified,
            )
            if item.id == previous.id
            else item
            for item in self.versions[old_memory_id]
        ]
        self.records[old_memory_id] = MemoryRecord(
            id=old.id,
            user_id=old.user_id,
            scope=old.scope,
            memory_type=old.memory_type,
            subject_key=old.subject_key,
            status=MemoryStatus.SUPERSEDED,
            project_id=old.project_id,
            active_version_id=old.active_version_id,
            confidence=old.confidence,
            importance=old.importance,
        )
        del self.active_versions[old_memory_id]
        return self.add_record(command)

    # --- 读取 ---

    def get_record(self, memory_id: str):
        return self.records.get(memory_id)

    def find_active_by_subject(self, subject_key: str):
        for record in self.records.values():
            if record.subject_key == subject_key and record.status is MemoryStatus.ACTIVE:
                return MemoryRecord(
                    id=record.id,
                    user_id=record.user_id,
                    scope=record.scope,
                    memory_type=record.memory_type,
                    subject_key=record.subject_key,
                    status=record.status,
                    project_id=record.project_id,
                    active_version_id=record.active_version_id,
                    confidence=record.confidence,
                    importance=record.importance,
                    active_version=self.active_versions.get(record.id),
                )
        return None

    def list_sources(self, version_id: str):
        return list(self.sources.get(version_id, ()))


class RecordingTracer:
    """把 trace 记录留在内存里，便于断言"秘密没有进 trace"。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_memory_candidate(self, **kwargs) -> None:
        self.rows.append(kwargs)


def _pipeline(repository, *, proposals=None, tracer=None) -> MemoryWritePipeline:
    def complete(_messages):
        payload = {"candidates": [dict(item) for item in (proposals or [])]}
        return json.dumps(payload, ensure_ascii=False)

    return MemoryWritePipeline(
        repository,
        extractor=CandidateExtractor(complete=complete if proposals is not None else None),
        policy=MemoryWritePolicy(),
        tracer=tracer or RecordingTracer(),
    )


def _repo() -> FakeMemoryRepository:
    return FakeMemoryRepository()


def _process(pipeline, proposals_by_turn, digest, context, *, range_end):
    pipeline._extractor = CandidateExtractor(
        complete=lambda _m: json.dumps({"candidates": proposals_by_turn}, ensure_ascii=False)
    )
    return pipeline.process_turn(digest, context)


# --------------------------------------------------------------------------- #
# 管线：决策与审计（离线）
# --------------------------------------------------------------------------- #


def test_pipeline_adds_then_noops_then_supersedes() -> None:
    repository = _repo()
    pipeline = _pipeline(repository)
    context = _context(allowed=(("user_statement", "rows-1"),))
    base = {
        "memory_type": "fact",
        "scope": "project",
        "entity": "klonet",
        "attribute": "runtime_version",
        "sources": [{"source_type": "user_statement", "source_id": "rows-1"}],
    }

    first = _process(
        pipeline,
        [{**base, "content": "运行时要求 Python 3.8", "proposed_decision": "add"}],
        _digest("rows-3"),
        context,
        range_end="rows-3",
    )
    assert first.ok and len(first.accepted) == 1
    record_id = first.accepted[0].applied.record.id

    second = _process(
        pipeline,
        [{**base, "content": "运行时要求 Python 3.8", "proposed_decision": "add"}],
        _digest("rows-5"),
        context,
        range_end="rows-5",
    )
    assert second.outcomes[0].decision is WriteDecision.NOOP

    third = _process(
        pipeline,
        [{**base, "content": "运行时要求 Python 3.11", "proposed_decision": "supersede"}],
        _digest("rows-7"),
        context,
        range_end="rows-7",
    )
    assert third.outcomes[0].decision is WriteDecision.SUPERSEDE
    assert repository.records[record_id].status is MemoryStatus.SUPERSEDED


def test_pipeline_skips_an_already_processed_range() -> None:
    repository = _repo()
    pipeline = _pipeline(repository)
    context = _context()
    proposals = [
        {
            "memory_type": "fact",
            "scope": "project",
            "entity": "klonet",
            "attribute": "runtime_version",
            "content": "运行时要求 Python 3.11",
            "sources": [{"source_type": "user_statement", "source_id": "rows-1"}],
        }
    ]

    first = _process(pipeline, proposals, _digest("rows-3"), context, range_end="rows-3")
    assert first.skipped is False

    again = _process(pipeline, proposals, _digest("rows-3"), context, range_end="rows-3")
    assert again.skipped is True
    assert again.outcomes == ()


def test_pipeline_updates_when_only_evidence_changed() -> None:
    repository = _repo()
    pipeline = _pipeline(repository)
    context = _context(allowed=(("user_statement", "rows-1"),))
    base = {
        "memory_type": "fact",
        "scope": "project",
        "entity": "klonet",
        "attribute": "shell",
        "content": "默认 shell 是 bash",
        "sources": [{"source_type": "user_statement", "source_id": "rows-1"}],
    }
    first = _process(pipeline, [base], _digest("rows-3"), context, range_end="rows-3")
    record_id = first.accepted[0].applied.record.id

    # 同一句话，但多了一条工具证据 → UPDATE（追加证据而不是新记忆）。
    pipeline._extractor = CandidateExtractor(
        complete=lambda _m: json.dumps(
            {"candidates": [
                {**base, "sources": [
                    {"source_type": "user_statement", "source_id": "rows-1"},
                    {"source_type": "tool_result", "source_id": "tool-9"},
                ]}
            ]},
            ensure_ascii=False,
        )
    )
    context2 = _context(
        allowed=(("user_statement", "rows-1"), ("tool_result", "tool-9"))
    )
    second = pipeline.process_turn(_digest("rows-5"), context2)
    assert second.outcomes[0].decision is WriteDecision.UPDATE
    # 内容等价 → 只补来源，不新增版本（版本表上 (memory_id, content_hash) 是唯一的）。
    assert len(repository.versions[record_id]) == 1
    version_id = repository.active_versions[record_id].id
    assert len(repository.sources[version_id]) == 2


def test_pipeline_records_rejections_without_writing_memory() -> None:
    repository = _repo()
    tracer = RecordingTracer()
    pipeline = _pipeline(repository, tracer=tracer)

    outcome = _process(
        pipeline,
        [{"memory_type": "fact", "scope": "project", "entity": "a",
          "attribute": "b", "content": "这条没有来源"}],
        _digest("rows-3"),
        _context(),
        range_end="rows-3",
    )
    assert outcome.outcomes[0].decision is WriteDecision.REJECT
    assert repository.records == {}
    # 拒绝也要进候选表（§7.1："拒绝只记录在 candidate 表"）。
    assert len(repository.candidates) == 1
    assert [row["stage"] for row in tracer.rows] == ["policy_rejected"]


def test_sensitive_candidate_never_reaches_database_or_trace() -> None:
    """阶段 3 的验收标准之一：敏感测试数据不进数据库、也不进 trace。"""

    repository = _repo()
    tracer = RecordingTracer()
    pipeline = _pipeline(repository, tracer=tracer)

    outcome = _process(
        pipeline,
        [{
            "memory_type": "fact",
            "scope": "project",
            "entity": "klonet",
            "attribute": "api_key",
            "content": f"OPENAI_API_KEY={SECRET}",
            "sources": [{"source_type": "user_statement", "source_id": "rows-1"}],
        }],
        _digest("rows-3"),
        _context(),
        range_end="rows-3",
    )
    assert outcome.outcomes[0].decision is WriteDecision.REJECT

    dumped = json.dumps(
        {
            "candidates": repository.candidates,
            "records": [record.subject_key for record in repository.records.values()],
            "trace": tracer.rows,
        },
        ensure_ascii=False,
        default=str,
    )
    assert SECRET not in dumped
    assert repository.records == {}
    # 含敏感的候选连候选表都不进。
    assert repository.candidates == {}


def test_pipeline_survives_extraction_failure() -> None:
    repository = _repo()

    def boom(_messages):
        raise RuntimeError("upstream 500")

    pipeline = MemoryWritePipeline(
        repository, extractor=CandidateExtractor(complete=boom), tracer=RecordingTracer()
    )
    outcome = pipeline.process_turn(_digest("rows-3"), _context())
    assert outcome.ok is False
    assert "提取失败" in (outcome.error or "")
    assert outcome.outcomes == ()


def test_backfill_processes_only_unprocessed_ranges() -> None:
    repository = _repo()
    pipeline = _pipeline(repository)
    context = _context()
    proposals = [
        {
            "memory_type": "fact",
            "scope": "project",
            "entity": "klonet",
            "attribute": "runtime_version",
            "content": "运行时要求 Python 3.11",
            "sources": [{"source_type": "user_statement", "source_id": "rows-1"}],
        }
    ]
    _process(pipeline, proposals, _digest("rows-3"), context, range_end="rows-3")

    pipeline._extractor = CandidateExtractor(
        complete=lambda _m: json.dumps({"candidates": proposals}, ensure_ascii=False)
    )
    results = pipeline.backfill(
        [
            (_digest("rows-3"), context),  # 已处理 → 跳过
            (TurnDigest(
                source_event_range={"start": "rows-5", "end": "rows-7", "kind": "turn"},
                user_input="再看一次版本",
                assistant_reply="仍然是 3.11",
                source_ids=(("user_statement", "rows-1"),),
                project_id="demo",
            ), context),
        ]
    )
    assert len(results) == 1
    assert results[0].source_event_range["start"] == "rows-5"


def test_range_key_and_decisions_are_stable() -> None:
    assert range_key({"start": "rows-1", "end": "rows-3"}) == "rows-1..rows-3"
    assert range_key({}) == ".."


def test_plan_consolidation_rejects_when_proposal_missing_and_content_differs() -> None:
    """内容不同又没说 UPDATE/SUPERSEDE 时不允许猜——管线不能替模型做决定。"""

    existing = MemoryRecord(
        id=str(uuid4()),
        user_id=ALICE.user_id,
        scope=Scope.PROJECT,
        memory_type=MemoryType.FACT,
        subject_key="fact:project:klonet:runtime_version",
        project_id="demo",
        active_version=MemoryVersion(
            id=str(uuid4()),
            memory_id=str(uuid4()),
            version=1,
            content="运行时要求 Python 3.8",
            observed_at=_now(),
            valid_from=_now(),
            content_hash=content_hash("运行时要求 Python 3.8"),
        ),
    )
    candidate = MemoryCandidate(
        memory_type=MemoryType.FACT,
        scope=Scope.PROJECT,
        subject_key="fact:project:klonet:runtime_version",
        content="运行时要求 Python 3.11",
        user_id=ALICE.user_id,
        project_id="demo",
        sources=(
            MemorySource(
                source_type=SourceType.USER_STATEMENT,
                source_id="rows-1",
                observed_at=_now(),
            ),
        ),
    )
    plan = plan_consolidation(candidate, existing)
    assert plan.decision is WriteDecision.REJECT


# --------------------------------------------------------------------------- #
# 旧记忆工具：候选提交与兼容代理
# --------------------------------------------------------------------------- #


def test_bridge_submits_episode_as_candidate() -> None:
    repository = _repo()
    tracer = RecordingTracer()
    pipeline = _pipeline(repository, tracer=tracer)
    bridge = LegacyMemoryToolBridge(
        pipeline,
        # 情景记忆的来源就是"模型决定记下来的那一行"，所以事件行号必须在白名单里。
        context_provider=lambda: _context(allowed=(("history_event", "rows-42"),)),
        event_id_provider=lambda: "rows-42",
    )

    message = bridge.submit_episode("## 10:00 修掉了一个真实 bug\n\n原因是路径拼接用了字符串。")
    assert "候选" in message
    assert len(repository.records) == 1
    record = next(iter(repository.records.values()))
    assert record.memory_type is MemoryType.EPISODE
    assert record.subject_key == "episode:rows_42"  # 组件里的 "-" 会被归一成 "_"
    assert record.active_version is not None


def test_bridge_repeated_episode_is_idempotent_not_an_error() -> None:
    """同一行里重复调用 append_episode 不能报成"写入失败"，也不能写两条。"""

    repository = _repo()
    pipeline = _pipeline(repository, tracer=RecordingTracer())
    bridge = LegacyMemoryToolBridge(
        pipeline,
        context_provider=lambda: _context(allowed=(("history_event", "rows-42"),)),
        event_id_provider=lambda: "rows-42",
    )

    first = bridge.submit_episode("## 10:00 事件\n\n同一行里记两次")
    second = bridge.submit_episode("## 10:00 事件\n\n同一行里记两次")

    assert "候选" in first
    assert "写入失败" not in second
    assert "已经处理过" in second
    assert len(repository.records) == 1


def test_bridge_blocks_whole_file_overwrite_and_audits() -> None:
    repository = _repo()
    tracer = RecordingTracer()
    pipeline = _pipeline(repository, tracer=tracer)
    bridge = LegacyMemoryToolBridge(
        pipeline, context_provider=_context, event_id_provider=lambda: "rows-7"
    )

    message = bridge.block_overwrite("write_memory", "# MEMORY.md\n\n## 事实\n\n项目用 PG")
    assert "拒绝" in message
    assert repository.records == {}
    assert repository.candidates  # 审计留在候选表里
    assert tracer.rows and tracer.rows[0]["stage"] == "legacy_overwrite_blocked"


def test_bridge_blocked_overwrite_with_secret_is_not_persisted() -> None:
    repository = _repo()
    tracer = RecordingTracer()
    pipeline = _pipeline(repository, tracer=tracer)
    bridge = LegacyMemoryToolBridge(
        pipeline, context_provider=_context, event_id_provider=lambda: "rows-7"
    )

    bridge.block_overwrite("write_user", f"# USER.md\n\n- 我的 key 是 {SECRET}")
    dumped = json.dumps(
        {"candidates": repository.candidates, "trace": tracer.rows},
        ensure_ascii=False,
        default=str,
    )
    assert SECRET not in dumped
    assert repository.candidates == {}


def test_executor_routes_memory_tools_through_the_bridge() -> None:
    from klonet_agent.tools.executor import ToolExecutor

    calls: list[tuple[str, str]] = []

    class Bridge:
        def submit_episode(self, content: str) -> str:
            calls.append(("episode", content))
            return "已提交候选"

        def block_overwrite(self, tool_name: str, content: str) -> str:
            calls.append((tool_name, content))
            return "已拒绝整篇覆盖"

    executor = ToolExecutor(allowed_tools={"append_episode", "write_memory", "write_user"})
    executor.set_memory_tool_bridge(Bridge())

    assert executor.run("append_episode", {"content": "今天修了 bug"}) == "已提交候选"
    assert executor.run("write_memory", {"content": "# MEMORY"}) == "已拒绝整篇覆盖"
    assert executor.run("write_user", {"content": "# USER"}) == "已拒绝整篇覆盖"
    assert [name for name, _ in calls] == ["episode", "write_memory", "write_user"]


def test_executor_without_bridge_keeps_legacy_behaviour(tmp_path) -> None:
    """没有注入桥梁时行为必须和以前一致：不能因为加了新路径就丢掉旧能力。"""

    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.tools.executor import ToolExecutor

    store = MemoryStore.for_session(tmp_path, "alice", "demo")
    executor = ToolExecutor(allowed_tools={"append_episode"}, memory_store=store)
    result = executor.run("append_episode", {"content": "## 10:00 事件\n\n内容"})
    assert "情景记忆" in result
    assert "内容" in store.read_today_episode()


@pytest.fixture
def registry_flag():
    """在两种开关状态下重新加载工具表，跑完恢复。

    要改的是 **config 里的值**再 reload：registry 顶部是
    ``from klonet_agent.config import MEMORY_WRITE_PIPELINE_ENABLED``，
    直接改 registry 的属性会被 reload 重新导入覆盖掉。
    """

    import klonet_agent.config as config
    import klonet_agent.tools.registry as registry

    original = config.MEMORY_WRITE_PIPELINE_ENABLED

    def _load(enabled: bool):
        config.MEMORY_WRITE_PIPELINE_ENABLED = enabled
        importlib.reload(registry)
        return registry

    yield _load

    config.MEMORY_WRITE_PIPELINE_ENABLED = original
    importlib.reload(registry)


def _description(registry, name: str) -> str:
    for item in registry.TOOLS:
        if item["function"]["name"] == name:
            return item["function"]["description"]
    return ""


def test_tool_descriptions_follow_the_pipeline_flag(registry_flag) -> None:
    legacy = registry_flag(False)
    assert "整篇覆盖" in _description(legacy, "write_memory")
    assert "日记" in _description(legacy, "append_episode")

    controlled = registry_flag(True)
    assert "已停用" in _description(controlled, "write_memory")
    assert "受控写入" in _description(controlled, "write_user")
    assert "候选" in _description(controlled, "append_episode")


def test_tools_layer_has_no_direct_repository_write_path() -> None:
    """架构守卫：工具层不能绕过策略直接写记忆库。

    阶段 3 的验收标准是"主模型无法绕过 policy 任意更新正式记忆"。
    这条测试把它变成可执行断言：``tools/`` 里既不许引 PostgreSQL 实现，
    也不许出现任何仓库写方法的名字。
    """

    forbidden = (
        "PostgresMemoryRepository",
        "klonet_agent.memory.postgres",
        "add_record(",
        "add_version(",
        "replace_active(",
        "attach_sources(",
        "mark_expired(",
        "write_memory(",
        "write_user(",
    )
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "tools").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            if name in text:
                offenders.append(f"{path.name}: {name}")
    # self.memory_store.write_memory(...) 这样的旧文件写入是允许的（开关关闭时的兼容路径），
    # 所以只对仓库写方法与数据库实现做断言。
    offenders = [item for item in offenders if not item.endswith(("write_memory(", "write_user("))]
    assert offenders == []


# --------------------------------------------------------------------------- #
# 真库：审计落地与"秘密不进库"
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def admin_dsn() -> str:
    dsn = (os.environ.get(TEST_DSN_ENV) or "").strip()
    if not dsn:
        pytest.skip(
            f"未设置 {TEST_DSN_ENV}，跳过写入管线的真库测试"
            "（候选审计表与正式记忆的落地必须在真库上证明）"
        )
    try:
        import psycopg
    except ImportError:
        pytest.skip("未安装 psycopg，跳过写入管线的真库测试")
    try:
        with psycopg.connect(dsn, connect_timeout=5.0):
            pass
    except Exception as exc:
        pytest.fail(f"{TEST_DSN_ENV} 已设置但连不上：{exc}")
    return dsn


@pytest.fixture(scope="module")
def db(admin_dsn: str):
    with temporary_database(admin_dsn) as dsn:
        database = MemoryDatabase(dsn, min_size=1, max_size=4)
        database.open()
        try:
            assert database.run_migrations() == [
                "0001_init",
                "0002_roles_and_grants",
                "0003_immutable_versions",
                "0004_governance",
            ]
            yield database
        finally:
            database.close()


def _pg_pipeline(db, proposals, *, tracer=None):
    from klonet_agent.memory.postgres import PostgresMemoryRepository

    repository = PostgresMemoryRepository(db, ALICE)

    def complete(_messages):
        return json.dumps({"candidates": proposals}, ensure_ascii=False)

    return (
        MemoryWritePipeline(
            repository,
            extractor=CandidateExtractor(complete=complete),
            tracer=tracer or RecordingTracer(),
        ),
        repository,
    )


def _pg_digest(start: str, end: str, *, kind: str = "turn") -> TurnDigest:
    return TurnDigest(
        source_event_range={"start": start, "end": end, "kind": kind},
        user_input="项目运行时是什么版本？",
        assistant_reply="看 config.py，是 3.11。",
        events=({"role": "user", "content": "项目运行时是什么版本？", "event_id": start},),
        source_ids=(("user_statement", start),),
        project_id="demo",
    )


def test_real_db_pipeline_writes_memory_and_audits_candidates(db) -> None:
    proposals = [
        {
            "memory_type": "fact",
            "scope": "project",
            "entity": "klonet",
            "attribute": "runtime_version",
            "content": "项目运行时要求 Python 3.11",
            "importance": "high",
            "confidence": 0.9,
            "proposed_decision": "add",
            "sources": [{"source_type": "user_statement", "source_id": "rows-1"}],
        }
    ]
    pipeline, repository = _pg_pipeline(db, proposals)
    context = _context()
    outcome = pipeline.process_turn(_pg_digest("rows-1", "rows-2"), context)

    assert outcome.ok is True
    assert len(outcome.accepted) == 1
    record = outcome.accepted[0].applied.record
    assert record is not None
    assert record.memory_type is MemoryType.FACT

    # 候选表里有决策记录（审计），并且区间已经登记（幂等台账）。
    decided = repository.list_candidates(decision=WriteDecision.ADD)
    assert len(decided) >= 1
    assert any(
        range_key(item.source_event_range) == "rows-1..rows-2" for item in decided
    ) or any(
        range_key(item) == "rows-1..rows-2"
        for item in repository.processed_source_ranges()
    )


def test_real_db_pipeline_is_idempotent_per_event_range(db) -> None:
    proposals = [
        {
            "memory_type": "fact",
            "scope": "project",
            "entity": "klonet",
            "attribute": "idempotent_probe",
            "content": "这条记忆只应该出现一次",
            "sources": [{"source_type": "user_statement", "source_id": "rows-20"}],
        }
    ]
    pipeline, _ = _pg_pipeline(db, proposals)
    context = _context()
    digest = _pg_digest("rows-20", "rows-21")

    first = pipeline.process_turn(digest, context)
    assert first.skipped is False
    second = pipeline.process_turn(digest, context)
    assert second.skipped is True


def test_real_db_sensitive_payload_never_persisted(db) -> None:
    proposals = [
        {
            "memory_type": "fact",
            "scope": "project",
            "entity": "klonet",
            "attribute": "ingest_key",
            "content": f"OPENAI_API_KEY={SECRET}",
            "sources": [{"source_type": "user_statement", "source_id": "rows-30"}],
        }
    ]
    tracer = RecordingTracer()
    pipeline, repository = _pg_pipeline(db, proposals, tracer=tracer)
    outcome = pipeline.process_turn(_pg_digest("rows-30", "rows-31"), _context())
    assert outcome.outcomes[0].decision is WriteDecision.REJECT

    # 整库扫一遍：候选 payload 与正式版本里都不能出现这个秘密。
    with db.diagnostic_session() as conn:
        payloads = conn.execute(
            "SELECT candidate_payload::text AS payload FROM memory_write_candidates"
        ).fetchall()
        contents = conn.execute(
            "SELECT content FROM memory_versions"
        ).fetchall()
    blob = " ".join([row["payload"] for row in payloads] + [row["content"] for row in contents])
    assert SECRET not in blob
    assert SECRET not in json.dumps(tracer.rows, ensure_ascii=False, default=str)
    assert repository.find_active_by_subject(
        "fact:project:klonet:ingest_key"
    ) is None


def test_real_db_backfill_does_not_repeat_processed_range(db) -> None:
    proposals = [
        {
            "memory_type": "fact",
            "scope": "project",
            "entity": "klonet",
            "attribute": "backfill_probe",
            "content": "补扫只处理未处理的区间",
            "sources": [{"source_type": "user_statement", "source_id": "rows-40"}],
        }
    ]
    pipeline, repository = _pg_pipeline(db, proposals)
    context = _context()
    processed = _pg_digest("rows-40", "rows-41")
    pipeline.process_turn(processed, context)

    fresh = _pg_digest("rows-42", "rows-43")
    outcomes = pipeline.backfill([(processed, context), (fresh, context)])
    assert [range_key(item.source_event_range) for item in outcomes] == ["rows-42..rows-43"]


def test_real_db_memory_tool_bridge_submits_episode(db) -> None:
    from klonet_agent.memory.postgres import PostgresMemoryRepository

    repository = PostgresMemoryRepository(db, ALICE)
    pipeline = MemoryWritePipeline(
        repository, extractor=CandidateExtractor(), tracer=RecordingTracer()
    )
    bridge = LegacyMemoryToolBridge(
        pipeline,
        context_provider=lambda: _context(allowed=(("history_event", "rows-60"),)),
        event_id_provider=lambda: "rows-60",
    )
    message = bridge.submit_episode("## 11:00 真库验证\n\n记忆管线在 PostgreSQL 上跑通了。")
    assert "候选" in message

    record = repository.find_active_by_subject("episode:rows_60")
    assert record is not None
    assert record.memory_type is MemoryType.EPISODE

    # 同一行里重复提交不能报成失败，也不能写出第二条 episode。
    again = bridge.submit_episode("## 11:00 真库验证\n\n记忆管线在 PostgreSQL 上跑通了。")
    assert "写入失败" not in again
    with db.diagnostic_session() as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM memory_records "
            "WHERE subject_key = 'episode:rows_60'"
        ).fetchone()["n"]
    assert count == 1
