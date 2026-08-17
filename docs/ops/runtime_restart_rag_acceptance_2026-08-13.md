# Ops-Privilege 重启与 RAG 闭环验收报告

日期：2026-08-13

## 结论

- RAG 已成为 Discovery 的正式证据源，并以同一 evidence ID 提供给后续 Planner、Binding、Verifier 和回答阶段。
- `v4_e2e`、`v4e2e` 和项目目录名能解析为唯一实例 `/home/lzl/klonet_v4_e2e`。
- “重启平台”按运行清单处理所有受管应用组件：已运行组件重启，缺失组件启动。
- 计划在 RAG 和完整运行清单足够时不再无关地查询 Screen、Git 和日志。
- 规划、Binding、执行或验证失败都会产生持久化 `FailureRecord`，展示准确原因、环境是否改变和可选恢复方案。用户选择后刷新动态证据并生成新计划，不执行旧计划。

## 真实 Agent 验收输入

```text
帮我重启v4_e2e平台
```

本次只生成待审批计划，没有发送确认命令，因此没有改动服务器状态。

## 真实 Agent 回答

```text
Discovery：正在检索 Klonet 知识库…
Discovery：关键证据：Klonet 知识检索完成（reliable；startup_shutdown.md）。
Discovery：正在收集只读证据：privilege_capabilities
Discovery：关键证据：本轮只读补证完成。
Discovery：正在收集只读证据：running_platforms
Discovery：关键证据：本轮只读补证完成。

工作流协调器：变更计划 priv-ops-7d5edfaff5
目标：帮我重启v4_e2e平台
风险：medium
关键资源：
- 项目根目录: /home/lzl/klonet_v4_e2e
- 实例标识: v4e2e
- Master 端口: 47001
- Web Terminal 端口: 47003
- Worker 端口: 47002
变更步骤：
- restart-backend-roles: 重启 v4e2e 的应用组件 — 按项目根目录 /home/lzl/klonet_v4_e2e 重启 master 和 celery 和 web_terminal 和 worker
  预期：按要求重启 master 角色（端口 47001），并确认后端健康; 按要求重启 celery 角色，并确认进程就绪; 启动缺失的 web_terminal 角色（端口 47003），并确认监听就绪; 按要求重启 worker 角色（端口 47002），并确认后端健康
  验收：process_running, port_listening, backend_health, backend_health
  执行方式：准备项目根目录入口文件（6 个；覆盖：无）
  执行方式：重启交互式 Screen 组件 master（v4e2e_m）
  执行方式：重启交互式 Screen 组件 celery（v4e2e_c）
  执行方式：启动交互式 Screen 组件 web_terminal（v4e2e_web）
  执行方式：重启交互式 Screen 组件 worker（v4e2e_w）
精确计划哈希：e3361b27a4fae54b17211ebd4d99dbe45c27f9ce1ce6d62fe7dba2c8549d6d75
查看完整绑定：show-priv-plan-details priv-ops-7d5edfaff5
请使用以下命令确认这份精确计划：
confirm-priv-plan priv-ops-7d5edfaff5 e3361b27a4fae54b17211ebd4d99dbe45c27f9ce1ce6d62fe7dba2c8549d6d75
```

## RAG 证据验收

- 状态：`reliable`
- 置信度：`0.9513`
- 首条命中：`knowledge/klonet/ops/startup_shutdown.md`
- 命中章节：“Klonet 启动、停止与重启 / 第三步：进入正确运行目录”
- 对后续计划的实际指导：
  - `source_root=<project_root>/mains`
  - 复制目标和 `startup_cwd` 都是 `<project_root>`
  - 标准受管应用组件为 Master、Celery、Web Terminal、Worker
  - 可通过 `.klonet/runtime_components.json` 声明未来的受管应用组件
- 持久化计划的语义步骤同时引用 `running_platforms` 和 `klonet_knowledge` 两条 evidence ID。

## 自动化验收

- 重启、RAG、Binding、FailureRecord 定向回归：234 项通过（228 + 6）。
- 完整回归：1041 项通过，2 项失败。
- 两项失败均为既有环境问题：`shell_ast_parser_unavailable`，位于 `tests/test_privileged_execution.py`，与本轮功能无关，未放宽 Shell 安全策略。
- `git diff --check` 通过。
- `python3.8 -m compileall -q ops/privileged` 通过。
- 生产代码中未发现 `v4e2e`、`v4_e2e`、`privileged_v4` 或 `DiagnosticDecision` 等特例/旧架构命名。

