"""确定性 consolidation：把一个候选变成受控的写入决策（阶段 2）。

这一层解决的问题是"谁来负责数据完整性"。计划 §6.4 的结论是：让主模型自由决定
"写什么、怎么覆盖"等于把完整性交给概率模型，所以模型只能提出**候选 + 提议**，
最终决策由这里确定：

- 纯函数 :func:`plan_consolidation` 只依赖领域对象，不碰数据库，因此可以离线测试；
- :func:`apply_plan` 把计划交给 repository 执行，是唯一的写入出口。

## 规则表

输入是「候选」和「该 subject 当前的 active 记忆（可能没有）」，按顺序匹配第一条：

============================  ============================================  ==================
条件                           依据                                          决策
============================  ============================================  ==================
候选没有任何来源               §7.1 REJECT「无来源」                          REJECT
候选显式提议 REJECT            §6.4 五种动作之一                              REJECT
该 subject 没有 active 记忆     —                                             ADD
内容 hash 相同，有新来源/更高    §7.1 UPDATE「只补充来源或置信度」               UPDATE
置信度
内容 hash 相同，无新信息         §7.1 NOOP「内容 hash 等价」                    NOOP
类型是 episode 且内容不同        §6.5 规则 3「情景记忆保留各自事件」            UPDATE
提议 UPDATE                     §7.1 UPDATE                                    UPDATE
提议 SUPERSEDE，旧值有证据而     §6.5 规则 4「更晚且已验证的事实优先」           REJECT
新值没有证据                    + 规则 5「无法判定则记为 contradicts」          (contradicts)
提议 SUPERSEDE，类型是偏好而     §6.5 规则 2「工具证据不得替用户决定偏好」       REJECT
新值不是用户明确陈述                                                          (contradicts)
提议 SUPERSEDE，其余情况         §7.1 SUPERSEDE「同一 subject 的有效值变化」    SUPERSEDE
提议 ADD，但同 subject 已有      §5.2 唯一索引「一个 subject 只能有一条          REJECT
active 记忆                     active」，两个"当前有效"版本会让召回自相矛盾
提议缺失而内容不同               保守失败：不允许静默覆盖                        REJECT
============================  ============================================  ==================

## 两个刻意的取舍

1. **"含义有没有变"不由 Python 判断。** 语义比较需要模型，Python 只负责
   权限、唯一性、版本和事务（§6.4）。所以候选必须显式带 ``proposed_decision``；
   缺失时一律 REJECT，而不是猜一个 ADD/UPDATE 出来。
2. **REJECT 不是静默丢弃。** 写不进去的候选会带着 reason 记进
   ``memory_write_candidates``，需要保留成矛盾关系时通过
   :attr:`ConsolidationPlan.suggested_relation` 交给调用方建 ``contradicts``
   关系——见 §6.5 规则 5「模型不得静默合并互斥事实」。

## UPDATE 有两种形态

§7.1 说 UPDATE 是"同一事实含义未变，只补充描述、来源或置信度"。这两件事的落地
方式不同，:func:`apply_plan` 按内容哈希自动分派：

* 正文变了 → ``add_version`` 追加一个不可变版本；
* 正文没变、只多了来源或置信度 → ``attach_sources`` 挂到当前版本上。

第二种不能走追加版本：版本表上有 ``(memory_id, content_hash)`` 唯一约束，
"同一段正文只存一份"是数据库级的保证，硬追加会直接撞约束。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from klonet_agent.memory.domain import (
    ConsolidationPlan,
    MemoryCandidate,
    MemoryRecord,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryVersion,
    RelationType,
    WriteDecision,
    additional_source_keys,
    normalize_content,
    qualifies_as_verified,
    same_content,
    type_allows_supersede,
)
from klonet_agent.memory.repository import (
    MemoryRepository,
    NewRecordCommand,
    NewVersionCommand,
    RecordNotFoundError,
)

__all__ = [
    "ConsolidationOutcome",
    "apply_plan",
    "plan_consolidation",
]

# 置信度比较的容差：float 从 real 列读回来之后有精度损失，
# 0.95 与 0.950000002 不应该被当成"置信度提高了"。
_CONFIDENCE_EPSILON = 1e-6


@dataclass(frozen=True)
class ConsolidationOutcome:
    """一次 consolidation 的执行结果。

    ``record`` / ``version`` 只在真正写入时非空；NOOP 与 REJECT 不会有它们，
    调用方必须按 ``plan.decision`` 分支，不能假定一定有新记忆。
    """

    plan: ConsolidationPlan
    record: MemoryRecord | None = None
    version: MemoryVersion | None = None

    @property
    def decision(self) -> WriteDecision:
        return self.plan.decision


# --------------------------------------------------------------------------- #
# 纯决策
# --------------------------------------------------------------------------- #


def plan_consolidation(
    candidate: MemoryCandidate,
    existing: MemoryRecord | None,
    *,
    existing_sources: Sequence[MemorySource] | None = None,
) -> ConsolidationPlan:
    """决定这个候选应该走哪条路。

    ``existing`` 是同一 ``subject_key`` 的当前 active 记忆（用
    ``repository.find_active_by_subject`` 取），没有就传 None。
    ``existing_sources`` 是它 active 版本的来源；不传时从
    ``existing.active_version.sources`` 读。
    """

    subject = candidate.subject_key

    # R1 无来源。没有证据的记忆不进库——这是 §7.1 明确列出的 REJECT 条件。
    if not candidate.sources:
        return _reject(subject, "候选没有任何来源，无来源的记忆不写库")

    # R2 模型自己判定不值得写（临时、敏感、低复用价值）。敏感信息过滤由阶段 3
    # 的 write_policy 负责，这里只是尊重一个显式的"不要写"。
    if candidate.proposed_decision is WriteDecision.REJECT:
        return _reject(subject, "候选被显式判为不可写")

    active = _active_record(existing)
    if active is None:
        # R3 该 subject 还没有当前有效版本。
        return ConsolidationPlan(
            decision=WriteDecision.ADD,
            reason="该 subject 暂无 active 记忆，作为新记忆写入",
            subject_key=subject,
        )

    previous = active.active_version
    known_sources = (
        tuple(existing_sources)
        if existing_sources is not None
        else (previous.sources if previous is not None else ())
    )

    # R4/R5 内容等价：先判重再谈决策，避免"同一句话反复入库"。
    if previous is not None and same_content(previous.content, candidate.content):
        new_keys = additional_source_keys(known_sources, candidate.sources)
        if new_keys:
            return ConsolidationPlan(
                decision=WriteDecision.UPDATE,
                reason=(
                    f"内容等价，但带来 {len(new_keys)} 条新来源，"
                    "作为同一事实的新版本补充证据"
                ),
                subject_key=subject,
                target_memory_id=active.id,
            )
        if candidate.confidence > active.confidence + _CONFIDENCE_EPSILON:
            return ConsolidationPlan(
                decision=WriteDecision.UPDATE,
                reason="内容等价，但置信度提高，作为同一事实的新版本记录",
                subject_key=subject,
                target_memory_id=active.id,
            )
        return ConsolidationPlan(
            decision=WriteDecision.NOOP,
            reason="已有内容等价的 active 记忆，且没有新来源或更高置信度",
            subject_key=subject,
            target_memory_id=active.id,
        )

    # R6 情景记忆不互相覆盖：同一个 episode id 出现新描述是"补充",
    # 不是"旧经历错了"。
    if not type_allows_supersede(candidate.memory_type):
        return ConsolidationPlan(
            decision=WriteDecision.UPDATE,
            reason=(
                f"{candidate.memory_type.value} 不参与替代，"
                "新描述作为同一事件的新版本追加"
            ),
            subject_key=subject,
            target_memory_id=active.id,
        )

    proposal = candidate.proposed_decision

    # R7 内容不同但提议 UPDATE：含义未变，只是描述/来源变了。
    if proposal is WriteDecision.UPDATE:
        return ConsolidationPlan(
            decision=WriteDecision.UPDATE,
            reason="候选声明为 UPDATE：同一事实含义未变，追加新版本",
            subject_key=subject,
            target_memory_id=active.id,
        )

    # R8 内容不同且没有提议：不允许猜。猜成 ADD 会撞唯一索引，猜成 SUPERSEDE
    # 会静默丢掉旧值——两者都比"记一条拒绝原因"危险。
    if proposal is not WriteDecision.SUPERSEDE:
        return _reject(
            subject,
            "同一 subject 已有 active 记忆且候选内容不同，"
            "但候选没有声明 UPDATE 还是 SUPERSEDE（不允许静默覆盖）",
        )

    new_can_verify = qualifies_as_verified(candidate.memory_type, candidate.sources)
    old_verified = bool(previous and previous.verified) or qualifies_as_verified(
        active.memory_type, known_sources
    )

    # R9 偏好是主观的：只有用户明确陈述才能改。
    if candidate.memory_type is MemoryType.PREFERENCE and not new_can_verify:
        return _reject(
            subject,
            "偏好只能由用户明确陈述改写，工具证据不得替用户决定主观偏好",
            relation=RelationType.CONTRADICTS,
        )

    # R10 旧值有证据、新值没有：更晚的未验证说法不能推翻已验证事实，
    # 按 §6.5 规则 5 保留为矛盾而不是替代。
    if old_verified and not new_can_verify:
        return _reject(
            subject,
            "已存在有证据支持的当前值，未经证据支持的新值不能替代它；"
            "应记为 contradicts 并等待证据",
            relation=RelationType.CONTRADICTS,
        )

    return ConsolidationPlan(
        decision=WriteDecision.SUPERSEDE,
        reason="同一 subject 的有效值发生变化，新记忆替代旧记忆并结束旧值有效期",
        subject_key=subject,
        target_memory_id=active.id,
    )


def _active_record(existing: MemoryRecord | None) -> MemoryRecord | None:
    """只有 active 状态的记录才算"当前有效版本"。"""

    if existing is None:
        return None
    return existing if existing.status is MemoryStatus.ACTIVE else None


def _reject(
    subject_key: str, reason: str, *, relation: RelationType | None = None
) -> ConsolidationPlan:
    return ConsolidationPlan(
        decision=WriteDecision.REJECT,
        reason=reason,
        subject_key=subject_key,
        target_memory_id=None,
        suggested_relation=relation,
    )


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #


def apply_plan(
    repository: MemoryRepository,
    plan: ConsolidationPlan,
    candidate: MemoryCandidate,
    *,
    candidate_id: str | None = None,
) -> ConsolidationOutcome:
    """执行 consolidation 计划。

    **顺序是"先写正文、后记决策"**，这是刻意的：如果反过来，写入失败会在候选表里
    留下一条"已 ADD"的假审计，而重放又不会发生；现在这个顺序下，记账失败只会让
    候选停留在未处理状态，最坏结果是审计里少一条记录，不会出现没有审计的正式记忆。

    正文写入与候选决策各自是原子事务，但两者之间有一个极小的窗口。
    把整条管线收进同一个事务是阶段 3（受控写入管线）的工作，见计划 §6.4。

    REJECT 建议的矛盾关系（``plan.suggested_relation``）不在这里落地：矛盾双方
    是两条独立的逻辑记忆，需要调用方先写入新记忆再建关系，属于管线职责。
    """

    outcome = ConsolidationOutcome(plan=plan)

    if plan.decision is WriteDecision.ADD:
        record = repository.add_record(_as_new_record(candidate))
        outcome = ConsolidationOutcome(
            plan=plan, record=record, version=record.active_version
        )
    elif plan.decision is WriteDecision.UPDATE:
        if not plan.target_memory_id:
            raise RecordNotFoundError("UPDATE 计划缺少目标记忆 id")
        target = repository.get_record(plan.target_memory_id)
        current = target.active_version if target is not None else None
        if current is not None and same_content(current.content, candidate.content):
            # 内容没变，只有来源/置信度变了：挂到当前版本上，不追加新版本。
            # 追加一个内容等价的版本会撞 `(memory_id, content_hash)` 唯一约束——
            # 那条约束正是版本表"同一段正文只存一份"的保证，这里顺着它走。
            version = repository.attach_sources(
                plan.target_memory_id,
                candidate.sources,
                confidence=candidate.confidence,
                verified=candidate.verified,
            )
            record = repository.get_record(plan.target_memory_id)
        else:
            version = repository.add_version(
                _as_new_version(plan.target_memory_id, candidate)
            )
            record = repository.get_record(plan.target_memory_id)
        outcome = ConsolidationOutcome(plan=plan, record=record, version=version)
    elif plan.decision is WriteDecision.SUPERSEDE:
        if not plan.target_memory_id:
            raise RecordNotFoundError("SUPERSEDE 计划缺少被替代的记忆 id")
        record = repository.replace_active(
            plan.target_memory_id,
            _as_new_record(candidate),
            relation_confidence=1.0,
            reason=plan.reason,
        )
        outcome = ConsolidationOutcome(
            plan=plan, record=record, version=record.active_version
        )

    if candidate_id:
        repository.record_decision(
            candidate_id, plan.decision, reason=plan.reason
        )
    return outcome


def _as_new_record(candidate: MemoryCandidate) -> NewRecordCommand:
    return NewRecordCommand(
        user_id=candidate.user_id,
        scope=candidate.scope,
        memory_type=candidate.memory_type,
        subject_key=candidate.subject_key,
        content=normalize_content(candidate.content),
        project_id=candidate.project_id,
        summary=candidate.summary,
        importance=candidate.importance,
        confidence=candidate.confidence,
        verified=candidate.verified,
        observed_at=candidate.observed_at,
        valid_from=candidate.valid_from,
        metadata=candidate.metadata,
        sources=candidate.sources,
    )


def _as_new_version(memory_id: str, candidate: MemoryCandidate) -> NewVersionCommand:
    return NewVersionCommand(
        memory_id=memory_id,
        content=normalize_content(candidate.content),
        summary=candidate.summary,
        observed_at=candidate.observed_at,
        valid_from=candidate.valid_from,
        metadata=candidate.metadata,
        sources=candidate.sources,
        verified=candidate.verified,
        confidence=candidate.confidence,
    )
