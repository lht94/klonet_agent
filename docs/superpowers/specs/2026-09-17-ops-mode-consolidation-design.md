# Ops 模式收敛设计

## 目标

对外只保留一个 `ops` CLI 模式，并让它使用当前 Ops-Privilege 的 Supervisor、计划状态机、风险门控、受控执行与验收链路。

## 范围

- `--mode ops` 解析为高权限 Supervisor Profile。
- 旧普通 Ops Profile、OperationPlan 工具和旧执行路径不再从该 Profile 暴露或调用。
- `--mode ops-privilege` 不再是 CLI 可选值，也不保留兼容别名。
- 内部 `ops/privileged` 包名与 `OPS_PRIVILEGE_*` 配置变量保持不变，避免改变已验证的安全执行边界。
- 文档、评测和测试改用 `ops` 作为唯一外部模式名。

## 架构

`agent.py --mode ops` -> `get_profile("ops")` -> Ops Supervisor -> intent 分类 -> 只读证据或变更计划 -> 风险审批 -> 受控执行 -> Verifier。

旧 `OperationPlanStore` 与相关 Action Registry 文件仍留在仓库，标注为 legacy；它们不再被 `ops` Profile 的工具集或 Orchestrator 路由使用。这使历史实现可审计、可按需迁移，而不会让用户在运行时选择两条行为不同的运维路径。

## 兼容性与失败处理

旧命令 `--mode ops-privilege` 将由 argparse 拒绝，并提示可用模式；不做隐式降级，以免调用方误以为仍在旧模式。已持久化的 legacy OperationPlan 不自动迁移、不自动执行。现有会话内模式值若为旧名称，只在内部读取历史数据时保留其字符串，不作为可创建的新入口。

## 验收

- `get_profile("ops")` 返回 Supervisor 工作流和不含 legacy OperationPlan 工具的工具集合。
- Orchestrator 在 `ops` 模式初始化并调用 Supervisor。
- CLI 接受 `ops`，拒绝 `ops-privilege`。
- 用户文档与真实能力评测入口使用 `ops`。
- 旧 Ops 实现仍可导入，未被删除。
