# Klonet Agent 版本演进记录

## 记录规则

本文专门记录每次 Git 版本分析。`doc/07_klonet_agent_knowledge_notes.md` 用来沉淀可以深入讨论的架构专题，本文用来记录提交顺序、改动内容和设计原理。

以后每次分析新的提交时，继续追加到本文。

当前已经分析到：

```text
987ba87 Initial Klonet agent implementation
1afdb63 Add project planning docs
6e1b289 Move project proposal doc into doc folder
23d0689 fix: make local cli entrypoints testable
b495a63 refactor: align prompts with klonet teaching agent
e3c04e9 fix: exclude runtime memory from knowledge index
d177938 test: cover session isolation
fcb56d9 docs: add tool constraint knowledge notes
e9ffc6a feat: add trace and eval harness
a3509a4 docs: record phase one harness progress
43a6436 feat: complete phase one context controls
a76bdea docs: record phase one completion
6bed5ea fix: harden cli tests and workspace diff
```

## 1. `987ba87`：Initial Klonet agent implementation

这一版是从原始 `agent_v7` 到 `klonet_agent` 的主体迁移。

主要改动：

- 建立 `agents/`、`app/`、`llm/`、`tools/`、`memory/`、`journal/`、`knowledge/`、`workspace/` 等分层目录。
- 新增 Mentor/Coding 双 Profile 的基础结构。
- 新增项目日志 `ProjectJournal`。
- 新增本地知识库索引、检索和 RAG 包装。
- 新增 workspace 沙箱、结构化文件工具、测试工具和 diff 工具。
- 新增 evals 和基础测试。

设计原理：

原始版本更像单体个人 Agent。该版本开始把 Agent 拆成多个职责边界清晰的模块：主流程编排、模型调用、工具执行、记忆、项目日志、知识库、workspace 安全边界分别放到独立模块中。这样可以让 Agent 从“能聊天、能调用工具”的 demo，升级为可以长期维护、可以解释项目、可以记录教学过程的 Klonet 专用协作系统。

## 2. `1afdb63`：Add project planning docs

这一版新增了 `doc/00` 到 `doc/06` 的项目规划文档。

主要改动：

- 新增项目总览。
- 新增需求说明。
- 新增架构设计。
- 新增实现计划。
- 新增差异化与实验设计。
- 新增 prompt/context/harness 设计。
- 新增当前实现记录。

设计原理：

这不是运行时代码改动，而是把项目从“代码原型”推进到“课程项目/工程项目”。文档把目标、架构、计划、实验和当前状态固定下来，方便后续开发、答辩、复盘和验收。

## 3. `6e1b289`：Move project proposal doc into doc folder

这一版把 `Klonet_Coding_Agent.docx` 移动到 `doc/` 目录。

设计原理：

这是项目资料归档。把提案文档和 Markdown 设计文档放在同一目录，能够让 `doc/` 成为统一的项目说明入口，也方便后续知识库索引时把项目资料作为上下文来源。

## 4. `23d0689`：fix: make local cli entrypoints testable

这一版修复了本地启动方式和测试方式。

主要改动：

- `agent.py` 增加 `sys.path` 处理。
- 新增 `klonet_agent/__init__.py` 兼容包。
- 新增 `tests/test_cli_entry.py`。
- 新增 `tests/helpers.py`。
- 调整多处测试，让它们能在仓库根目录稳定运行。

设计原理：

Python 包有一个常见问题：当仓库目录本身就是包目录时，站在包目录内部运行 `python -m klonet_agent.agent`，导入路径可能找不到父目录。该版本通过兼容包和 `sys.path` 处理，让 `python -m klonet_agent.agent --help` 和 `python agent.py --help` 都可测试。

## 5. `b495a63`：refactor: align prompts with klonet teaching agent

这一版把旧个人 Agent 口吻切换成 Klonet 教学协作 Agent 的正式表达。

主要改动：

- 清理运行文案中的旧称呼。
- 清理记忆 prompt 中的旧个人 Agent 设定。
- 新增 `tests/test_prompt_style.py`，防止旧称呼重新出现。
- 修复 `AgentSession.update_todos()` 的缩进问题。
- 新增 session todo 更新测试。

设计原理：

