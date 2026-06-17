# 架构方案

## 总体设计

项目采用：

```text
共享轻量 Agent Runtime + 两个 Agent Profile
```

不要为 Mentor Agent 和 Coding Agent 做两套完全独立的框架。两者应该共享底层能力，通过 profile 区分：

- 系统提示词
- 可用工具
- 默认工作流
- 是否强制 RAG
- 是否强制 Review

## 架构分层

```text
klonet_agent/
  agent.py              # CLI 启动入口
  orchestrator.py       # 工具循环和主编排
  session.py            # user_id/project_id/mode/todos/workspace/journal
  prompts.py            # 分层 prompt
  config.py             # 路径、模型、默认配置

  agents/               # Mentor/Coding Profile
  llm/                  # 模型调用
  tools/                # 工具声明和执行
  memory/               # 长期记忆、用户画像、历史记录
  journal/              # 项目 Markdown 状态机
  knowledge/            # Klonet RAG 和知识索引
  workspace/            # workspace 管理与沙箱
  subagents/            # 后续 ReviewAgent 等
  tracing/              # 后续 trace
  evals/                # 评估样例
  tests/                # 单元测试
```

## 两个 Agent Profile

### Mentor Profile

用途：

- Klonet 知识问答。
- 源码解释。
- 报错排查。
- 项目进度理解。

默认工作流：

```text
retrieve -> explain -> suggest next step
```

可用工具：

- `search_knowledge`
- `read_project_journal`
- `load_skill`
- `append_episode`
- `write_memory`
- `write_user`
- `web_fetch`
- `update_todos`

默认不开放：

- `write_file`
- `run_tests`
- `show_diff`

这样可以避免 Mentor Agent 在答疑时误修改代码。

### Coding Profile

用途：

- 代码修改。
- 测试验证。
- diff 检查。
- 项目日志记录。
- 轻量 review。

默认工作流：

```text
plan -> retrieve -> edit -> test -> diff -> journal -> review
```

可用工具：

- Mentor 工具
- `list_files`
- `read_file`
- `write_file`
- `run_tests`
- `show_diff`
- `create_project_journal`
- `append_journal_event`
- `update_project_status`
- `record_test_result`
- `record_acceptance_gap`

## Prompt 工程

Prompt 不应该写成一个巨大的字符串，而应该分层。

当前建议分为：

- `CORE_SYSTEM_PROMPT`
- `SAFETY_PROMPT`
- `MENTOR_PROMPT`
- `CODING_PROMPT`
- `STYLE_PROMPT`
- `TASK_PROMPT`

### CORE_SYSTEM_PROMPT

描述 Agent 的核心身份：

- Klonet 专用教学协作 Agent。
- 服务正在学习和维护 Klonet 的同学。
- 目标是理解 Klonet、规范开发 Klonet、沉淀项目过程。

### SAFETY_PROMPT

描述安全边界：

- 不泄露密钥。
- 不越权读写文件。
- 高风险操作需要人工确认。
- 优先使用结构化工具。

### MENTOR_PROMPT

描述导师模式：

- 优先检索知识库。
- 默认不改代码。
- 回答要带来源和下一步建议。

### CODING_PROMPT

描述开发模式：

- 计划、检索、读文件、写代码、测试、diff、日志、review。
- 修改代码后说明影响范围和验证结果。

### STYLE_PROMPT

描述代码风格：

- 中文注释。
- 模块职责清楚。
- 实现直观。
- 不过度抽象。

### TASK_PROMPT

描述 todo 使用规则：

- 多步骤任务先更新 todo。
- 同一时间只允许一个 `in_progress`。
- 完成后更新为 `completed`。

## Context 工程

每轮模型调用不应该把所有东西都塞进去，而应该按优先级注入上下文。

推荐优先级：

1. 安全规则和模式规则。
2. 当前任务目标。
3. 当前用户、项目、workspace、journal。
4. 当前 todo。
5. RAG 检索证据。
6. 最近对话。
7. 项目日志摘要。
8. 长期记忆。
9. 工具说明。

上下文优化策略：

- RAG 只返回 top-k 结果。
- 工具结果过长时截断或落盘。
- 历史对话压缩成情景记忆。
- 项目日志按章节读取，不一定每次全量注入。

## Harness 工程

Harness 指 Agent 的运行外壳和评估系统。

它需要负责：

- 会话状态管理。
- 工具权限控制。
- trace 记录。
- 任务回放。
- eval case 执行。
- token 和耗时统计。
- 测试结果收集。

后续评估时可以比较：

- 通用 Claude Code / Codex。
- 无 RAG 的 Klonet Agent。
- 有 RAG 的 Klonet Agent。
- 有 RAG + 项目日志的 Klonet Agent。

## 是否使用 LangGraph

第一阶段不建议重度使用 LangGraph。

原因：

- 当前目标是理解底层机制和完成毕业设计原型。
- 自研轻量循环更适合学习和展示。
- 现在最缺的是知识库、项目日志、沙箱、prompt/context/harness，而不是图编排框架。

后续可以考虑：

- 长任务恢复。
- human-in-the-loop。
- 多节点 ReviewAgent。
- 可视化状态流。
- 生产化持久化。

使用建议：

```text
阶段 1：自研轻量工具循环
阶段 2：稳定流程后可选迁移 LangGraph
阶段 3：生产化时再引入持久化和人工审批
```
