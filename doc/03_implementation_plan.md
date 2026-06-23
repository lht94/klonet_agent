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

目标：让项目从“本地可运行框架”升级为“能基于真实 Klonet 资料回答、定位源码、记录项目过程、辅助验收”的教学协作工具。

阶段 2 的优先级是：

1. 先接入真实 Klonet 知识源。
2. 再增强检索质量和证据引用。
3. 再把项目日志、任务、用户项目元数据结构化。
4. 最后用 eval 和 trace 验证效果。

### 1. 固定第二阶段验收标准

要做：

- 明确第二阶段不只看 `pytest`，还要看真实问答和真实开发任务表现。
- 增加第二阶段计划和验收说明文档。
- 在 README 中标明当前进入“真实 Klonet 知识增强”阶段。

验收：

- Mentor 能回答 Klonet 架构、启动、部署、常见报错、核心流程问题。
- 回答必须说明证据来源，不能凭空编。
- Coding 能根据 Klonet 风格指南和源码上下文完成小改动。
- 项目日志能沉淀为后续可检索经验。
- eval case 能对比“无 RAG / 有 RAG / 有 RAG + 日志”的效果差异。

### 2. 建立 Klonet 知识目录结构

要做：

- 按四层结构整理知识库：

```text
knowledge/
  klonet/                    # 人工知识层
  klonet_index/              # 机器索引层
  extracted_docs/            # 原始文档抽取文本
  klonet_experience/         # 运行经验层
  raw_manifest.jsonl         # 原始证据清单
```

- 第一批先整理最关键的人工知识文档：

```text
knowledge/klonet/
  00_project_overview.md
  01_architecture.md
  ops/environment_setup.md
  ops/startup_shutdown.md
  dev/backend_api_development.md
  dev/git_workflow.md
```

实现思路：

- Markdown 负责解释和教学，给人和 Agent 共同阅读。
- JSONL/SQLite 负责定位和索引。
- 源码和原始文档负责最终证据。
- 运行经验层沉淀真实问答、排错、开发和验收案例。

验收：

- 每篇人工知识文档都有“适用场景、核心结论、关键流程、关键文件、常见问题、证据来源”。
- 公共知识中不出现密码、token、真实账号等敏感内容。

### 3. 接入真实 Klonet 源码和文档

要做：

- 增加轻量知识源配置，记录 Klonet 源码、文档、运维资料所在路径。
- 将真实 Klonet 源码以只读参考资料接入 workspace 或知识源目录，供检索和源码定位使用。
- 扫描 `.md`、`.txt`、`.py`、`.js`、`.vue` 等文本文件。
- 对 `.docx`、`.pdf` 先做可选抽取：依赖存在则抽取，不存在则在 manifest 中标记跳过。
- 生成 `knowledge/raw_manifest.jsonl`。

manifest 建议字段：

```text
source_path
type
category
derived_text
sensitivity
updated_at
```

验收：

- 原始 Klonet 资料不直接提交进当前仓库。
- 可提交的是整理后的公开知识文档、索引脚本和脱敏后的样例。
- 私有路径走本地配置，避免污染 Git。
- 本地原始资料目录，例如 `klonet_knowledge/`，不应被 pytest 收集，也不应直接进入 Git。

### 4. 升级检索：从关键词 JSONL 到 BM25 优先的混合检索

要做：

- 保留当前 JSONL 索引格式，增加来源和分类字段。
- 新增 BM25 检索器，先不强制引入复杂向量数据库。
- 预留 HybridRetriever 接口，后续再接向量检索。

索引字段建议：

```text
source
category
title
chunk_id
tags
updated_at
content
```

检索流程：

```text
用户问题 -> 查询归一化 -> BM25 候选 -> 分类/标题/tag 加权 -> top-k 证据片段
```

验收：

- `search_knowledge("拓扑部署进度条卡住")` 能命中拓扑部署、任务队列、运维排错相关文档。
- `search_knowledge("代码风格指南")` 必须稳定命中风格指南。
- 没有证据时返回“未检索到可靠证据”，而不是硬答。