这版做了两件重要的事。第一是产品定位统一：Klonet 项目需要教学、协作、验收和项目管理，因此运行文案要切换成更正式的 Klonet Agent 表达。第二是修复会话任务更新链路：`ToolExecutor` 会调用 `self.session.update_todos(...)`，所以该方法必须真实属于 `AgentSession`。

## 6. `e3c04e9`：fix: exclude runtime memory from knowledge index

这一版修复了知识库索引污染问题。

主要改动：

- 在 `knowledge/indexer.py` 中新增运行时记忆文件过滤。
- 跳过 `MEMORY.md`、`USER.md`、`history.jsonl`、`tokens.jsonl` 和按日期生成的情景记忆。
- 保留 `memory/store.py` 这类源码文件进入知识库。
- 新增测试确认运行时记忆不会进入知识索引。

设计原理：

知识库应该沉淀项目公共知识，而不是混入用户私有状态。否则 RAG 检索时可能把旧对话、用户偏好、临时记忆当成项目事实，导致回答污染。

## 7. `d177938`：test: cover session isolation

这一版补强了会话隔离测试。

主要改动：

- 新增 `tests/test_session.py`。
- 验证不同用户和项目会生成不同的 `workspace_path` 和 `journal_path`。
- 验证不同 session 的 todo 列表互不影响。
- 验证非法 todo 状态会回退为 `pending`。
- 验证多个 `in_progress` 会被拒绝，并且失败时不会覆盖原任务列表。
- 在 README 和 `doc/06_current_implementation_notes.md` 中更新测试数量。

设计原理：

`AgentSession` 是多用户隔离的第一层。它用 `user_id` 和 `project_id` 决定：

```text
workspaces/{user_id}/{project_id}
journals/{user_id}/{project_id}.md
```

这保证不同学生、不同项目不会混用同一份 workspace、journal 和 todo。

新增测试里最重要的是“失败不污染原状态”。当模型给出两个 `in_progress` 时，`update_todos()` 会返回错误，并保留旧任务列表。这个设计避免模型一次错误规划就把已有任务状态覆盖掉。

这一版的本质不是新增业务功能，而是把“会话隔离”从设计意图变成可验证行为。

## 8. `fcb56d9`：docs: add tool constraint knowledge notes

这一版把工具约束机制整理为文档。

主要改动：

- 新增 `doc/07_klonet_agent_knowledge_notes.md`。
- 记录 Mentor/Coding 双模式不只是 prompt 差异，而是有工具权限边界。
- 提交了 `__init__.py` 和 `prompts.py` 的权限位变化。

设计原理：

该文档的作用是把关键设计沉淀下来，方便后续回顾和继续追加分析。

工具约束机制是 Klonet Agent 中很重要的一块：Prompt 只能告诉模型“应该怎么做”，但工具白名单、工具 schema 过滤、执行器二次检查和 workspace 沙箱，才是真正决定模型“能不能做”的系统边界。

## 9. `e9ffc6a`：feat: add trace and eval harness

这一版补齐了阶段一后半部分的 harness 能力。

主要改动：

- 新增 `TraceLogger`，用 JSONL 记录工具调用、LLM token 和耗时。
- `AgentOrchestrator` 在调用模型时记录 LLM trace。
- `ToolExecutor` 在执行工具时记录工具名、状态、耗时和结果摘要。
- 新增 `evals/runner.py`，可以读取 `evals/*.jsonl` 并生成 `evals/summary.md`。
- 新增 `tests/test_tracing.py` 和 `tests/test_eval_runner.py`。

设计原理：

阶段一的目标不是马上做复杂评测系统，而是先让本地 CLI 具备可观测性和可评估入口。

trace 解决的是“过程可见”问题：后续可以知道一次任务调用了哪些工具、花了多久、用了多少 token、是否被权限系统拒绝。

eval runner 解决的是“样例可管理”问题：现在至少可以统一读取 mentor、coding、error 三类 case，检查字段完整性，并生成汇总报告。后续接入真实模型执行和自动评分时，可以沿用这个 runner。

## 10. `a3509a4`：docs: record phase one harness progress

这一版把阶段一 harness 进展写入版本演进文档。

主要改动：

