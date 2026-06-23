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

当前已经清理旧个人 Agent 口吻，运行时提示、记忆提示和工具输出统一使用
Klonet 教学协作 Agent 的表达。

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

当前已经补充会话隔离测试，确认不同用户/项目有独立的 workspace、journal、todo 和工作历史。

记忆路径：

```text
memory/sessions/{user_id}/{project_id}/history.jsonl
memory/sessions/{user_id}/{project_id}/MEMORY.md
memory/users/{user_id}/USER.md
```

初始化上下文最多加载最近 20 条历史，避免旧对话污染和 token 膨胀。

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
- 生成适合注入上下文的项目日志摘要。
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
knowledge/task_templates.md
```

当前能力：

- 扫描本地文本文件。
- 切分 chunk。
- 写入 JSONL 索引。
- 关键词检索。
- 返回适合注入模型上下文的证据文本。
- 跳过 `memory/MEMORY.md`、`memory/USER.md` 和日期情景记忆等运行时状态文件，
  避免把个人历史记忆混入 Klonet 公共知识库。
- 提供常见任务模板，覆盖 Mentor 问答、Coding 开发、报错排查和修复测试失败。

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
- `show_diff` 在无 Git 仓库时返回文件摘要，在 Git 仓库中补充未跟踪文件列表。

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
evals/runner.py
```

这些文件用于后续做对比实验。当前 `EvalRunner` 可以离线读取 jsonl case、
校验最小字段，并生成 `evals/summary.md` 汇总。

### 11. Trace 与 Token 统计

新增：

```text
tracing/logger.py
```

当前支持：

- 记录 LLM 调用 token 和耗时。
- 记录工具调用状态、耗时和结果摘要。
- 统一截断过长工具结果，避免上下文膨胀。
- trace 写入 `tracing/trace.jsonl`，作为后续评估和安全审计依据。

### 11.1 运行时路由与编排保护

当前新增：

- 区分 Klonet 域内、通用技术和混合问题。
- 明确“不需要 Klonet”时不暴露 `search_knowledge`。
- RAG 默认 `top_k=3`，并过滤低分、低关键词覆盖结果。
- Mentor 不暴露 `update_todos`。
- Coding todo 最多自动续跑一次，工具循环最多 8 轮。
- todo 支持 `waiting_user` 和 `blocked`，暂停状态不会自动续跑。
- Prompt 要求保留否定条件，不能把通用问题强行拉回 Klonet。


### 11.2 BM25 分层检索架构

当前新增：

- 使用 `jieba` 做中文领域分词，保留 API 路由、类名、函数名和文件路径等结构化 token。
- 使用 `rank-bm25` 替换简单词频统计，索引未变化时复用内存 BM25。
- `KnowledgeChunk` 增加 `chunk_id/layer/domain/priority/status/quality/sensitivity/last_verified`。
- Markdown 优先按标题切分，超长章节再使用重叠窗口。
- 兼容现有知识文档的复数 `domains` 以及 `current_verified`、`diagnostic_playbook` 状态，并统一转换为检索 metadata。
- 对路由领域名和知识 frontmatter 领域名做显式别名匹配，例如 `runtime` 可匹配 `operations/deployment/environment`。
- 公共索引在构建阶段排除 `review_required/restricted` 内容。
- Query Router 返回 `scope/confidence/task_type/domains/reasons/hard_disable_rag`。
- 只有明确排除 Klonet 时才硬隐藏 RAG；普通 general 分类只做软路由。
- 概念、故障排查、源码定位、开发和项目进度使用不同知识层权重。
- 检索输出分为 `reliable/weak/none`，无可靠证据时允许拒答。
- `search_knowledge` 兼容原有参数，并新增 `task_type/layers/domains/min_priority`。

检索评估：

```text
cases: 11
scope_accuracy: 1.0
task_type_accuracy: 1.0
recall_at_3: 1.0
recall_at_10: 1.0
mrr: 1.0
abstention_accuracy: 1.0
general_rag_false_positive_rate: 0.0
```

评估入口：

```bash
python -m klonet_agent.evals.retrieval_runner
```

### 12. Tests

新增：

```text
tests/test_cli_entry.py
tests/test_eval_runner.py
tests/test_imports.py
tests/test_prompt_style.py
tests/test_pytest_config.py
tests/test_session.py
tests/test_tracing.py
tests/test_journal.py
tests/test_knowledge.py
tests/test_workspace_tools.py
tests/test_knowledge_pipeline.py
tests/test_orchestrator_controls.py
tests/test_retrieval_architecture.py
tests/test_retrieval_eval.py
```

当前测试结果：

```bash
python -m pytest -q
# 54 passed
```

## 当前 .gitignore 策略

已忽略：

- `.env`
- Python 缓存
- `.pytest_cache`
- `workspaces/`
- `journals/`
- `memory/`
- `knowledge/index.jsonl`
- `evals/summary.md`
- `evals/retrieval_summary.md`
- `tracing/trace.jsonl`
- `memory/sessions/`
- `memory/users/`
- `memory/20*.md`
- `memory/MEMORY.md`
- `memory/USER.md`
- `journals/`
- `workspaces/`
- `skills/.system/`

原因：

- 这些是运行时状态、隐私文件、缓存或生成产物。
- 不应该进入版本管理。

同时新增 `pytest.ini`，让根目录测试跳过 `workspaces/`、`journals/`、`memory/`
等运行时目录，避免真实使用生成的测试污染项目自身测试结果。

## 当前还没做的事情

- 真正接入 Klonet 源码仓库。
- 向量数据库。
- SQLite 多用户元数据。
- 独立 ReviewAgent。
- Web/API 服务。
- LangGraph 状态图。

## 下一步建议

优先顺序：

1. 接入真实 Klonet 源码到 workspace。
2. 为 Klonet 补充规范文档和常见报错文档。
3. 扩展 `search_knowledge` 的索引范围。
4. 接入真实 Klonet 任务，开始用 eval runner 做 token/速度/质量对比实验。