### 5. 让 Mentor 回答严格基于证据

要做：

- Klonet 相关问题先检索知识库。
- 有项目上下文时读取项目日志摘要。
- 回答中区分“根据证据”和“推断”。
- 检索不足时明确说明缺少哪些资料。

回答结构建议：

```text
结论
证据
排查步骤 / 学习建议
不确定点
```

验收：

- 不偏题。
- 不把安全模块回答成阶段验收。
- 每个关键结论有来源，或者明确说明不确定。

### 6. 强化 Coding Agent 的开发闭环

要做：

- 固定 Coding 流程：

```text
理解任务 -> 检索知识/风格 -> 读取相关文件 -> 制定计划 -> 修改 -> 测试 -> diff -> journal -> review
```

- 加强工具调用约束：
  - 用户明确要求“先检索”时，必须先调用 `search_knowledge`。
  - 用户要求“报告原始工具返回”时，总结前必须保留原始返回摘要。
  - 修改代码后必须运行测试和 `show_diff`，除非用户明确跳过。
- 项目日志写入更结构化：
  - 本次任务目标。
  - 修改文件。
  - 测试结果。
  - diff 摘要。
  - 验收差异。

验收：

- Coding 模式完成小任务后，journal 中能看到完整闭环。
- trace 中能看到工具顺序符合预期。
- 用户要求的关键步骤不能被模型跳过。

### 7. 引入 SQLite 管理用户、项目、任务和日志元数据

要做：

- 增加轻量 SQLite 存储。
- Markdown journal 继续做人类可读项目记录。
- SQLite 负责列表、筛选、统计和后续 Web/API 扩展。

建议数据库：

```text
data/klonet_agent.db
```

建议表：

```text
users
projects
tasks
journal_events
knowledge_sources
retrieval_logs
```

验收：

- 能按 `user_id + project_id` 查项目。
- 能查某项目最近任务、测试结果、验收差异。
- 日志工具写 Markdown 的同时同步一份元数据到 SQLite。

### 8. 增强 trace、token 统计和上下文压缩

要做：

- 保留当前 trace logger，并补充第二阶段需要的统计维度。
- 记录每轮对话的检索次数、工具调用次数、输入 token、输出 token、工具结果摘要长度和总耗时。
- 限制 RAG top-k、工具返回长度和项目日志注入长度。
- 对长 journal、长 trace、长工具输出优先注入摘要，而不是全文。
- 为 eval runner 输出 token/速度/质量对比数据。

验收：

- 能看到一次任务中 token 主要消耗在哪里。
- 小型 Coding 闭环不再因为 journal、trace 或工具结果注入导致上下文明显膨胀。
- eval 结果能对比“无 RAG / 有 RAG / 有 RAG + 日志”的 token、耗时和工具调用次数。

### 9. 让项目日志进入知识库

要做：

- 新增 `knowledge/klonet_experience/`。
- 增加日志提炼脚本或工具。
- 从 journal 中提取已完成任务、报错、解决方案、验收差异。
- 生成可维护的经验 case 文档，再进入知识索引。

case 模板：

```text
# 问题标题

## 现象
## 排查路径
## 根因
## 解决方案
## 相关源码
## 可复用结论
```

验收：

- 后来的同学问相似问题时，能检索到历史解决案例。
- 运行经验层不包含私人记忆、临时闲聊或敏感信息。

### 10. 扩展 eval 与 trace，对比第二阶段效果

要做：

- 扩展 `evals/mentor_cases.jsonl`，覆盖 Klonet 架构、部署、报错、验收问题。
- 扩展 `evals/coding_cases.jsonl`，覆盖小代码修改、测试、diff、journal 闭环。
- 扩展 `evals/error_cases.jsonl`，覆盖路径逃逸、危险命令、无证据回答。
- eval runner 输出检索、证据、工具顺序、journal、token、耗时和工具调用次数。

验收：

- 能证明第二阶段比第一阶段更懂 Klonet。
- 能为答辩或项目汇报提供实验表格。

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
