# Ops-Privilege V4 阶段化工作流设计

## 目标

将 Ops-Privilege 从“所有运维请求都进入 Planner/Binding”的 V3 流程迁移为按意图动态路由的阶段化工作流：只读请求走 `Intent -> Discovery -> Response`，变更请求走 `Intent -> Discovery <-> Planning -> Confirmation -> Execution -> Verification -> Response`。

## 边界

- Discovery 是唯一可运行注册只读 Probe 或确定性只读 argv 的阶段，统一缓存、去重和预算。
- Evidence Synthesis 只处理内存证据，输出带证据引用的事实、不确定项和缺失决策。
- Change Planner 只输出真实主机变化；发现、归并和回答不能成为 ChangeStep。
- Binder 只输出 `registered_action`、`shell_artifact` 或 `blocked`。
- Verification 只使用执行证据、Checker 和补充只读 Probe；不能执行变更或覆盖失败 Checker。
- Response 是固定终点，不进入执行绑定。

## 复用策略

V4 重新实现上层合同与编排，继续复用现有 Intent Classifier、Goal Guard、Probe Registry、Action Registry/Runner、Shell 审核、Executor、Checker 和风险策略。测试期由显式配置选择 V3/V4，单次请求不自动回退。V4 验收后删除 V3 计划数据和上层旧代码。

## 有界循环

- 主 Discovery 最多 3 轮、每轮最多 4 个 Probe、总计最多 10 个不同请求。
- 相同 probe+args 在同一工作流中只执行一次。
- Change Planner 最多两次声明 evidence gap；Binder 失败最多重规划一次。
- 只读预算耗尽后，只要有可靠事实就带不确定性回答。
- `execution_unknown` 只能重新验证，不能自动重放。

## 验收

原失败查询必须不创建计划、不调用 Binder，并在有界 Discovery 后回答。完整变更闭环需让 Agent 自行发现现有 Git remote/branch，将新实例部署到 `/home/lzl/klonet_v4_e2e`，冻结独立资源，经确认后执行并验证，且不修改现有平台。
