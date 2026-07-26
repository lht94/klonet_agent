# Ops-Privilege Supervisor 单入口回归

运行日期：2026-07-27

本轮将旧的关键词路由替换为：

`Supervisor → 精确 Plan Control → Goal Safety Guard → 模型 Intent Classifier`

确定性回归共 60 条：

- 46 条通过；
- 14 条失败均来自本轮未调整的既有命令风险分级；
- 非控制、未被 Goal Safety Guard 硬拒绝的请求统一记录为 `model_required`，
  不再离线使用关键词猜测 conversation、readonly 或 mutation；
- 精确控制命令与原始目标硬拒绝仍由确定性规则直接评估。

机器可读结果见
`results/supervisor_deterministic_results.jsonl`。

注意：该结果只证明确定性入口与既有 Risk Engine 的表现。模型意图分类质量需要
运行 live eval 后单独统计，不能用这份 deterministic 结果代替。
