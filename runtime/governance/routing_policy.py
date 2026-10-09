"""确定性路由策略（计划 §4.5，阶段 5）。

路由是**受约束决策**而不是按供应商名写 if/else：

1. 硬能力不满足的模型先剔除（capability / context / cost / latency / privacy）；
2. 再在可行集合里按 质量 → 健康 → 成本 → 延迟 的确定性权重评分；
3. 每次选择产出 :class:`RouteDecision`：候选集、排除原因（reason code）、
   选中与策略版本——可解释、可回放。

计划约束：首版 shadow，不控制生产流量；学习型路由器暂不实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from klonet_agent.runtime.governance.capabilities import (
    ModelCapability,
    ModelCapabilityRegistry,
    RouteReasonCode,
    TaskRequirements,
)
from klonet_agent.runtime.governance.models import (
    GovernanceDomainError,
    new_id,
    utcnow,
)

# 策略版本：权重或规则变化时递增，用于回放对比。
POLICY_VERSION = "deterministic-1"

# 确定性权重（和为 1）。质量用"有没有冻结 eval 基线"作保守代理：
# 有基线 1.0，没有 0.5——这是刻意保守，鼓励先补 eval 再谈路由优化。
_WEIGHTS = {"quality": 0.40, "health": 0.25, "cost": 0.20, "latency": 0.15}


@dataclass
class RouteDecision:
    """一次路由决策（可解释、可回放）。"""

    decision_id: str
    run_id: str
    user_id: str
    project_id: str | None
    task_level: str
    selected: str
    candidate_scores: dict[str, float]
    excluded: dict[str, list[str]]          # model_id -> reason codes
    reason_codes: list[str]
    policy_version: str = POLICY_VERSION
    actual_model: str | None = None
    shadow: bool = True
    occurred_at: Any = field(default_factory=utcnow)


def hard_filter(
    registry: ModelCapabilityRegistry, requirements: TaskRequirements
) -> tuple[list[ModelCapability], dict[str, list[str]]]:
    """硬约束过滤：不满足即剔除，并给出 reason code。"""

    feasible: list[ModelCapability] = []
    excluded: dict[str, list[str]] = {}
    for model in registry.all():
        if requirements.task_level not in model.task_levels:
            excluded.setdefault(model.model_id, []).append(
                RouteReasonCode.EXCLUDED_CAPABILITY
            )
            continue
        if requirements.needs_tool_calling and not model.tool_calling:
            excluded.setdefault(model.model_id, []).append(
                RouteReasonCode.EXCLUDED_CAPABILITY
            )
            continue
        if requirements.needs_structured_output and not model.structured_output:
            excluded.setdefault(model.model_id, []).append(
                RouteReasonCode.EXCLUDED_CAPABILITY
            )
            continue
        if requirements.needs_vision and not model.vision:
            excluded.setdefault(model.model_id, []).append(
                RouteReasonCode.EXCLUDED_CAPABILITY
            )
            continue
        if model.context_window < requirements.min_context_tokens:
            excluded.setdefault(model.model_id, []).append(
                RouteReasonCode.EXCLUDED_CONTEXT
            )
            continue
        if (
            requirements.max_cost_per_1k_input is not None
            and model.cost_per_1k_input > requirements.max_cost_per_1k_input
        ):
            excluded.setdefault(model.model_id, []).append(RouteReasonCode.EXCLUDED_COST)
            continue
        if (
            requirements.max_latency_ms_p50 is not None
            and model.latency_ms_p50 > requirements.max_latency_ms_p50
        ):
            excluded.setdefault(model.model_id, []).append(
                RouteReasonCode.EXCLUDED_LATENCY
            )
            continue
        if not model.supports_privacy(requirements.privacy_max_class):
            excluded.setdefault(model.model_id, []).append(
                RouteReasonCode.EXCLUDED_PRIVACY
            )
            continue
        feasible.append(model)
    return feasible, excluded


def score_candidates(
    feasible: list[ModelCapability],
    requirements: TaskRequirements,
) -> dict[str, float]:
    """确定性评分：quality(0.4) + health(0.25) + cost(0.2) + latency(0.15)。

    cost/latency 分数做集合内归一化（相对最优者得 1.0），避免绝对值量纲
    影响权重解释；集合只有一个候选时两项都给 1.0。
    """

    if not feasible:
        return {}
    costs = [m.cost_per_1k_input for m in feasible]
    latencies = [m.latency_ms_p50 for m in feasible]
    min_cost, max_cost = min(costs), max(costs)
    min_lat, max_lat = min(latencies), max(latencies)

    def _norm(value: float, low: float, high: float) -> float:
        if high <= low:
            return 1.0
        return 1.0 - (value - low) / (high - low)

    scores: dict[str, float] = {}
    for model in feasible:
        quality = 1.0 if model.eval_baseline else 0.5
        cost_score = _norm(model.cost_per_1k_input, min_cost, max_cost)
        latency_score = _norm(float(model.latency_ms_p50), float(min_lat), float(max_lat))
        total = (
            _WEIGHTS["quality"] * quality
            + _WEIGHTS["health"] * model.health
            + _WEIGHTS["cost"] * cost_score
            + _WEIGHTS["latency"] * latency_score
        )
        scores[model.model_id] = round(total, 6)
    return scores


def decide_route(
    registry: ModelCapabilityRegistry,
    requirements: TaskRequirements,
    *,
    run_id: str,
    user_id: str,
    project_id: str | None,
    actual_model: str | None = None,
    shadow: bool = True,
) -> RouteDecision | None:
    """完整路由决策：硬过滤 → 评分 → 选择。无可行候选返回 None。"""

    feasible, excluded = hard_filter(registry, requirements)
    if not feasible:
        return RouteDecision(
            decision_id=new_id(),
            run_id=run_id,
            user_id=user_id,
            project_id=project_id,
            task_level=requirements.task_level,
            selected="",
            candidate_scores={},
            excluded=excluded,
            reason_codes=[RouteReasonCode.NO_CANDIDATE],
            actual_model=actual_model,
            shadow=shadow,
        )
    scores = score_candidates(feasible, requirements)
    best = max(scores.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return RouteDecision(
        decision_id=new_id(),
        run_id=run_id,
        user_id=user_id,
        project_id=project_id,
        task_level=requirements.task_level,
        selected=best,
        candidate_scores=scores,
        excluded=excluded,
        reason_codes=[RouteReasonCode.SELECTED_BY_SCORE],
        actual_model=actual_model,
        shadow=shadow,
    )
