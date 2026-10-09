"""模型能力注册表（计划 §4.5，阶段 5）。

每个模型版本声明：上下文窗口、最大输出、tool calling / 结构化输出 / 视觉
等能力、延迟、价格、速率限制、健康状态、数据驻留（隐私上限）、固定版本。
**不感知 ProviderRouter**——注册表只回答"哪些模型有什么能力"；真正的
调用仍只走 ``LLMClient → ProviderRouter``（计划 §3.3-5 不变量）。

首版声明来自部署配置（与 config.py 里的模型清单一致），并显式标注
``eval_baseline=None``：尚未有冻结 eval 基线的模型在评分里按保守值处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from klonet_agent.runtime.governance.models import (
    GovernanceDomainError,
    enum_text,
)


class RouteReasonCode:
    """路由 reason code（计划 §4.5 验收：任何路由结果都有机器可读原因）。"""

    SELECTED_BY_SCORE = "route.selected_by_score"
    EXCLUDED_CAPABILITY = "route.excluded_capability"
    EXCLUDED_CONTEXT = "route.excluded_context"
    EXCLUDED_COST = "route.excluded_cost"
    EXCLUDED_LATENCY = "route.excluded_latency"
    EXCLUDED_PRIVACY = "route.excluded_privacy"
    NO_CANDIDATE = "route.no_candidate"


@dataclass
class ModelCapability:
    """一个**固定版本**模型的能力声明（计划：固定版本 + eval 基线）。"""

    model_id: str
    version: str
    context_window: int = 32768
    max_output_tokens: int = 8192
    tool_calling: bool = True
    parallel_tool_calls: bool = False
    structured_output: bool = False
    vision: bool = False
    latency_ms_p50: int = 3000
    cost_per_1k_input: float = 0.0
    rate_limit_rpm: int = 60
    # 健康度 0~1：由运维观测更新；未知时保守给 0.5。
    health: float = 0.5
    # 数据驻留上限：该模型最高可承接的隐私级别（public/internal/sensitive）。
    # secret 永远不进模型（隐私网关拒绝）。
    max_privacy_class: str = "internal"
    # 适用任务等级（cheap/standard/critical 的子集）。
    task_levels: frozenset[str] = field(default_factory=lambda: frozenset({"standard"}))
    # 冻结的 eval 基线（EvalReport id）；没有基线时质量分按保守值。
    eval_baseline: str | None = None

    def __post_init__(self) -> None:
        if not self.model_id or not self.version:
            raise GovernanceDomainError("模型能力声明必须带固定 model_id 与 version")
        if not 0.0 <= float(self.health) <= 1.0:
            raise GovernanceDomainError("health 必须在 [0, 1]")
        if self.max_privacy_class not in {"public", "internal", "sensitive"}:
            raise GovernanceDomainError(
                "max_privacy_class 只允许 public/internal/sensitive（secret 不进模型）"
            )

    def supports_privacy(self, max_needed: str) -> bool:
        order = {"public": 0, "internal": 1, "sensitive": 2}
        return order.get(self.max_privacy_class, 0) >= order.get(max_needed, 1)


@dataclass
class TaskRequirements:
    """任务侧的能力要求（硬约束来源）。"""

    task_level: str = "standard"          # cheap / standard / critical
    needs_tool_calling: bool = False
    needs_structured_output: bool = False
    needs_vision: bool = False
    min_context_tokens: int = 0
    max_cost_per_1k_input: float | None = None
    max_latency_ms_p50: int | None = None
    # 本任务数据可达到的最高隐私级别（超出该级别的模型被硬过滤）。
    privacy_max_class: str = "internal"

    def __post_init__(self) -> None:
        if self.task_level not in {"cheap", "standard", "critical"}:
            raise GovernanceDomainError(
                f"task_level 非法: {self.task_level}（cheap/standard/critical）"
            )


class ModelCapabilityRegistry:
    """能力注册表。新增供应商只需注册能力，不侵入 Agent 主循环。"""

    def __init__(self, capabilities: list[ModelCapability] | None = None):
        self._models: dict[tuple[str, str], ModelCapability] = {}
        for item in capabilities or []:
            self.register(item)

    def register(self, capability: ModelCapability) -> None:
        key = (capability.model_id, capability.version)
        if key in self._models:
            raise GovernanceDomainError(f"模型版本重复注册: {key}")
        self._models[key] = capability

    def get(self, model_id: str, version: str | None = None) -> ModelCapability | None:
        if version is not None:
            return self._models.get((model_id, version))
        # 未指定版本时取该 model_id 最新注册的一条（注册序即版本序的约定）。
        matches = [c for (mid, _), c in self._models.items() if mid == model_id]
        return matches[-1] if matches else None

    def all(self) -> list[ModelCapability]:
        return list(self._models.values())

    def by_task_level(self, level: str) -> list[ModelCapability]:
        return [c for c in self.all() if level in c.task_levels]


def default_registry() -> ModelCapabilityRegistry:
    """从部署配置（config.py 的模型清单）构造默认注册表。

    这里声明的是**当前部署实际在用的固定版本**；价格/延迟/健康度是保守
    初始值，应由线上观测与 eval 回填。加新供应商时改这里，不改主循环。
    """

    from klonet_agent.config import CHAT_LLM_MODEL, PARATERA_MODEL

    declared = [
        ModelCapability(
            model_id=CHAT_LLM_MODEL,
            version="deployed",
            context_window=1_000_000,
            max_output_tokens=65536,
            tool_calling=True,
            parallel_tool_calls=False,
            structured_output=True,
            vision=True,
            latency_ms_p50=3000,
            cost_per_1k_input=0.0,
            rate_limit_rpm=60,
            health=0.7,
            max_privacy_class="sensitive",
            task_levels=frozenset({"standard", "critical", "cheap"}),
        ),
        ModelCapability(
            model_id=PARATERA_MODEL,
            version="deployed",
            context_window=131072,
            max_output_tokens=16384,
            tool_calling=True,
            parallel_tool_calls=False,
            structured_output=True,
            vision=False,
            latency_ms_p50=6000,
            cost_per_1k_input=0.0,
            rate_limit_rpm=60,
            health=0.6,
            max_privacy_class="sensitive",
            task_levels=frozenset({"standard", "critical", "cheap"}),
        ),
    ]
    registry = ModelCapabilityRegistry([])
    seen: set[tuple[str, str]] = set()
    for item in declared:
        key = (item.model_id, item.version)
        if key in seen:
            continue  # 部署里两个入口指向同一模型时按一条声明处理。
        seen.add(key)
        registry.register(item)
    return registry
