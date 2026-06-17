# 落地计划

## 阶段 1：本地 CLI 可用

目标：先让项目从 `agent_v7` 迁移成 `klonet_agent`，并完成最小可运行架构。

### 1. 修复迁移与基础可运行

要做：

- 把 `agent_v7` 引用迁移成 `klonet_agent`。
- 检查 `agent.py`、`app/cli.py`、`orchestrator.py`、各模块 `__init__.py`。
- 增加最小导入测试。
- 支持命令行选择模式。

实现思路：

```bash
python3 -m klonet_agent.agent --mode mentor --user-id default --project-id default
python3 -m klonet_agent.agent --mode coding --user-id default --project-id demo
```

验收：

- 核心模块可 import。
- CLI 能启动。
- pytest 通过。

### 2. 建立 Agent Profile

要做：

- 新增 `agents/profile.py`。
- 定义 `AgentProfile`。
- 新增 Mentor/Coding Profile。
- Orchestrator 根据 profile 过滤工具。

实现思路：

```text
AgentProfile:
  name
  mode_prompt
  allowed_tools
  default_workflow
  requires_rag
  requires_review
```

验收：

- Mentor 模式不能调用写文件和测试工具。
- Coding 模式可以调用 workspace 内开发工具。

### 3. 重构 Prompt

要做：

- 拆分 prompt。
- 删除个人陪伴型系统提示词。
- 加入安全、导师、开发、风格、任务规划规则。

实现思路：

```text
CORE_SYSTEM_PROMPT
SAFETY_PROMPT
MENTOR_PROMPT
CODING_PROMPT
STYLE_PROMPT
TASK_PROMPT
```

验收：

- 不同 profile 注入不同模式规则。
- Prompt 内容适合 Klonet 教学协作场景。

### 4. 会话隔离

要做：

- 完善 `AgentSession`。
- 把全局 todo 迁移到 session 内。
- 保存 `user_id`、`project_id`、`mode`、`workspace_path`、`journal_path`。

验收：

- 不同 session 有不同 todo。
- 不同用户项目对应不同 workspace 和 journal。

### 5. Markdown 项目状态机

要做：

- 实现 `journal/project_journal.py`。
- 实现 `journal/templates.py`。
- 提供项目日志工具。

日志路径：

```text
journals/{user_id}/{project_id}.md
```

工具：

- `create_project_journal`
- `read_project_journal`
- `append_journal_event`
- `update_project_status`
- `record_test_result`
- `record_acceptance_gap`

验收：

- 能创建日志。
- 能更新状态。
- 能追加执行记录、测试结果、验收差异。

### 6. Klonet 知识库第一版

要做：

- 实现 `knowledge/indexer.py`。
- 实现 `knowledge/retriever.py`。
- 实现 `knowledge/rag.py`。
- 新增 `search_knowledge` 工具。

实现思路：

- 第一版使用 JSONL 索引。
- 用关键词打分。
- 后续再替换成 BM25 + 向量检索。

验收：

- 能构建索引。
- 能搜索相关片段。
- Mentor/Coding 都可以调用知识检索。

### 7. 代码规范与风格指南

要做：

- 新增 `knowledge/style_guide.md`。
- 总结当前项目代码和注释风格。

内容：

- 命名规范。
- 模块边界。
- 注释风格。
- 测试方式。
- 禁止过度抽象。

验收：

- Coding Agent 写代码前可以检索该指南。

### 8. Workspace 与安全工具

要做：

- 实现 `workspace/manager.py`。
- 实现 `workspace/sandbox.py`。
- 实现结构化文件工具。
- 实现安全测试工具。

工具：

- `list_files`
- `read_file`
- `write_file`
- `run_tests`
- `show_diff`

验收：

- 文件读写限制在 workspace 内。
- 不能读取 `../outside.txt`。
- 测试命令走白名单。

### 9. Coding Agent 开发闭环

要做：

- 在 prompt 和工具中固定开发流程。
- 自动记录 journal。
- 修改代码后运行测试。
- 查看 diff。
- 做轻量 review。

验收：

- Coding 模式能形成：

```text
plan -> retrieve -> edit -> test -> diff -> journal -> review
```

### 10. Mentor Agent 导师闭环

要做：

- Mentor 回答 Klonet 问题前优先检索。
- 能读取项目日志。
- 能给出学习建议和排查步骤。

验收：

- 回答不是泛泛而谈，而是基于证据。
- 没有证据时明确说明不确定。

### 11. Token 与速度优化

要做：

- RAG top-k。
- 工具结果截断。
- 项目日志摘要化。
- 常见任务模板化。
- 记录 token、耗时、工具调用次数。

验收：

- 能为后续对比实验收集数据。

### 12. Harness 与评估

要做：

- 新增 `evals/mentor_cases.jsonl`。
- 新增 `evals/coding_cases.jsonl`。
- 新增 `evals/error_cases.jsonl`。
- 后续实现 eval runner。

验收：

- 每个 case 包含输入、期望行为、是否改代码、验收标准。

## 阶段 2：多用户和知识库增强

目标：让项目更接近真实教学场景。

要做：

- 引入 SQLite 管理用户、项目、任务、日志元数据。
- 知识库升级为 BM25 + 向量检索。
- Klonet 真正源码仓库接入 workspace。
- 项目日志进入知识库。
- 增加 trace logger。
- 增加 token 统计。

## 阶段 3：生产化和实验验证

目标：能展示完整实验结果和教学管理价值。

要做：

- Web/API 服务。
- 权限系统。
- ReviewAgent 独立化。
- eval runner 自动跑对比实验。
- 生成实验表格。
- 可选引入 LangGraph。

## 当前已经完成的第一版内容

已经完成：

- Git 仓库初始化。
- `agent_v7` 到 `klonet_agent` 迁移。
- Agent Profile。
- 分层 Prompt。
- Session 隔离。
- Project Journal。
- 本地 JSONL 知识库。
- Style Guide。
- Workspace Sandbox。
- 结构化工具。
- eval case 雏形。
- 最小测试。

当前测试结果：

```bash
python3 -m pytest -q
# 5 passed
```
