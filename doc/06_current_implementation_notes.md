# 当前版本实现记录

## 版本说明

当前版本是 Klonet 专用教学协作 Agent 的第一版落地实现。

它完成了从 `agent_v7` 到 `klonet_agent` 的迁移，并建立了 Mentor/Coding 双 Profile 的基础架构。

## 已实现内容

### 1. Git 初始化

当前项目已经初始化为 Git 仓库。

初始提交：

```text
987ba87 Initial Klonet agent implementation
```

当前分支：

```text
main
```

### 2. 包名迁移

已将主要代码中的 `agent_v7` 引用迁移成 `klonet_agent`。

运行方式改为：

```bash
cd C:\Users\LHT\OneDrive\课设\agent开发\klonet_agent
python -m klonet_agent.agent --mode mentor --user-id default --project-id default
python -m klonet_agent.agent --mode coding --user-id default --project-id demo
python agent.py --help
```

### 3. Agent Profile

新增目录：

```text
agents/
```

关键文件：

- `agents/profile.py`
- `agents/mentor.py`
- `agents/coding.py`

Profile 决定：

- Agent 名称。
- 模式 prompt。
- 可用工具集合。
- 默认工作流。
- 是否需要 RAG。
- 是否需要 Review。

### 4. Prompt 分层

`prompts.py` 已拆分为：

- `CORE_SYSTEM_PROMPT`
- `SAFETY_PROMPT`
- `MENTOR_PROMPT`
- `CODING_PROMPT`
- `STYLE_PROMPT`
- `TASK_PROMPT`

### 5. Session 隔离

`AgentSession` 当前包含：

- `user_id`
- `project_id`
- `mode`
- `history`
- `token_total`
- `loaded_skills`
- `todos`
- `workspace_path`
- `journal_path`

原来的全局 `TODOS` 已经迁移到 session 内。

### 6. Project Journal

新增项目日志模块：

```text
journal/project_journal.py
journal/templates.py
```

项目日志路径：

```text
journals/{user_id}/{project_id}.md
```

当前支持：

- 创建日志。
- 读取日志。
- 追加事件。
- 更新状态。
- 记录测试结果。
- 记录验收差异。

### 7. Knowledge Base

新增知识库第一版：

```text
knowledge/indexer.py
knowledge/retriever.py
knowledge/rag.py
knowledge/style_guide.md
```

当前能力：

- 扫描本地文本文件。
- 切分 chunk。
- 写入 JSONL 索引。
- 关键词检索。
- 返回适合注入模型上下文的证据文本。

当前生成索引：

```text
knowledge/index.jsonl
```

该文件是生成产物，已加入 `.gitignore`。

### 8. Workspace 与 Sandbox

新增：

```text
workspace/manager.py
workspace/sandbox.py
workspace/git_ops.py
```

当前能力：

- 为用户/项目创建 workspace。
- 限制文件路径不能逃出 workspace。
- 拦截危险命令。
- 限制测试命令白名单。
- 查看 git diff。

### 9. 结构化工具

当前新增工具：

- `search_knowledge`
- `list_files`
- `read_file`
- `write_file`
- `run_tests`
- `show_diff`
- `create_project_journal`
- `read_project_journal`
- `append_journal_event`
- `update_project_status`
- `record_test_result`
- `record_acceptance_gap`

旧的 `run_command` 保留兼容，但已经加入危险命令检查。

### 10. Evals

新增：

```text
evals/mentor_cases.jsonl
evals/coding_cases.jsonl
evals/error_cases.jsonl
```

这些文件用于后续做对比实验。

### 11. Tests

新增：

```text
tests/test_cli_entry.py
tests/test_imports.py
tests/test_journal.py
tests/test_knowledge.py
tests/test_workspace_tools.py
```

当前测试结果：

```bash
python -m pytest -q
# 8 passed
```

## 当前 .gitignore 策略

已忽略：

- `.env`
- Python 缓存
- `.pytest_cache`
- `knowledge/index.jsonl`
- `memory/history.jsonl`
- `memory/20*.md`
- `memory/MEMORY.md`
- `memory/USER.md`
- `journals/`
- `workspaces/`
- `skills/.system/`

原因：

- 这些是运行时状态、隐私文件、缓存或生成产物。
- 不应该进入版本管理。

## 当前还没做的事情

- 真正接入 Klonet 源码仓库。
- 向量数据库。
- SQLite 多用户元数据。
- 独立 ReviewAgent。
- Web/API 服务。
- trace logger。
- token 统计。
- eval runner。
- LangGraph 状态图。

## 下一步建议

优先顺序：

1. 接入真实 Klonet 源码到 workspace。
2. 为 Klonet 补充规范文档和常见报错文档。
3. 扩展 `search_knowledge` 的索引范围。
4. 实现 trace logger。
5. 实现 eval runner，开始做 token/速度/质量对比实验。
