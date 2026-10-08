"""记忆写入策略：把模型提出的候选变成"可以进库的东西"（阶段 3）。

计划 §6.4 的管线是「事件范围 → 提取候选 → **敏感信息过滤** → 判断是否值得长期保存
→ 检索相似现有记忆 → 决策 → 事务 → outbox」。这个文件负责中间那两步半——
所有"候选该不该写、写成什么样"的规则都在这里，而且**只有这一个出口**：

* ``memory/candidate_extractor.py`` 只做形状解析，不做判断；
* ``memory/versioning.py`` 只在候选已经是合法的 :class:`MemoryCandidate` 之后做决策。

分开的好处是每一条拒绝都能给出唯一、可审计的原因；坏处是规则会集中在一个文件里
（这是刻意的，审计时需要"所有拒绝理由一览"）。

## 与 ``ops`` 里既有敏感正则的关系

``ops/operations.py`` 与 ``ops/privileged/action_runner.py`` 各有一份
``(?i)\\b...password|passwd|pwd|api[_-]?key|secret|token...\\s*[:=]``，用途是
**拒绝**把含密钥的操作写进计划/脚本。本文件的用途不同：它要**定位并脱敏片段**，
因此需要 span 级匹配，除"赋值型"之外还要认无赋值的密钥形状、请求头、私钥、
连接串。两处的核心赋值模式保持一致口径，但刻意不互相 import——
``memory/`` 是更底层的包，不该依赖 ``ops/``（会引入 0.24s 的额外 import 与
一条从下往上的依赖边）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from klonet_agent.memory.candidate_extractor import CandidateProposal, RawSource
from klonet_agent.memory.domain import (
    MemoryCandidate,
    MemoryDomainError,
    MemorySource,
    MemoryType,
    Scope,
    SourceType,
    Tenant,
    WriteDecision,
    build_subject_key,
    confidence_ceiling,
    importance_for_level,
    normalize_content,
    qualifies_as_verified,
)

__all__ = [
    "CANDIDATE_CONTENT_MAX_CHARS",
    "TRACE_CONTENT_MAX_CHARS",
    "MemoryWritePolicy",
    "PolicyVerdict",
    "RejectionReason",
    "SensitiveFinding",
    "SensitiveKind",
    "TurnContext",
    "redact_text",
    "safe_for_trace",
    "scan_sensitive",
]


# 进库正文的上限（与 candidate_extractor 的解析上限一致，这里是第二道闸）。
CANDIDATE_CONTENT_MAX_CHARS = 2000
# 低于这个长度不认为是"独立成立的一句话"。
MIN_CONTENT_CHARS = 6

# trace 里正文最多留这么多字符。trace 是长期落盘文件，不能整段抄候选内容。
TRACE_CONTENT_MAX_CHARS = 200
TRACE_REASON_MAX_CHARS = 300

REDACTION = "***"

# 脱敏后剩余内容占比低于这个值时，认为"这条候选基本就是秘密本身"，直接拒绝。
REDACTION_SURVIVAL_RATIO = 0.35


class SensitiveKind(str, Enum):
    """敏感片段的类型。只用于 trace 与统计，不含任何原值。"""

    CREDENTIAL_ASSIGNMENT = "credential_assignment"
    PROVIDER_TOKEN = "provider_token"
    PRIVATE_KEY = "private_key"
    AUTHORIZATION_HEADER = "authorization_header"
    COOKIE_HEADER = "cookie_header"
    CONNECTION_STRING = "connection_string"
    JWT = "jwt"
    ENV_FILE_BODY = "env_file_body"


@dataclass(frozen=True)
class SensitiveFinding:
    """一次敏感命中。``preview`` 只保留极短的形状信息，绝不包含完整原值。"""

    kind: SensitiveKind
    pattern: str
    preview: str


# 注意每条模式都必须**只匹配到值为止**：脱敏是把命中区间替换成 ***，
# 匹配范围过大（例如把整行都吃掉）会把有用的上下文一起抹掉。
SENSITIVE_PATTERNS: tuple[tuple[SensitiveKind, str, re.Pattern[str]], ...] = (
    (
        SensitiveKind.PRIVATE_KEY,
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        SensitiveKind.AUTHORIZATION_HEADER,
        "authorization_header",
        re.compile(
            r"(?i)\b(?:authorization|proxy-authorization)\s*[:=]\s*"
            r"(?:bearer|basic|token)?\s*[A-Za-z0-9._~+/=-]{8,}"
        ),
    ),
    (
        SensitiveKind.COOKIE_HEADER,
        "cookie_header",
        re.compile(r"(?i)\b(?:set-)?cookie\s*[:=]\s*[^\s;]{8,}"),
    ),
    (
        SensitiveKind.CONNECTION_STRING,
        "dsn_with_password",
        re.compile(
            r"(?i)\b[a-z][a-z0-9+.-]{2,}://[^\s:/@]+:[^\s:/@]+@[^\s/]+"
        ),
    ),
    (
        SensitiveKind.PROVIDER_TOKEN,
        "openai_style_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    ),
    (
        SensitiveKind.PROVIDER_TOKEN,
        "github_token",
        re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}\b"),
    ),
    (
        SensitiveKind.PROVIDER_TOKEN,
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        SensitiveKind.PROVIDER_TOKEN,
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        SensitiveKind.PROVIDER_TOKEN,
        "dashscope_key",
        re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    ),
    (
        SensitiveKind.JWT,
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        SensitiveKind.ENV_FILE_BODY,
        "env_assignment_line",
        re.compile(r"(?m)^[ \t]*[A-Z][A-Z0-9_]{2,}\s*=\s*\S+[ \t]*$"),
    ),
    # 与 ops 的赋值型模式保持同一口径（允许 key 前后有前后缀，如
    # POSTGRES_PASSWORD / api_key / db_secret 等）。
    (
        SensitiveKind.CREDENTIAL_ASSIGNMENT,
        "credential_assignment",
        re.compile(
            r"(?i)\b[A-Za-z0-9_-]*(?:password|passwd|pwd|api[_-]?key|"
            r"secret|token|credential|passphrase)[A-Za-z0-9_-]*\s*[:=]\s*"
            r"[^\s,;\"']{4,}"
        ),
    ),
)


def scan_sensitive(text: str) -> tuple[SensitiveFinding, ...]:
    """扫描文本里的敏感片段。返回的预览只保留形状，不回显原值。"""

    target = str(text or "")
    if not target:
        return ()
    findings: list[SensitiveFinding] = []
    seen: set[tuple[SensitiveKind, int, int]] = set()
    for kind, name, pattern in SENSITIVE_PATTERNS:
        for match in pattern.finditer(target):
            key = (kind, match.start(), match.end())
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                SensitiveFinding(
                    kind=kind,
                    pattern=name,
                    # 只给类型与长度，例如 "credential_assignment(len=31)"。
                    preview=f"{name}(len={match.end() - match.start()})",
                )
            )
    findings.sort(key=lambda item: (item.kind.value, item.pattern))
    return tuple(findings)


def contains_sensitive(text: str) -> bool:
    return bool(scan_sensitive(text))


def redact_text(
    text: str, *, max_chars: int | None = None
) -> tuple[str, tuple[SensitiveFinding, ...]]:
    """把敏感片段替换成 ``***``，可选再限长。

    返回 ``(脱敏后的文本, 命中列表)``。命中列表里没有原值，可以安全进 trace。
    """

    target = str(text or "")
    findings = scan_sensitive(target)
    if findings:
        # 收集真实区间后从后往前替换，避免位移影响后续区间。
        spans: list[tuple[int, int]] = []
        for kind, _name, pattern in SENSITIVE_PATTERNS:
            for match in pattern.finditer(target):
                spans.append((match.start(), match.end()))
        spans.sort()
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        pieces: list[str] = []
        cursor = 0
        for start, end in merged:
            pieces.append(target[cursor:start])
            pieces.append(REDACTION)
            cursor = end
        pieces.append(target[cursor:])
        target = "".join(pieces)

    if max_chars is not None and len(target) > max_chars:
        target = target[:max_chars] + "…"
    return target, findings


def safe_for_trace(
    text: str, *, max_chars: int = TRACE_CONTENT_MAX_CHARS
) -> str:
    """trace 专用渲染：先脱敏再限长。

    trace 是长期落盘文件，任何写进它的文本都必须过这里——
    "敏感数据不进入 trace" 是靠**唯一入口**保证的，而不是靠调用方自觉。
    """

    redacted, _ = redact_text(text, max_chars=max_chars)
    return redacted.replace("\n", " ").strip()


def _surviving_ratio(original: str, redacted: str) -> float:
    """脱敏后还剩下多少有效内容。用于判断"这条候选是否基本就是秘密本身"。"""

    def weight(value: str) -> int:
        return len(re.sub(r"\s+", "", value.replace(REDACTION, "")))

    before = weight(original)
    if before == 0:
        return 0.0
    return weight(redacted) / before


class RejectionReason(str, Enum):
    """策略层的拒绝原因。字符串化后进 trace 与候选表。"""

    MALFORMED = "malformed_candidate"
    EMPTY_CONTENT = "empty_content"
    TOO_SHORT = "content_too_short"
    TOO_LONG = "content_too_long"
    NO_SOURCE = "no_source"
    INVALID_SOURCE = "invalid_source"
    FABRICATED_SOURCE = "fabricated_source"
    SENSITIVE_ONLY = "sensitive_content_only"
    TRANSIENT_PREFERENCE = "transient_preference"
    SCOPE_NOT_ALLOWED = "scope_not_allowed"
    MISSING_PROJECT = "missing_project_scope"
    MISSING_EPISODE_ID = "missing_episode_id"
    DOMAIN_RULE = "domain_rule_violation"


@dataclass(frozen=True)
class TurnContext:
    """一个回合里"什么是合法证据、写到哪个作用域"的上下文。

    ``allowed_sources`` 是**白名单**：只有真实出现在本回合事件/工具结果里的
    ``(source_type, source_id)`` 才允许被候选引用。这是"模型不能编造来源"的落点。
    """

    tenant: Tenant
    observed_at: datetime
    allowed_sources: frozenset[tuple[str, str]] = frozenset()
    project_id: str | None = None
    allow_shared_ops: bool = False
    # 用户显式说"记住"时提高优先级（§7.2）；不影响任何校验，只影响调用方的调度。
    explicit_user_request: bool = False


@dataclass(frozen=True)
class PolicyVerdict:
    """一次策略审查的结论。``candidate`` 只在通过时非空。"""

    approved: bool
    reason: str
    candidate: MemoryCandidate | None = None
    reason_code: RejectionReason | None = None
    findings: tuple[SensitiveFinding, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def decision_reason(self) -> str:
        """给候选表/ trace 用的原因文本，已脱敏限长。"""

        return safe_for_trace(self.reason, max_chars=TRACE_REASON_MAX_CHARS)


# 偏好类的稳定性判断。用户说"这次先这样"和"以后都这样"是完全不同的两件事，
# 把前者写进长期画像会让后续任务被错误约束（计划 §6.3）。
_TRANSIENT_MARKERS = (
    "这次", "本次", "这一轮", "这轮", "当前这次", "现在先", "先这样", "临时",
    "暂时", "就这一次", "for now", "this time", "just this once", "temporarily",
)
_STABLE_MARKERS = (
    "以后", "今后", "从现在起", "一直", "始终", "永远", "默认", "每次", "记住",
    "以后都", "from now on", "always", "never", "by default", "every time",
)


class MemoryWritePolicy:
    """把候选审成"可入库的 MemoryCandidate"。

    参数都是刻意的可调项，默认值对应计划里的保守选择：

    * ``redact_sensitive=True``：命中敏感片段时**脱敏后保留**而不是整条拒绝。
      §7.1 把"敏感"列为 REJECT 条件，但那会连带丢掉与秘密同处一句的可用事实
      （例如"DSN 是 postgresql://u:p@h/db，库名 klonet_memory"）。
      脱敏保留了可用部分，且秘密同样不会进库、不会进 trace。
      需要严格拒绝时把它设成 False。
    * ``reject_fabricated_source=True``：候选引用了白名单外的来源就整条拒绝。
      只丢掉那一条来源是不够的——引用了不存在的证据说明这条候选整体不可信。
    """

    def __init__(
        self,
        *,
        redact_sensitive: bool = True,
        reject_fabricated_source: bool = True,
        reject_transient_preference: bool = True,
        content_max_chars: int = CANDIDATE_CONTENT_MAX_CHARS,
        min_content_chars: int = MIN_CONTENT_CHARS,
    ) -> None:
        self.redact_sensitive = redact_sensitive
        self.reject_fabricated_source = reject_fabricated_source
        self.reject_transient_preference = reject_transient_preference
        self.content_max_chars = content_max_chars
        self.min_content_chars = min_content_chars

    # ------------------------------------------------------------------ 出口 --

    def review(self, proposal: CandidateProposal, context: TurnContext) -> PolicyVerdict:
        """审查一条候选。任何拒绝都返回带原因的 verdict，不抛异常。"""

        warnings: list[str] = list(proposal.warnings)
        findings: list[SensitiveFinding] = []

        content, content_findings = redact_text(proposal.content)
        findings.extend(content_findings)
        summary = ""
        if proposal.summary:
            summary, summary_findings = redact_text(proposal.summary)
            findings.extend(summary_findings)

        if findings and not self.redact_sensitive:
            return self._reject(
                RejectionReason.SENSITIVE_ONLY,
                "候选命中敏感片段（口令/密钥/连接串等），按策略整条拒绝",
                findings,
            )
        if findings and _surviving_ratio(proposal.content, content) < REDACTION_SURVIVAL_RATIO:
            # 脱敏之后几乎什么都不剩，说明这条候选就是在抄秘密。
            return self._reject(
                RejectionReason.SENSITIVE_ONLY,
                "候选脱敏后剩余内容不足，判定为敏感内容本身，拒绝写入",
                findings,
            )

        normalized = normalize_content(content)
        if not normalized:
            return self._reject(
                RejectionReason.EMPTY_CONTENT, "候选正文为空", findings
            )
        if len(normalized) < self.min_content_chars:
            return self._reject(
                RejectionReason.TOO_SHORT,
                f"候选正文太短（{len(normalized)} < {self.min_content_chars}），"
                "不构成一条独立成立的记忆",
                findings,
            )
        if len(normalized) > self.content_max_chars:
            return self._reject(
                RejectionReason.TOO_LONG,
                f"候选正文超过 {self.content_max_chars} 字上限",
                findings,
            )

        sources_verdict = self._review_sources(
            proposal.sources, context, findings
        )
        if isinstance(sources_verdict, PolicyVerdict):
            sources_verdict = _with_warnings(sources_verdict, warnings)
            return sources_verdict
        sources = sources_verdict

        try:
            memory_type = MemoryType(proposal.memory_type)
            scope = Scope(proposal.scope)
        except ValueError:
            return self._reject(
                RejectionReason.MALFORMED,
                f"记忆类型或作用域非法：{proposal.memory_type!r}/{proposal.scope!r}",
                findings,
                warnings,
            )

        scope_verdict = self._review_scope(memory_type, scope, context)
        if scope_verdict is not None:
            return _with_warnings(scope_verdict, warnings)

        if (
            memory_type is MemoryType.PREFERENCE
            and self.reject_transient_preference
        ):
            marker = _transient_marker(proposal.content)
            if marker:
                return self._reject(
                    RejectionReason.TRANSIENT_PREFERENCE,
                    f"候选像一次性要求（命中“{marker}”），"
                    "临时要求只留在本轮上下文，不写进长期画像",
                    findings,
                    warnings,
                )
            if not _has_stable_marker(proposal.content):
                warnings.append("偏好没有显式稳定性措辞，按普通偏好处理")

        try:
            subject_key = build_subject_key(
                memory_type=memory_type,
                scope=None if memory_type is MemoryType.EPISODE else scope,
                entity=proposal.subject or None,
                attribute=proposal.attribute,
                episode_id=proposal.episode_id or None,
            )
        except MemoryDomainError as exc:
            return self._reject(
                RejectionReason.MALFORMED,
                f"无法构造 subject_key：{exc}",
                findings,
                warnings,
            )

        verified = bool(proposal.verified)
        if verified and not qualifies_as_verified(memory_type, sources):
            # 不整条拒绝：说法本身可能仍然有用，但"已验证"这个标记没有依据。
            warnings.append("候选自称已验证但证据等级不够，已降级为未验证")
            verified = False

        # §7.1：confidence 来自来源等级。取两者较小值，
        # 防止模型给一条只有对话事件的记忆打 0.99。
        ceiling = confidence_ceiling(sources)
        confidence = min(float(proposal.confidence), ceiling)
        if confidence < float(proposal.confidence):
            warnings.append(
                f"置信度按来源等级从 {proposal.confidence} 压到 {confidence}"
            )

        try:
            candidate = MemoryCandidate(
                memory_type=memory_type,
                scope=scope,
                subject_key=subject_key,
                content=normalized,
                user_id=context.tenant.user_id,
                summary=summary or None,
                project_id=context.project_id if scope is Scope.PROJECT else None,
                importance=importance_for_level(proposal.importance),
                confidence=confidence,
                verified=verified,
                observed_at=context.observed_at,
                sources=sources,
                proposed_decision=_proposed_decision(proposal),
            )
        except MemoryDomainError as exc:
            return self._reject(
                RejectionReason.DOMAIN_RULE,
                f"候选违反领域规则：{exc}",
                findings,
                warnings,
            )

        return PolicyVerdict(
            approved=True,
            reason="通过策略审查",
            candidate=candidate,
            findings=tuple(findings),
            warnings=tuple(warnings),
        )

    # ---------------------------------------------------------------- 内部 --

    def _review_sources(
        self,
        raw_sources: Sequence[RawSource],
        context: TurnContext,
        findings: list[SensitiveFinding],
    ) -> tuple[MemorySource, ...] | PolicyVerdict:
        if not raw_sources:
            return self._reject(
                RejectionReason.NO_SOURCE,
                "候选没有任何来源；无来源的记忆不写库（§7.1）",
                findings,
            )

        allowed = context.allowed_sources
        sources: list[MemorySource] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_sources:
            try:
                source_type = SourceType(raw.source_type)
            except ValueError:
                return self._reject(
                    RejectionReason.INVALID_SOURCE,
                    f"来源类型非法：{raw.source_type!r}",
                    findings,
                )
            key = (source_type.value, raw.source_id)
            if key not in allowed:
                # 白名单为空 = 本回合没有任何可引用的证据，此时任何来源都不可核对。
                # 刻意不做"白名单为空就放行"的兜底：那样一来运行时忘了传上下文，
                # "模型不能编造来源"这条保证会静默失效。
                if self.reject_fabricated_source:
                    return self._reject(
                        RejectionReason.FABRICATED_SOURCE,
                        f"候选引用了本回合证据之外的来源"
                        f"（{source_type.value}:{raw.source_id}），"
                        "无法核对，整条拒绝",
                        findings,
                    )
                continue
            if key in seen:
                continue
            seen.add(key)
            excerpt, excerpt_findings = redact_text(
                raw.excerpt, max_chars=TRACE_CONTENT_MAX_CHARS
            )
            findings.extend(excerpt_findings)
            try:
                sources.append(
                    MemorySource(
                        source_type=source_type,
                        source_id=raw.source_id,
                        observed_at=context.observed_at,
                        source_excerpt=excerpt,
                    )
                )
            except MemoryDomainError as exc:
                return self._reject(
                    RejectionReason.INVALID_SOURCE,
                    f"来源不合法：{exc}",
                    findings,
                )

        if not sources:
            return self._reject(
                RejectionReason.NO_SOURCE,
                "候选的来源全部不可核对，拒绝写入",
                findings,
            )
        return tuple(sources)

    def _review_scope(
        self, memory_type: MemoryType, scope: Scope, context: TurnContext
    ) -> PolicyVerdict | None:
        if scope is Scope.SHARED_OPS:
            if not context.allow_shared_ops:
                return self._reject(
                    RejectionReason.SCOPE_NOT_ALLOWED,
                    "当前会话没有运维共享权限，不能写 shared_ops 记忆",
                    (),
                )
            if memory_type is MemoryType.PREFERENCE:
                return self._reject(
                    RejectionReason.SCOPE_NOT_ALLOWED,
                    "shared_ops 不接受偏好类记忆（偏好一定有归属人）",
                    (),
                )
            return None
        if scope is Scope.PROJECT:
            if not context.project_id:
                return self._reject(
                    RejectionReason.MISSING_PROJECT,
                    "候选声明为项目作用域，但当前回合没有项目上下文",
                    (),
                )
            return None
        return None

    def _reject(
        self,
        code: RejectionReason,
        reason: str,
        findings: Sequence[SensitiveFinding],
        warnings: Sequence[str] = (),
    ) -> PolicyVerdict:
        return PolicyVerdict(
            approved=False,
            reason=reason,
            reason_code=code,
            findings=tuple(findings),
            warnings=tuple(warnings),
        )


def _with_warnings(verdict: PolicyVerdict, warnings: Sequence[str]) -> PolicyVerdict:
    if not warnings:
        return verdict
    merged = tuple(dict.fromkeys((*warnings, *verdict.warnings)))
    return PolicyVerdict(
        approved=verdict.approved,
        reason=verdict.reason,
        candidate=verdict.candidate,
        reason_code=verdict.reason_code,
        findings=verdict.findings,
        warnings=merged,
    )


def _transient_marker(text: str) -> str | None:
    lowered = str(text or "").lower()
    for marker in _TRANSIENT_MARKERS:
        if marker in lowered:
            return marker
    return None


def _has_stable_marker(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _STABLE_MARKERS)


def _proposed_decision(proposal: CandidateProposal) -> WriteDecision | None:
    if not proposal.proposed_decision:
        return None
    try:
        return WriteDecision(proposal.proposed_decision)
    except ValueError:
        return None


def allowed_source_ids(
    events: Sequence[Mapping[str, object]],
    *,
    tool_results: Sequence[Mapping[str, object]] = (),
) -> frozenset[tuple[str, str]]:
    """从回合事件里算出"可被引用的证据"白名单。

    两类来源：历史事件用它的 ``event_id``（``rows-<n>``），工具结果用工具调用 id。
    只有真实出现过的 id 进白名单——这就是模型不能编造来源的机制保证。
    """

    allowed: set[tuple[str, str]] = set()
    for event in events:
        # 工具结果用工具调用 id 登记：模型引用 `tool_result:<call_id>`。
        tool_call_id = str(event.get("tool_call_id") or "").strip()
        if tool_call_id:
            allowed.add((SourceType.TOOL_RESULT.value, tool_call_id))
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            continue
        role = str(event.get("role") or "")
        kind = (
            SourceType.USER_STATEMENT.value
            if role == "user"
            else SourceType.HISTORY_EVENT.value
        )
        allowed.add((kind, event_id))
    for result in tool_results:
        tool_call_id = str(result.get("tool_call_id") or result.get("id") or "").strip()
        if tool_call_id:
            allowed.add((SourceType.TOOL_RESULT.value, tool_call_id))
    return frozenset(allowed)
