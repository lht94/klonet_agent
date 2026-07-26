# Ops-Privilege 自适应 PEV 设计

## 目标

把原来由模型直接调用任意 Shell 的 `ops-privilege` 升级为可控、可恢复、可审计的
高权限工作流，同时保留它动态生成命令、支持交互式 sudo 的通用 Agent 能力。

普通 `ops` 的 Action Registry、OperationPlan、helper 和 sudoers 契约保持不变。
`ops-privilege` 使用独立状态机，不伪装成拥有 OS 级沙箱。

## 架构

```text
用户目标
  -> Ops Supervisor / 路由
  -> Planner（独立系统提示词、无工具）
  -> 确定性风险门控
  -> 人类确认（按风险）
  -> Executor（非模型可见）
  -> Checker Registry
  -> Verifier（独立系统提示词、无工具）
  -> 完成 / 阻塞 / 重规划
```

Planner 与 Verifier 可以使用同一个底层模型，但不共享系统提示词、工具权限和本次
子任务上下文。主 Agent 的模型可见工具中不包含任意 Shell，也不包含普通 Ops 的
计划执行工具。

## 风险与授权

- 只读步骤：无需确认，不持久化计划。
- 单个可逆低/中风险步骤：形成 MicroPlan，进入高权限模式即视为本步骤授权。
- 多步骤低/中风险计划：执行前要求一次 `confirm-priv <plan_id>`。
- 高风险步骤：先确认计划，再用
  `confirm-priv-step <plan_id> <step_id>` 精确确认步骤。
- 根目录递归删除、磁盘格式化、fork bomb、内联 sudo 密码、明显数据外传和无边界
  删除由确定性规则硬拒绝，模型不能覆盖。

授权绑定计划内容哈希。命令、cwd、风险、前后置条件或回滚内容发生变化后，旧授权
立即失效。重规划生成的新步骤必须重新确认。

## 执行与验证

Executor 只接受 `PrivilegedStep`，负责一次执行、实时转发输出和有界捕获，不规划、
不审批、不重试、不判定目标是否完成。stdin 继承当前终端，因此 sudo 可以由用户
现场输入密码。

Checker Registry 首批支持：

- `exit_code_zero`
- `file_exists`
- `file_contains`
- `service_active`
- `process_running`
- `process_cwd_matches`
- `port_listening`
- `screen_session_exists`
- `container_running`
- `command_available`
- `python_import_succeeds`
- `package_version`
- `nginx_config_valid`
- `log_has_no_fatal_error`

已知命令族会自动补充后置检查。未知修改至少检查退出码，但验证级别标记为
`partial`。退出码非零、超时、执行结果未知、必需 Checker 失败或不可用时，
Verifier 不得给出 `passed`。

## 持久化与恢复

计划保存在：

```text
memory/sessions/{user_id}/{project_id}/privileged_ops_plans/{plan_id}.json
```

每个状态迁移都原子落盘。进程重启后，`running`/`verifying` 步骤变成
`execution_unknown`。`resume-priv` 只运行与退出码无关的当前状态检查；证据充分时
可以把原步骤标记完成，证据不足时保持阻塞，绝不自动重放原命令。

## 审计事件

- `privileged_plan_created`
- `privileged_plan_approved`
- `privileged_step_started`
- `privileged_step_finished`
- `privileged_verification`
- `privileged_plan_blocked`
- `privileged_plan_completed`

事件记录 plan、step、风险、状态、返回码和验证结论；完整有界输出保存在计划证据中。

## 简历表述

项目名称：

> Klonet 智能运维 Agent｜多 Agent 编排与高权限执行治理

一句话：

> 设计并实现面向真实服务器运维的自适应 Plan–Execute–Verify 多 Agent 工作流，
> 通过独立 Planner/Verifier、确定性高权限 Executor、风险分级审批和执行后证据校验，
> 提升复杂运维任务的可控性、可恢复性与可审计性。

要点：

- 设计自适应 PEV 编排：简单可逆任务走 MicroPlan，复杂任务持久化为多步骤计划，
  Planner、Executor、Verifier 通过结构化契约解耦。
- 建立权限边界与结果边界：主 Agent 无法直接执行任意 Shell，通过风险分级审批、
  计划哈希授权和 Checker Registry 校验服务、进程、端口、文件、容器及日志状态。
- 实现检查点恢复与审计：持久化计划状态、授权、输出和验证证据，对超时或中断步骤
  禁止自动重放，并支持基于当前状态证据恢复。

关键词：Python、LLM Agent、Multi-Agent Orchestration、PEV、Privilege Boundary、
Human-in-the-loop、State Machine、Command Risk Classification、
Evidence-based Verification、Failure Recovery。