- 新增 `doc/08_klonet_agent_version_notes.md`。
- 记录从初始迁移到 trace/eval harness 的阶段性变化。
- 把 `e9ffc6a` 中新增的 trace 和 eval 能力纳入文档。

设计原理：

这类提交不改变运行逻辑，但它很重要：项目已经开始有“版本知识库”。后续每次分析不只停留在口头说明，而是沉淀为可回顾的版本演进记录。

这和 `doc/07_klonet_agent_knowledge_notes.md` 的职责不同：`07` 记录架构专题，`08` 记录提交历史和版本演进。

## 11. `43a6436`：feat: complete phase one context controls

这一版补齐阶段一上下文控制的几个细节。

主要改动：

- `ProjectJournal` 支持生成摘要。
- `read_project_journal` 工具默认返回摘要，传 `max_chars=0` 时读取全文。
- 新增 `knowledge/task_templates.md`，沉淀 Mentor 问答、Coding 开发、报错排查、修复测试失败等常见任务流程。
- `ToolExecutor` 统一截断过长工具结果，避免工具输出直接撑爆上下文。
- 新增项目日志摘要、任务模板检索、工具结果截断测试。

设计原理：

阶段一的上下文策略不能只依赖“少传一点”的口头约束，需要把几个基础机制落到代码中：

- RAG 使用 top-k。
- 项目日志可以摘要化。
- 常见任务有模板可检索。
- 工具结果有统一截断兜底。

这些机制让本地 CLI 原型更接近可持续使用的教学协作 Agent，而不是一次性 demo。

## 12. `a76bdea`：docs: record phase one completion

这一版更新版本演进文档，记录阶段一完成状态。

主要改动：

- 更新 `doc/08_klonet_agent_version_notes.md`。
- 记录阶段一已经完成的关键能力：双 Profile、工具权限、session 隔离、RAG、项目日志、trace、eval、上下文控制。
- 更新阶段性测试结果。

设计原理：

这类文档提交的价值在于明确阶段边界。阶段一不是“所有功能都完成”，而是本地 CLI 原型已经具备可运行、可观测、可测试、可解释的最小闭环。

换句话说，它把项目状态从“功能列表”提升为“阶段验收记录”。

## 13. `6bed5ea`：fix: harden cli tests and workspace diff

这一版修复了两个实际使用中容易暴露的问题：Windows CLI 输出编码，以及 workspace diff 的可用性。

主要改动：

- `app/cli.py` 新增 `configure_console_encoding()`，启动 CLI 时将 stdout/stderr 配置为 UTF-8，避免 Windows GBK 环境遇到特殊字符时崩溃。
- 新增 `pytest.ini`，让 pytest 不递归收集 `.test_tmp`、`workspaces`、`journals`、`memory` 等运行时目录。
- `workspace/git_ops.py` 增强 `show_diff()`：
  - 如果 workspace 不是独立 Git 仓库，降级返回文件摘要。
  - 如果是 Git 仓库，除了 `git diff`，还会读取 `git status --short`。
  - 能显示未跟踪文件，避免新增文件在 diff 中完全不可见。
- 新增 CLI 编码测试、pytest 配置测试、workspace diff 降级测试、未跟踪文件测试。

设计原理：

第一，CLI 编码属于 Windows 真实使用问题。模型输出和项目文案里有中文、特殊符号，如果 stdout/stderr 还是 GBK，命令行可能在打印阶段失败。将输出流配置为 UTF-8，并用 `errors="replace"` 兜底，可以提高本地运行稳定性。

第二，pytest 配置是在隔离测试环境。Agent 会生成运行时目录，如果 pytest 误入这些目录，测试结果会被运行产物污染。`norecursedirs` 明确告诉 pytest 不要收集这些目录。

第三，`show_diff()` 原先只适合标准 Git 仓库里的已跟踪文件改动。真实 workspace 可能还没初始化 Git，也可能刚新建文件但未暂存。新增的降级摘要和未跟踪文件展示，让 Coding Agent 在检查改动时不容易漏掉新文件。

这一版的核心是把“测试能跑”推进到“真实本地环境更稳”。

## 当前验证状态

截至 `6bed5ea` 后，当前测试结果为：

```bash
python -m pytest -q
# 31 passed
```
