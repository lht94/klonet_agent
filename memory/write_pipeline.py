"""受控写入管线（阶段 3）。

把计划 §6.4 的流程图落成代码：

```
事件范围 → 提取候选 → 敏感信息过滤 → 判断是否值得长期保存
        → 检索相似现有记忆 → ADD/UPDATE/SUPERSEDE/NOOP/REJECT
        → 数据库事务 → embedding outbox
```

三段职责的边界（这是本文件存在的意义）：

* ``candidate_extractor``：**说**了什么（形状解析，不判断）；
* ``write_policy``：**能不能写**（敏感、作用域、稳定性、来源可核对）；
* ``versioning``：**怎么写**（对已有记忆做 ADD/UPDATE/SUPERSEDE/NOOP/REJECT）；
* 本文件：把它们串起来，并负责**审计**与**幂等**。

## 幂等：按事件区间

计划 §7.2 要求"session 结束和 checkpoint 只负责扫描尚未处理的事件区间，
**不能重复处理已有 source range**"。落点是把事件区间当作幂等键：

* 每个回合的事件区间 ``{"start": "rows-5", "end": "rows-9"}`` 决定一个区间键；
* 区间内第 i 条候选的幂等键是 ``"rows-5..rows-9#i"``；
* 区间是否已处理，由候选表里已登记的 ``source_event_range`` 判定——
  候选表本身既是审计日志，也是处理台账，不额外引入第二份状态。

## 失败方向

* 提取失败（模型挂了）→ 返回带 ``error`` 的结果，**不抛异常**，不影响用户回答；
* 单条候选的任何问题 → 只影响它自己；
* 敏感候选**绝不落库、绝不进 trace**（连被拒原因里的原文都不会有）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from klonet_agent.memory.candidate_extractor import (
    CandidateExtractor,
    CandidateProposal,
    ExtractionError,
    RawSource,
    TurnDigest,
)
from klonet_agent.memory.domain import (
    ConsolidationPlan,
    WriteDecision,
    content_hash,
)
from klonet_agent.memory.repository import CandidateNotFoundError, MemoryRepository
from klonet_agent.memory.versioning import (
    ConsolidationOutcome,
    apply_plan,
    plan_consolidation,
)
from klonet_agent.memory.write_policy import (
    MemoryWritePolicy,
    PolicyVerdict,
    TurnContext,
    redact_text,
    safe_for_trace,
)

__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "CandidateOutcome",
    "LegacyMemoryToolBridge",
    "MemoryWriteTracer",
    "NullWriteTracer",
    "TurnOutcome",
    "MemoryWritePipeline",
    "range_key",
]

# 与 candidate_extractor 的合同版本保持一致；写进候选 payload 便于事后解析。
CANDIDATE_SCHEMA_VERSION = 1

# 一次补扫最多处理多少个区间，避免 checkpoint 之后积压太多时一次跑太久。
DEFAULT_BACKFILL_LIMIT = 20


class MemoryWriteTracer(Protocol):
    """候选与决策的审计出口。

    实现必须自己对内容做脱敏限长——管线传进来的已经过 ``safe_for_trace``，
    但 trace 是长期落盘文件，多一道闸不亏（见 ``tracing/logger.py``）。
    """

    def record_memory_candidate(
        self,
        *,
        user_id: str,
        project_id: str | None,
        stage: str,
        memory_type: str,
        scope: str,
        subject_key: str,
        decision: str,
        reason: str,
        content_preview: str,
        source_event_range: Mapping[str, Any],
        redactions: Sequence[str] = (),
        warnings: Sequence[str] = (),
    ) -> None:
        ...


class NullWriteTracer:
    """不记录任何东西的 tracer。

    **只在"没有配置 trace 文件"时使用**（例如单元测试）；生产路径必须给真的
    出口，否则"决策可审计"就是空话。
    """

    def record_memory_candidate(self, **kwargs: Any) -> None:  # noqa: D102 - 见 Protocol
        return None


@dataclass(frozen=True)
class CandidateOutcome:
    """一条候选从提出到落地的完整经过。"""

    proposal: CandidateProposal
    verdict: PolicyVerdict | None = None
    plan: ConsolidationPlan | None = None
    applied: ConsolidationOutcome | None = None
    # 真正生效的决策（策略拒绝时没有 consolidation 计划）。
    decision: WriteDecision | None = None
    reason: str = ""
    persisted_candidate_id: str | None = None

    @property
    def wrote(self) -> bool:
        return bool(self.applied and self.applied.record is not None)


@dataclass(frozen=True)
class TurnOutcome:
    """一个回合（或一次补扫区间）的处理结果。"""

    source_event_range: Mapping[str, Any]
    outcomes: tuple[CandidateOutcome, ...] = ()
    parse_rejections: tuple[str, ...] = ()
    error: str | None = None
    skipped: bool = False

    @property
    def accepted(self) -> tuple[CandidateOutcome, ...]:
        return tuple(item for item in self.outcomes if item.wrote)

    @property
    def rejected(self) -> tuple[CandidateOutcome, ...]:
        return tuple(item for item in self.outcomes if not item.wrote)

    @property
    def ok(self) -> bool:
        return self.error is None


def range_key(source_event_range: Mapping[str, Any] | None) -> str:
    """把事件区间压成可比较的字符串键。

    `source_event_end` 是**半开区间**（见上下文管理阶段的约定），
    所以键里保留 start/end 两个端点，不自己算"覆盖了几条"。
    """

    payload = dict(source_event_range or {})
    start = str(payload.get("start") or "")
    end = str(payload.get("end") or "")
    return f"{start}..{end}"


def _normalized_range(source_event_range: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(source_event_range or {})
    return {
        "start": str(payload.get("start") or ""),
        "end": str(payload.get("end") or ""),
    }


class MemoryWritePipeline:
    """把一次回合的候选收进受控写入路径。

    一个实例绑定一个租户的 repository（``PostgresMemoryRepository`` 已经这么做了），
    因此这里不再接租户参数——没有"忘了传 user_id"的调用方式。
    """

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        extractor: CandidateExtractor | None = None,
        policy: MemoryWritePolicy | None = None,
        tracer: MemoryWriteTracer | None = None,
        backfill_limit: int = DEFAULT_BACKFILL_LIMIT,
    ) -> None:
        self._repository = repository
        self._extractor = extractor or CandidateExtractor()
        self._policy = policy or MemoryWritePolicy()
        self._tracer = tracer or NullWriteTracer()
        self._backfill_limit = max(1, int(backfill_limit))

    @property
    def extractor(self) -> CandidateExtractor:
        return self._extractor

    # ------------------------------------------------------------ 回合入口 --

    def process_turn(
        self,
        digest: TurnDigest,
        context: TurnContext,
        *,
        proposals: Sequence[CandidateProposal] | None = None,
    ) -> TurnOutcome:
        """处理"一个用户回合及其工具循环持久化之后"的候选提取。

        ``proposals`` 用于两种情况：测试注入，以及**旧记忆工具**提交候选
        （工具已经把内容整理成候选，不需要再花一次模型调用）。
        为 None 时走提取器。

        这个方法**不抛异常**：任何失败都进 ``TurnOutcome.error``。
        计划 §7.2 明确要求候选失败不影响用户回答。
        """

        source_range = _normalized_range(digest.source_event_range)
        key = range_key(source_range)

        try:
            if self._range_already_processed(source_range):
                return TurnOutcome(
                    source_event_range=source_range, skipped=True
                )
        except Exception as exc:  # noqa: BLE001 - 台账查询失败不能中断回合
            return TurnOutcome(
                source_event_range=source_range,
                error=f"读取写入台账失败：{safe_for_trace(str(exc))}",
            )

        parse_rejections: tuple[str, ...] = ()
        if proposals is None:
            try:
                proposals, parse_rejections = self._extractor.extract(digest)
            except ExtractionError as exc:
                return TurnOutcome(
                    source_event_range=source_range,
                    error=f"候选提取失败：{safe_for_trace(str(exc))}",
                )

        outcomes: list[CandidateOutcome] = []
        for index, proposal in enumerate(proposals):
            outcomes.append(
                self._process_one(
                    proposal,
                    context,
                    source_range=source_range,
                    idempotency_key=f"{key}#{index}",
                )
            )

        for reason in parse_rejections:
            self._trace(
                context,
                stage="parse_rejected",
                memory_type="",
                scope="",
                subject_key="",
                decision=WriteDecision.REJECT.value,
                reason=reason,
                content="",
                source_range=source_range,
            )

        return TurnOutcome(
            source_event_range=source_range,
            outcomes=tuple(outcomes),
            parse_rejections=parse_rejections,
        )

    def process_proposal(
        self,
        proposal: CandidateProposal,
        context: TurnContext,
        *,
        source_event_range: Mapping[str, Any],
        idempotency_key: str,
    ) -> CandidateOutcome:
        """单条候选的入口。旧记忆工具走这里。"""

        return self._process_one(
            proposal,
            context,
            source_range=_normalized_range(source_event_range),
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------ 补扫入口 --

    def backfill(
        self, items: Iterable[tuple[TurnDigest, TurnContext]]
    ) -> list[TurnOutcome]:
        """补扫尚未处理的区间（session 结束 / checkpoint 之后调用）。

        ``items`` 由调用方从事件日志里构造（它才知道哪些区间"应当被处理"）。
        这里只负责跳过已处理的、并限制单次处理量。
        """

        results: list[TurnOutcome] = []
        for digest, context in items:
            if len(results) >= self._backfill_limit:
                break
            outcome = self.process_turn(digest, context)
            if outcome.skipped:
                continue
            results.append(outcome)
        return results

    # ------------------------------------------------------- 旧工具审计出口 --

    def record_blocked_legacy_write(
        self,
        *,
        tool_name: str,
        content: str,
        context: TurnContext,
        event_id: str | None = None,
    ) -> str | None:
        """登记一次"整篇覆盖"尝试（旧工具被拒时的审计）。

        与策略拒绝一样分两档：命中敏感片段的只进 trace，**绝不落库**；
        其余在候选表里留一条可检索的记录，便于统计"模型还在用老方式写记忆"的频率。
        """

        redacted, findings = redact_text(content)
        reason = (
            f"{tool_name} 的整篇覆盖已被拒绝：长期记忆改为受控写入，"
            "工具不能整体替换它"
        )
        anchor = str(event_id or "").strip() or "rows-unknown"
        source_range = {
            "start": anchor,
            "end": anchor,
            "origin": f"tool:{tool_name}",
        }
        self._trace(
            context,
            stage="legacy_overwrite_blocked",
            memory_type="fact" if tool_name == "write_memory" else "preference",
            scope="project" if context.project_id else "user",
            subject_key="",
            decision=WriteDecision.REJECT.value,
            reason=reason,
            content=redacted,
            source_range=source_range,
            redactions=findings,
        )
        if findings:
            return None

        payload = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "stage": "legacy_overwrite_blocked",
            "reason_code": "legacy_tool_not_authoritative",
            "reason": safe_for_trace(reason),
            "tool_name": tool_name,
            "content_preview": safe_for_trace(redacted),
        }
        key = f"tool:{tool_name}:{anchor}:{content_hash(content)[:16]}"
        try:
            return self._repository.add_candidate_payload(
                payload=payload,
                user_id=context.tenant.user_id,
                project_id=context.project_id,
                idempotency_key=key,
                source_event_range=source_range,
            )
        except Exception:  # noqa: BLE001 - 审计失败不升级为错误
            return None

    # ---------------------------------------------------------------- 内部 --

    def _range_already_processed(self, source_range: Mapping[str, Any]) -> bool:
        target = range_key(source_range)
        for known in self._repository.processed_source_ranges(
            limit=self._backfill_limit * 10
        ):
            if range_key(known) == target and target != "..":
                return True
        return False

    def _process_one(
        self,
        proposal: CandidateProposal,
        context: TurnContext,
        *,
        source_range: Mapping[str, Any],
        idempotency_key: str,
    ) -> CandidateOutcome:
        verdict = self._policy.review(proposal, context)

        if not verdict.approved or verdict.candidate is None:
            stage = "policy_rejected"
            self._trace(
                context,
                stage=stage,
                memory_type=proposal.memory_type,
                scope=proposal.scope,
                subject_key="",
                decision=WriteDecision.REJECT.value,
                reason=verdict.reason,
                content=proposal.content,
                source_range=source_range,
                redactions=verdict.findings,
                warnings=verdict.warnings,
            )
            candidate_id = self._persist_rejection(
                proposal,
                verdict,
                context,
                source_range=source_range,
                idempotency_key=idempotency_key,
            )
            return CandidateOutcome(
                proposal=proposal,
                verdict=verdict,
                decision=WriteDecision.REJECT,
                reason=verdict.reason,
                persisted_candidate_id=candidate_id,
            )

        candidate = verdict.candidate
        try:
            existing = self._repository.find_active_by_subject(candidate.subject_key)
            existing_sources = (
                self._repository.list_sources(existing.active_version_id)
                if existing is not None and existing.active_version_id
                else None
            )
            plan = plan_consolidation(
                candidate, existing, existing_sources=existing_sources
            )
        except Exception as exc:  # noqa: BLE001 - 读路径失败不能中断回合
            reason = f"检索现有记忆失败：{safe_for_trace(str(exc))}"
            self._trace(
                context,
                stage="lookup_failed",
                memory_type=candidate.memory_type.value,
                scope=candidate.scope.value,
                subject_key=candidate.subject_key,
                decision=WriteDecision.REJECT.value,
                reason=reason,
                content=candidate.content,
                source_range=source_range,
                warnings=verdict.warnings,
            )
            return CandidateOutcome(
                proposal=proposal,
                verdict=verdict,
                decision=WriteDecision.REJECT,
                reason=reason,
            )

        candidate_id = self._repository.add_candidate(
            candidate,
            idempotency_key=idempotency_key,
            source_event_range=dict(source_range),
        )

        try:
            applied = apply_plan(
                self._repository, plan, candidate, candidate_id=candidate_id
            )
        except CandidateNotFoundError:
            # 幂等路径：这条候选已经决策过了（同一个区间被重复处理，例如
            # 同一行里重复调用 append_episode）。不是错误，也不该报"写入失败"。
            reason = "该候选已经处理过，按幂等跳过"
            self._trace(
                context,
                stage="duplicate",
                memory_type=candidate.memory_type.value,
                scope=candidate.scope.value,
                subject_key=candidate.subject_key,
                decision=WriteDecision.NOOP.value,
                reason=reason,
                content=candidate.content,
                source_range=source_range,
            )
            return CandidateOutcome(
                proposal=proposal,
                verdict=verdict,
                plan=plan,
                decision=WriteDecision.NOOP,
                reason=reason,
                persisted_candidate_id=candidate_id,
            )
        except Exception as exc:  # noqa: BLE001 - 写失败同样只是"这次没写成"
            reason = f"写入失败：{safe_for_trace(str(exc))}"
            self._trace(
                context,
                stage="write_failed",
                memory_type=candidate.memory_type.value,
                scope=candidate.scope.value,
                subject_key=candidate.subject_key,
                decision=plan.decision.value,
                reason=reason,
                content=candidate.content,
                source_range=source_range,
                warnings=verdict.warnings,
            )
            return CandidateOutcome(
                proposal=proposal,
                verdict=verdict,
                plan=plan,
                decision=plan.decision,
                reason=reason,
                persisted_candidate_id=candidate_id,
            )

        self._trace(
            context,
            stage="applied",
            memory_type=candidate.memory_type.value,
            scope=candidate.scope.value,
            subject_key=candidate.subject_key,
            decision=plan.decision.value,
            reason=plan.reason,
            content=candidate.content,
            source_range=source_range,
            redactions=verdict.findings,
            warnings=verdict.warnings,
        )
        return CandidateOutcome(
            proposal=proposal,
            verdict=verdict,
            plan=plan,
            applied=applied,
            decision=plan.decision,
            reason=plan.reason,
            persisted_candidate_id=candidate_id,
        )

    def _persist_rejection(
        self,
        proposal: CandidateProposal,
        verdict: PolicyVerdict,
        context: TurnContext,
        *,
        source_range: Mapping[str, Any],
        idempotency_key: str,
    ) -> str | None:
        """把"安全但被拒"的候选记进候选表，供事后审计。

        **命中敏感片段的候选不写库**：候选表也是数据库的一部分，
        把含密钥的正文写进去等于把秘密落了盘。这类拒绝只留在 trace，
        而且 trace 里也只有脱敏后的预览。
        """

        if verdict.findings:
            return None
        payload = {
            "schema_version": CANDIDATE_SCHEMA_VERSION,
            "stage": "policy_rejected",
            "reason_code": verdict.reason_code.value if verdict.reason_code else "",
            "reason": verdict.decision_reason,
            "memory_type": proposal.memory_type,
            "scope": proposal.scope,
            "entity": proposal.subject,
            "attribute": proposal.attribute,
            "episode_id": proposal.episode_id,
            "content_preview": safe_for_trace(proposal.content),
            "proposed_decision": proposal.proposed_decision or "",
            "raw_index": proposal.raw_index,
            "warnings": [safe_for_trace(item, max_chars=120) for item in verdict.warnings],
        }
        try:
            return self._repository.add_candidate_payload(
                payload=payload,
                user_id=context.tenant.user_id,
                project_id=context.project_id,
                idempotency_key=idempotency_key,
                source_event_range=dict(source_range),
            )
        except Exception:  # noqa: BLE001 - 审计写入失败不该升级成回合失败
            return None

    def _trace(
        self,
        context: TurnContext,
        *,
        stage: str,
        memory_type: str,
        scope: str,
        subject_key: str,
        decision: str,
        reason: str,
        content: str,
        source_range: Mapping[str, Any],
        redactions: Sequence[Any] = (),
        warnings: Sequence[str] = (),
    ) -> None:
        """统一的 trace 出口：内容与原因都先过 ``safe_for_trace``。"""

        try:
            self._tracer.record_memory_candidate(
                user_id=context.tenant.user_id,
                project_id=context.project_id,
                stage=stage,
                memory_type=memory_type,
                scope=scope,
                subject_key=subject_key,
                decision=decision,
                reason=safe_for_trace(reason),
                content_preview=safe_for_trace(content),
                source_event_range=dict(source_range),
                redactions=[getattr(item, "preview", "") for item in redactions],
                warnings=[safe_for_trace(item, max_chars=120) for item in warnings],
            )
        except Exception:  # noqa: BLE001 - trace 失败不能影响写入结论
            return None


# --------------------------------------------------------------------------- #
# 旧记忆工具 -> 受控管线（兼容代理）
# --------------------------------------------------------------------------- #


class LegacyMemoryToolBridge:
    """把 `append_episode` / `write_memory` / `write_user` 接到受控管线上。

    计划 §6.4 要取消的正是"主模型直接决定写什么"这件事，但三个旧工具还在工具表里、
    模型也还在按老提示词调用它们。这个适配层的做法是：

    * ``append_episode``（记录今日事件）→ **真候选提交**：一条 episode 候选，
      事件身份用当前事件行号，走完整的策略 + consolidation 路径。
      这本来就是它在新版里的语义（计划 §6.1：情景记忆升级为原子 Episode）。
    * ``write_memory`` / ``write_user``（整篇覆盖）→ **不再有写入权限**：
      调用会被审计并返回一段说明，告诉模型改用什么方式表达。整篇 Markdown
      解析成 fact/preference 属于阶段 6 的 ``memory/migration.py``，
      在这里再写一个 Markdown 解析器会产生两份口径。

    ``context_provider`` 由运行时注入（它知道当前回合的证据行号）；测试里注入固定值。
    """

    def __init__(
        self,
        pipeline: "MemoryWritePipeline",
        *,
        context_provider: Any,
        event_id_provider: Any,
    ) -> None:
        self._pipeline = pipeline
        self._context_provider = context_provider
        self._event_id_provider = event_id_provider

    def submit_episode(self, content: str) -> str:
        """`append_episode` 的新实现：提交一条 episode 候选。"""

        text = str(content or "").strip()
        if not text:
            return "Error: append_episode 的 content 为空"

        context = self._context_provider()
        event_id = str(self._event_id_provider() or "").strip()
        source_id = event_id or "rows-unknown"
        source_range = {"start": source_id, "end": source_id, "origin": "tool:append_episode"}
        proposal = CandidateProposal(
            memory_type="episode",
            scope="project" if context.project_id else "user",
            content=text,
            episode_id=source_id,
            importance="high",
            confidence=0.6,
            verified=False,
            proposed_decision="add",
            sources=(
                RawSource(
                    source_type="history_event",
                    source_id=source_id,
                    excerpt="",
                ),
            ),
        )
        outcome = self._pipeline.process_proposal(
            proposal,
            context,
            source_event_range=source_range,
            # 用正文哈希做键：同一行里记录两条不同的经历都应当留下，
            # 而同一段文本重复提交只算一次。
            idempotency_key=f"tool:append_episode:{source_id}:{content_hash(text)[:16]}",
        )
        if outcome.wrote:
            return "已把这次经历提交为一条情景记忆候选，并已通过受控写入。"
        return (
            "这次经历没有写入长期记忆："
            f"{safe_for_trace(outcome.reason or '未通过策略审查')}"
        )

    def block_overwrite(self, tool_name: str, content: str) -> str:
        """`write_memory` / `write_user` 的新实现：审计 + 拒绝整篇覆盖。"""

        text = str(content or "")
        context = self._context_provider()
        self._pipeline.record_blocked_legacy_write(
            tool_name=tool_name,
            content=text,
            context=context,
            event_id=str(self._event_id_provider() or ""),
        )
        target = "项目事实" if tool_name == "write_memory" else "用户偏好"
        return (
            f"已拒绝 {tool_name} 的整篇覆盖：长期记忆现在是受控写入，"
            f"任何一次工具调用都不能整体替换它。\n"
            f"请改用结构化方式表达 {target}：\n"
            f"- 具体经历继续用 append_episode；\n"
            f"- 需要长期保留的{target}会在回合结束后由受控管线从对话里提取，"
            f"你只需要在回答里把它讲清楚、并说明依据。"
        )
