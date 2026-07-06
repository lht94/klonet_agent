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

## 14. 2026-07-01 至 2026-07-06：Ops 受控运维执行体系成型

本次分析范围：

```text
起点前一版：454603b fix: show knowledge sources in ops observations
本次首个提交：fd79c76 feat: add controlled ops operation plan base
本次最后提交：6445ec4 fix: allow controlled ops startup file edits
提交数量：98
分析日期：2026-07-06
```

### 总体结论

这一大段版本的核心不是普通功能堆叠，而是把 Klonet Agent 从“能解释、能检索、能做只读诊断”，推进到“能以受控方式参与服务器运维”的阶段。

核心变化可以概括为五条主线：

```text
1. 新增 Ops OperationPlan：所有会修改服务器环境的动作先变成可审计计划。
2. 新增受控 recipe：模型不能写任意 shell，只能选择白名单 recipe 和结构化参数。
3. 新增服务器 helper：真正高权限动作由 root 安装的 /usr/local/bin/klonet-agent-op 执行。
4. 新增大量只读环境探针：先收集端口、screen、进程、nginx、配置、安装包、服务健康等证据。
5. 新增部署文档和服务安装脚本：支持专用 klonet-agent 账号、sudoers、systemd、SSH 登录和真实执行开关。
```

从 Agent 安全设计看，这一阶段的关键原则是：

```text
LLM 负责理解目标和组织步骤。
Python 状态机负责计划、确认、顺序和阻断。
Recipe runner 负责白名单动作分发。
Helper 脚本负责服务器侧真实执行。
Sudoers 负责高权限边界。
只读工具负责给每一步提供证据。
```

这和之前 Mentor/Coding 的工具约束思想是一致的：不能只靠 prompt 告诉模型“不要乱执行”，而要把“能执行什么、什么时候执行、执行前需要哪些证据、失败后如何恢复”写进代码链路。

### 主体架构变化

#### 1. OperationPlan：把危险操作变成计划对象

新增的 `ops/operations.py` 是这一阶段的核心。它定义了：

```text
OperationPlan
OperationStep
OperationPlanStore
RecipeExecutionResult
```

其中 `OperationPlan` 表示一次运维任务，例如部署、重启、销毁平台；`OperationStep` 表示计划里的一个步骤，例如预检、准备文件、启动服务、验证健康；`OperationPlanStore` 负责把计划保存到 `memory/ops_operation_plans/*.json`，让计划跨轮对话仍然可追踪。

设计原理是：

```text
模型不能直接把“部署平台”翻译成 shell。
它必须先生成一个计划，计划里每一步都有 step_id、risk、status、recipe_id 和 recipe_args。
计划先保存，用户确认后才允许执行。
```

这解决了运维 Agent 最大的问题：LLM 可能在上下文里看起来“懂了”，但实际服务器操作必须可审计、可暂停、可恢复。

#### 2. 状态机：用代码控制执行顺序和恢复

`OperationStep` 的状态包括：

```text
pending
approved
running
completed
failed
blocked
```

`OperationPlan` 的状态包括：

```text
pending
approved
aborted
completed
failed
```

执行时不是模型自己决定跳到哪一步，而是 `execute_next_step()` 找当前第一个未完成步骤，并由 `execute_step()` 检查：

```text
计划是否已确认。
步骤是否需要二次确认。
前置步骤是否完成。
步骤是否已经 blocked、failed 或 running。
是否绑定了 recipe。
recipe_runner 是否可用。
```

如果上次执行停在 `running`，系统会把它转为 `blocked`，要求先检查运行态再恢复。这一点非常重要，因为真实服务器操作可能被中断，不能假设“上次没返回就是没执行”。

#### 3. ControlledRecipeRunner：只执行白名单动作

`ops/recipes.py` 引入 `ControlledRecipeRunner`。它不是通用命令执行器，而是 recipe 分发器。

这一阶段逐步支持的 recipe 包括：

```text
restart_screen_component
stop_screen_component
stop_platform_screens
start_platform_screens
validate_project_files
prepare_project_files
extract_archive
run_install_script
write_ops_file
reload_nginx
manual_checkpoint
```

设计原理是：

```text
模型只提供 recipe_id 和结构化 recipe_args。
代码校验参数、路径、组件名、脚本名、文件类型和风险。
真实执行时只调用固定 helper 子命令。
```

这避免了把 Ops 工具变成“变相 arbitrary shell”。例如运行安装脚本只允许 `base_requ_setup.sh NORMAL` 和 `docker_service.sh`，不会允许模型随便拼一个 `bash xxx.sh`。

#### 4. Server helper：把高权限动作收口到 root 安装脚本

`scripts/klonet-agent-op` 是服务器侧 helper。它支持 dry-run 和 execute 两种模式，并提供固定子命令：

```text
restart-screen-component
stop-screen-component
stop-platform-screens
start-platform-screens
reload-nginx
extract-archive
run-install-script
```

真实执行前会校验：

```text
platform 名是否合法。
screen session 是否属于该 platform。
project_root 是否是安全绝对路径。
入口文件是否存在。
端口是否已被占用。
nginx -t 是否通过。
压缩包成员是否路径逃逸。
安装脚本是否在 allowlist 中。
```

设计原理是：

```text
Agent 进程本身不直接拥有无限 sudo。
需要 root 安装 helper 和 sudoers 白名单。
真实执行时 recipe runner 走 sudo -n /usr/local/bin/klonet-agent-op <固定子命令>。
```

这把权限边界从 LLM prompt 下沉到 Linux sudoers 和 helper 参数校验。

#### 5. 环境探针：先拿证据，再执行计划

`tools/environment.py` 在这一阶段大幅扩展，增加了很多只读工具：

```text
inspect_ops_context
inspect_system_environment
inspect_klonet_runtime
inspect_process_detail
inspect_platform_instances
inspect_platform_health
inspect_service_health
inspect_install_scripts
inspect_archive
inspect_nginx_routes
inspect_frontend_config
render_klonet_config
render_docker_daemon_config
read_klonet_logs
inspect_screen_session
```

它们的共同原则是：

```text
只读。
不修改服务器。
返回结构化证据。
尽量带 resolved_path、mtime、size、status、recommendation。
敏感内容要脱敏。
```

这让 Ops Agent 在生成计划前可以先确认事实，例如：哪个端口被谁占用、screen 是否存在、进程 cwd 是什么、nginx 路由指向哪里、frontend config.js 是否和后端端口匹配。

#### 6. Profile、Registry、Executor、Prompt 联动

这一阶段不是只加了 Python 函数，还把工具接入 Agent 主流程：

```text
agents/profile.py
  给 Ops profile 增加可见工具白名单。

tools/registry.py
  定义每个工具的 schema、参数和说明。

tools/executor.py
  把工具名路由到真实 Python 函数或 OperationPlanStore。

prompts.py
  明确 Ops 模式行为规则：先只读诊断，再计划，再确认，再受控执行。

orchestrator.py
  增加 Ops 路由提示、可见工具进度文案、确认命令处理和中断历史清理。
```

设计原理是：一个工具要真正可靠，不只是“写一个函数”。它还必须有 profile 权限、schema 描述、执行器映射、prompt 行为边界和测试覆盖。

### 分阶段演进

#### 阶段 A：7 月 1 日，建立受控运维计划底座

这一阶段从 `fd79c76` 开始。系统先建立 OperationPlan，然后逐步加入路由、端口诊断、平台实例识别、步骤状态机、recipe runner、helper dry-run 和 sudoers 合同。

关键变化：

```text
从“模型建议你怎么操作”变为“模型创建一个可保存、可确认、可执行的计划”。
从“执行 shell”变为“执行 recipe”。
从“用户口头同意”变为“confirm <plan_id> / confirm-step <plan_id> <step_id>”。
```

#### 阶段 B：7 月 2 日，计划执行链路完善

这一阶段主要把计划执行从“能生成”推进到“能按顺序执行并处理失败”。新增了 start/stop/restart 的 controlled screen recipe，默认 deploy/restart/destroy 会自动绑定 recipe。随后增加了 `execute_ops_next_step`，让模型不要猜 step_id，而是让状态机选择下一步。

同时增加大量防护：

```text
阻止重复 screen。
阻止端口已占用时部署。
启动前要求端口配置。
停止前检查 screen。
失败时报告 helper 错误。
未知环境状态时 blocked。
blocked 步骤必须 resolve 后才能继续。
```

#### 阶段 C：7 月 3 日，计划查询、过滤和部署准备能力增强

这一阶段让 Agent 能管理多个计划：描述、列表、按状态过滤、按操作和目标过滤。随后加入部署前准备能力：检查系统 Python、准备项目入口文件、解压安装包、运行安装脚本、写受控文件、reload nginx、渲染部署配置草案。

关键原理：

```text
部署不是单个命令，而是“资料准备 -> 配置草案 -> 文件写入 -> 服务启动 -> 健康验证”的流程。
```

#### 阶段 D：7 月 4 日，环境诊断和配置草案能力成型

这一阶段主要扩展只读环境工具和配置生成能力。包括解析 nginx 路由、生成后端 config.py、对齐前端 config.js、检查压缩包和命令路径、生成 docker daemon.json 草案、服务健康摘要、安装脚本检查、平台健康验收、前端配置校验。

核心意义是：Agent 不再靠记忆或猜测部署配置，而是先读取实际服务器状态，再生成可审查草案。

#### 阶段 E：7 月 5 日，真实执行部署和专用账号体系

这一阶段把 dry-run 能力推进到真实服务器执行：recipe runner 通过 `sudo -n` 调用 helper，安装脚本支持创建 `klonet-agent` 专用账号、安装 systemd service、配置环境文件、启用真实执行开关、配置 SSH 登录工作流。

同时修复真实运行中暴露的问题：

```text
中断 tool_call history 会污染后续请求。
长安装输出需要 streaming。
中断步骤需要安全恢复。
部署路径应优先使用 dedicated account 目录。
端口诊断要优先精确 PID/cwd。
敏感源文件和密钥要脱敏。
proc cwd 读不到时要标为 unchecked。
路由里的数字不一定是端口。
```

#### 阶段 F：7 月 6 日，源码获取知识与启动文件写入边界补齐

这一阶段把“源码从哪里来”写入知识库 runbook，并让检索能优先命中 `source_acquisition` 相关内容。最后一版 `6445ec4` 放宽受控写文件的边界：Ops 不是普通业务代码开发，但允许通过 `write_ops_file` 修改平台启动必需文件，例如 `gun.py`、`master_main.py`、`worker_main.py`、`web_terminal_main.py`、`celery_worker.py` 等。

设计原理是：

```text
Ops 不应该改业务逻辑。
但部署和启动平台时，入口文件、配置文件、nginx/frontend 配置属于运维启动面。
这类文件可以写，但必须通过 OperationPlan + write_ops_file + dry-run 预览 + 备份 + 敏感路径拒绝。
```

### 逐提交说明

| 提交 | 日期 | 更新内容 | 原理和意义 |
| :--- | :--- | :--- | :--- |
| `fd79c76` | 2026-07-01 | 新增 `ops/operations.py`、`create_ops_operation_plan`、Ops profile 权限和测试。 | 建立 OperationPlan 底座，把部署/重启/销毁先转成可审计计划，不直接执行服务器修改。 |
| `10e6794` | 2026-07-01 | 在优化计划文档中记录 Ops 路由优化方案。 | 先把“运维意图如何路由”写成设计，避免后续工具只靠关键词乱触发。 |
| `2456658` | 2026-07-01 | 在优化计划文档补充 Ops 工具能力路线图。 | 明确 Ops 工具从只读诊断到受控执行的能力边界。 |
| `b046ce4` | 2026-07-01 | 新增 `ops/routing.py`，支持端口、路径、组件、动作抽取，并加入端口 owner 检查。 | 让系统先识别“这是端口诊断/部署/重启/日志排查”，再推荐合适工具。 |
| `52ae90f` | 2026-07-01 | 把 screen 快照标记为历史证据。 | screen 输出只能说明某个时间点的状态，不能当作当前实时事实。 |
| `450b895` | 2026-07-01 | 新增平台实例识别工具，按 screen、进程、config 识别 Klonet 实例。 | 多平台服务器上不能只看一个进程，要聚合 platform、role、cwd、ports。 |
| `e3f93f8` | 2026-07-01 | 为 OperationPlan 加入步骤状态机。 | 用 pending/approved/running/completed/failed/blocked 管住执行顺序和异常恢复。 |
| `c34e74f` | 2026-07-01 | 给 OperationPlan 接入 recipe runner hook。 | 计划步骤只调用注入的受控 recipe runner，不把模型文本当 shell。 |
| `3564327` | 2026-07-01 | 新增 `ops/recipes.py` 和 restart screen dry-run recipe。 | 先用 dry-run 预览重启动作，证明参数合法且不会修改环境。 |
| `757dafe` | 2026-07-01 | 新增 `scripts/klonet-agent-op` helper dry-run 合同。 | 把服务器侧操作做成固定 CLI 子命令，为 sudoers 白名单做准备。 |
| `a164b1b` | 2026-07-01 | helper 增加 guarded execute mode。 | 即使进入 execute，也必须经过参数校验，不能执行任意命令。 |
| `17dac73` | 2026-07-01 | 新增 helper sudoers 安装合同和测试。 | 高权限动作必须由 root 安装的 helper 和 sudoers 白名单承接。 |
| `aa255ff` | 2026-07-01 | recipe runner 可以执行受控 helper。 | Python recipe 层开始能调用服务器 helper，但仍保持白名单动作。 |
| `1b30873` | 2026-07-02 | 用环境变量门控真实执行。 | 默认 dry-run；只有显式配置真实执行开关时才允许修改环境。 |
| `c9314d8` | 2026-07-02 | 扩展 restart plan 步骤。 | 将重启拆成 master、worker、celery、web_terminal、verify 等步骤，便于确认和恢复。 |
| `097c87b` | 2026-07-02 | 新增 controlled platform screen start recipe。 | 启动整个平台时仍走固定 screen 命令模板和参数校验。 |
| `dae3cb0` | 2026-07-02 | 新增 controlled screen stop recipe。 | 停止单个 screen 组件也走白名单 helper，而不是自由 kill。 |
| `75cb7e1` | 2026-07-02 | 新增 stop platform screens recipe。 | 销毁/停止平台时只停止属于目标 platform 的 screen。 |
| `705bc83` | 2026-07-02 | destroy 计划默认绑定 controlled stop recipe。 | 销毁流程不再停留在手工提示，而能进入受控停止动作。 |
| `2cdd067` | 2026-07-02 | plan 输出展示 recipe args。 | 用户能看到计划将用什么参数执行，提高可审计性。 |
| `b8a1056` | 2026-07-02 | deploy 计划默认绑定 start recipe。 | 当有 project_root 时，部署计划能自动进入启动 screen 的受控 recipe。 |
| `c98ff68` | 2026-07-02 | restart 计划默认绑定组件级 recipe。 | 重启每个组件时自动生成 platform、component、screen、project_root 参数。 |
| `36154c9` | 2026-07-02 | 强制 plan step 按顺序执行。 | 避免模型跳过预检直接启动服务。 |
| `33da221` | 2026-07-02 | 新增 manual checkpoint recipe。 | 对只需人工确认/只读证据的步骤，可以有受控的“环境未改动”完成方式。 |
| `d629cc9` | 2026-07-02 | 新增 `execute_ops_next_step` 工具。 | 模型不再猜下一个 step_id，由状态机决定下一步。 |
| `4009184` | 2026-07-02 | plan 输出下一步确认命令。 | 降低用户确认和继续执行的操作成本。 |
| `3bf7483` | 2026-07-02 | prompt 要求优先执行 next-step。 | 把状态机使用方式写入行为规则，避免模型绕过顺序控制。 |
| `6f9c2bf` | 2026-07-02 | Ops 诊断根据路由推荐具体工具。 | 端口问题走 process detail，日志问题走 screen/logs，运行态问题走 runtime/context。 |
| `0903880` | 2026-07-02 | 优化 plan action 输出。 | 让模型和用户更容易理解当前计划状态和下一步动作。 |
| `1b11706` | 2026-07-02 | 部署预检验证项目入口文件。 | 缺少 `gun.py`、`master_main.py` 等启动文件时阻断部署。 |
| `4784bcc` | 2026-07-02 | helper 阻止重复 platform screen。 | 已有 screen 存在时不能重复启动，避免端口和进程冲突。 |
| `8fb4ea4` | 2026-07-02 | helper 强制部署入口文件存在。 | 服务器侧再次校验，防止 Python 层遗漏。 |
| `cbcb206` | 2026-07-02 | helper 校验 restart 入口文件。 | 重启前确认启动命令依赖的文件仍在。 |
| `65efd46` | 2026-07-02 | helper 阻止端口占用时部署。 | 从 config 读取端口并检查监听状态，避免新平台覆盖已有服务。 |
| `a02146a` | 2026-07-02 | 停止服务前验证 screen。 | 不存在目标 screen 时阻断，避免误判停止成功。 |
| `a8cf2ff` | 2026-07-02 | 启动前要求部署端口配置。 | 没有 config 端口就无法判断冲突，因此应阻断。 |
| `dfbce9e` | 2026-07-02 | 重启前验证 screen。 | restart 不是盲目 stop/start，先确认目标组件存在。 |
| `f08bb88` | 2026-07-02 | helper 失败时报告命令失败细节。 | 失败不能只返回异常，要给 returncode、stdout/stderr 和环境是否未知。 |
| `161db0a` | 2026-07-02 | 环境状态未知时 blocked。 | 不确定是否已改动环境时不继续推进，要求重新检查。 |
| `bfd082f` | 2026-07-02 | plan 遇到 blocked step 停止。 | blocked 是硬阻断，不能被后续步骤绕过。 |
| `b01da45` | 2026-07-02 | 新增 resolve blocked step 工具和 prompt。 | 处理阻断后要写入证据，再把步骤恢复为 pending。 |
| `9562846` | 2026-07-03 | 防止直接 approve blocked step。 | blocked 必须先 resolve，不能靠 confirm-step 强行通过。 |
| `cda5337` | 2026-07-03 | 渲染 blocked step resolution。 | 让恢复动作有标准输出，包含证据和下一步。 |
| `84eb11b` | 2026-07-03 | 新增 describe ops plan。 | 用户可查看某个 plan 最新持久化状态，而不是依赖聊天记忆。 |
| `400d5d6` | 2026-07-03 | 新增 list ops plans。 | 多计划并存时可以查最近计划。 |
| `0c846ab` | 2026-07-03 | 支持按 status 过滤计划。 | 方便找 pending/approved/failed/blocked 相关计划。 |
| `7a0056b` | 2026-07-03 | 支持按 target 和 operation 过滤计划。 | 多平台、多操作时能定位目标计划。 |
| `1bf0f49` | 2026-07-03 | 新增系统 Python 运维检查。 | Klonet 启动依赖特定 Python/gunicorn/celery 路径，不能只看当前虚拟环境。 |
| `40abd00` | 2026-07-03 | 准备部署项目入口文件。 | 将 `mains/` 下启动文件复制到项目根目录，满足 screen 启动命令。 |
| `54b3300` | 2026-07-03 | 新增受控解压 recipe。 | 安装包解压前检查 zip/tar 成员，防止路径逃逸。 |
| `44611da` | 2026-07-03 | 新增受控安装脚本 recipe。 | 只允许白名单安装脚本和固定参数，避免任意 bash。 |
| `3d8b964` | 2026-07-03 | 新增受控文件写入 recipe。 | 配置/脚本写入进入 OperationPlan，dry-run 预览、真实执行备份。 |
| `b5b5d9e` | 2026-07-03 | 新增受控 nginx reload recipe。 | reload 前固定执行 `nginx -t`，校验失败则阻断。 |
| `5323ed0` | 2026-07-03 | 新增 Klonet 部署配置草案渲染。 | 让 Agent 生成可审查 config/nginx/frontend 草案，而不是直接写入。 |
| `52362f6` | 2026-07-04 | 新增 Nginx 路由解析。 | 读取已有 listen/server_name/location/proxy_pass/alias，避免端口和路径冲突。 |
| `dc82eef` | 2026-07-04 | 新增后端配置草案渲染。 | 根据端口生成 `config.py` 草案，作为写入前的审查材料。 |
| `3e86f3a` | 2026-07-04 | 对齐前端 config 草案。 | 如果已有前端 config.js，就沿用字段名，降低模板误写风险。 |
| `d6d8b8d` | 2026-07-04 | 新增压缩包和命令路径检查。 | 部署前先知道包结构和系统命令路径，减少运行时失败。 |
| `fe20574` | 2026-07-04 | 新增 Docker daemon config 草案。 | 修改 daemon.json 前先合并现有配置，保留 mirrors/dns/runtimes 等字段。 |
| `e73f4f9` | 2026-07-04 | 新增服务健康摘要。 | Redis/MySQL/RabbitMQ/Nginx/Docker 可复用则复用，不重复启动共享服务。 |
| `0d042d6` | 2026-07-04 | 新增安装脚本只读检查。 | 运行脚本前检查存在性、shebang、可执行位、风险标记和允许参数。 |
| `7e1e4e3` | 2026-07-04 | 新增平台健康验证。 | 启动/重启后统一检查 screen、进程 cwd、端口监听和 nginx 路由。 |
| `832ca46` | 2026-07-04 | 新增前端配置验证。 | 对比 frontend config.js、server/public/web_terminal 端口和 nginx alias。 |
| `6c6095f` | 2026-07-04 | 修复 prepare file recipe 绑定。 | 确保部署计划能正确把 prepare-files 绑定到文件准备动作。 |
| `d97d67a` | 2026-07-04 | 通过 sudo helper 运行安装准备。 | 需要写 `/root` 或高权限路径时，通过 helper 承接，而不是 Agent 直接写。 |
| `892e47c` | 2026-07-05 | 新增 sudo helper 调用边界设计文档。 | 先明确为什么真实执行必须经 sudo helper。 |
| `b54ee79` | 2026-07-05 | 新增 sudo helper 调用修复计划。 | 把设计拆成可执行修复步骤。 |
| `74ce050` | 2026-07-05 | 修复 recipe 真实执行走 sudo helper。 | 非 dry-run 时使用 `sudo -n` 调 helper，符合部署权限边界。 |
| `5bdb3e5` | 2026-07-05 | 新增专用 agent service 部署设计。 | 规划 `klonet-agent` 专用账号和 systemd 运行方式。 |
| `a3a90bd` | 2026-07-05 | 新增专用 agent service 部署计划。 | 将账号、服务、环境文件、sudoers 安装拆成任务。 |
| `010ea7e` | 2026-07-05 | 定义专用 agent service 合同测试。 | 先用测试明确 systemd 模板和安装脚本期望。 |
| `79ed565` | 2026-07-05 | 新增服务安装脚本和 dedicated account。 | 自动创建 `klonet-agent` 账号、安装 helper/sudoers/systemd。 |
| `1a7b45f` | 2026-07-05 | 文档解释 dedicated account 部署。 | 让用户知道为什么不用普通用户或 root 直接跑 Agent。 |
| `35540df` | 2026-07-05 | 记录真实 LLM persona 测试、Ops Git 回归、运维记忆。 | 把真实使用证据写入 docs/memory，作为后续优化依据。 |
| `eef31cd` | 2026-07-05 | 修复 helper quoting 测试跨平台问题。 | 测试不应依赖某个系统的 shlex 表现细节。 |
| `217c466` | 2026-07-05 | 新增 dedicated agent SSH 登录设计。 | 规划让 `klonet-agent` 可 SSH 登录但仍保留权限边界。 |
| `4e33fe5` | 2026-07-05 | 新增 dedicated agent SSH 登录计划。 | 将 shell、profile、权限目录拆成实施步骤。 |
| `a93a2ec` | 2026-07-05 | 支持 dedicated agent SSH login。 | 安装 profile，配置环境，便于以 agent 用户进入服务器排查。 |
| `b1c4e38` | 2026-07-05 | 文档解释 dedicated SSH agent workflow。 | 明确 SSH 登录、运行环境、sudo helper 的关系。 |
| `31a2367` | 2026-07-05 | README 新增 SSH 部署 quickstart。 | 给用户一条从服务器安装到登录使用的操作路径。 |
| `2fdf93b` | 2026-07-05 | README 新增 server venv bootstrap。 | 补齐服务器 Python 虚拟环境创建步骤。 |
| `1db565c` | 2026-07-05 | 配置 agent home tmpdir。 | 给 agent 用户独立 TMPDIR，避免无权限或临时文件污染。 |
| `d8eadc8` | 2026-07-05 | README 拆分 SSH 账号和 Python runtime 设置。 | 区分“能登录账号”和“能运行 Agent”的两类准备。 |
| `7d528e0` | 2026-07-05 | README 继续整理。 | 调整部署说明，使操作顺序更清晰。 |
| `7435011` | 2026-07-05 | 文档解释开启真实 Ops 执行。 | 说明 `KLONET_AGENT_OPS_REAL_EXECUTION=1` 与确认机制的关系。 |
| `8d72bd8` | 2026-07-05 | 默认启用真实 Ops 执行。 | 安装脚本写入环境变量，但真实修改仍受 OperationPlan 和 helper 白名单约束。 |
| `dfc4bc4` | 2026-07-05 | 丢弃中断的 tool_call history。 | 避免上一次未完成工具调用残留，导致 OpenAI/tool-call 协议错误。 |
| `bbc7fdc` | 2026-07-05 | 长安装输出改为 streaming。 | 安装脚本可能输出很长，流式输出避免缓冲过大和等待无反馈。 |
| `49eebbc` | 2026-07-05 | 安全恢复中断的 Ops steps。 | running 状态再次执行时转 blocked，要求检查环境后恢复。 |
| `87cd241` | 2026-07-05 | prompt 优先 dedicated ops deployment paths。 | 新平台默认放到 `klonet-agent` 专用目录，减少历史用户路径污染。 |
| `bf475cd` | 2026-07-05 | 优先精确端口诊断。 | 端口问题第一优先级查 PID/cwd，而不是用模糊运行态摘要替代。 |
| `8addb25` | 2026-07-05 | 运维源文件输出脱敏。 | 读取配置或源码时隐藏 password/token/key，防止泄露。 |
| `8a828fa` | 2026-07-05 | deploy precheck 没有 recipe 时阻断。 | 部署预检不能被当作普通 manual checkpoint 跳过。 |
| `78c6086` | 2026-07-05 | unreadable proc cwd 标为 unchecked。 | `/proc/<pid>/cwd` 读不到不是没有 cwd，而是证据不可读。 |
| `14582aa` | 2026-07-05 | 路由忽略非端口数字。 | 日期、plan id、platform 编号不应被误识别为端口。 |
| `2f5f45a` | 2026-07-06 | 文档和检索中补充 Klonet 源码获取说明。 | 明确安装包不是源码，平台源码需要 Git 或已验证副本。 |
| `d025d7c` | 2026-07-06 | 新增 Git 源码获取 runbook。 | 把后端/前端 Git 地址、SSH key、分支检查、远端回退边界写入知识库。 |
| `6445ec4` | 2026-07-06 | 允许受控写入 Ops 启动文件。 | Ops 仍不能改业务逻辑，但可通过 `write_ops_file` 修改平台启动必需文件。 |

### 这批更新的关键设计原则

#### 原则 1：执行前必须有计划

会修改服务器环境的任务不允许直接执行，必须先生成 OperationPlan。这样用户能看到：

```text
要做什么。
分几步做。
每一步风险是什么。
绑定了哪个 recipe。
将使用哪些参数。
需要哪种确认命令。
```

这让运维操作从“聊天中的建议”变成“可审计工作流”。

#### 原则 2：计划步骤必须绑定白名单 recipe

如果一个步骤没有 `recipe_id`，系统默认 blocked，尤其是部署预检这种关键步骤不能随便通过。

这背后的原则是：

```text
用户确认的是某个具体、受控、可验证的动作。
不是授权模型随便做它觉得合理的事。
```

#### 原则 3：真实执行必须经过 helper 和 sudoers

Agent 自身不应该持有无限 sudo 能力。真实执行链路是：

```text
execute_ops_next_step
  -> OperationPlanStore.execute_step
  -> ControlledRecipeRunner
  -> sudo -n /usr/local/bin/klonet-agent-op <allowlisted action>
  -> helper 内部再次校验参数和环境
```

这样即使模型或 Python 层出错，helper 和 sudoers 仍是最后的系统边界。

#### 原则 4：失败和中断不能自动重试

如果 recipe 返回 `environment_changed=unknown`，或步骤停留在 `running`，系统会进入 blocked。恢复流程是：

```text
只读检查运行态。
确认环境状态。
调用 resolve_ops_blocked_step 写入证据。
再按状态机继续。
```

这避免“上次可能执行了一半，但模型继续执行下一步”的危险。

#### 原则 5：Ops 写文件不是 Coding 改代码

这一阶段最后补齐了一个重要边界：Ops 可以写配置、nginx、前端 config、启动入口文件，但不能做普通业务源码开发。

判断依据是：

```text
是否属于平台启动和运维配置面。
是否由 render/inspect 工具给出草案或证据。
是否通过 write_ops_file recipe。
是否 dry-run 可预览。
是否真实执行前备份。
是否拒绝敏感路径。
```

### 和前面版本的关系

前面版本主要完成了：

```text
Mentor/Coding 双模式。
知识库检索。
意图路由。
回答策略。
工具权限白名单。
```

这批版本则是在此基础上新增第三类角色能力：

```text
Ops Agent = 只读诊断 + 受控计划 + 白名单执行 + 运行态验收。
```

所以当前系统已经不只是“教学 Agent”或“编码 Agent”，而是开始具备真实运维协作能力。但它的安全性来自多层代码约束，而不是来自模型自觉。

### 当前注意点

本次分析只覆盖已经提交的 Git 版本，不包括当前工作区未提交修改。当前工作区中还有以下未提交文件：

```text
ops/operations.py
prompts.py
tests/test_ops_operations.py
tools/registry.py
```

这些未提交内容如果后续进入 Git，需要单独再做一次版本分析。

## Ops Agent 预期架构：Plan-Execute-Replan 与权限层解耦

当前 Ops 能力已经具备 OperationPlan、recipe、helper/sudoers 等底座，但计划层和权限执行层仍然耦合较重。后续更理想的架构应该拆成五层：

```text
用户问题
  -> Ops Planner
  -> Action Router
  -> Executor
  -> Permission Guard
  -> System Helper / Tools
  -> Observation
  -> Replan
```

### 1. Ops Planner：只负责做什么

Planner 生成任务级、人类可读的运维计划，不直接关心 sudo、helper 或 recipe 细节。

例如用户要求部署 Klonet 平台时，Planner 输出应类似：

```text
1. 读取当前环境。
2. 检查端口、screen、已有平台和 Nginx 冲突。
3. 确认源码目录和入口文件。
4. 确保 Redis/MySQL/RabbitMQ 等共享基础服务可用。
5. 准备启动文件和配置。
6. 启动 Master/Worker/Celery/Web Terminal。
7. 验证平台健康状态。
```

这层的核心目标是像真实运维人员一样先列出行动计划，而不是提前暴露底层 recipe 绑定细节。

### 2. Action Router：把计划步骤翻译成动作

每个计划步骤应带一个 `action_type`，由 Action Router 映射到底层工具或 recipe。

示例：

```json
{
  "step_id": "ensure-shared-services",
  "title": "确保 Redis/MySQL/RabbitMQ 可用",
  "action_type": "ensure_shared_services"
}
```

映射关系类似：

```text
inspect_runtime          -> 只读环境工具
ensure_shared_services   -> ensure_shared_services recipe
prepare_project_files    -> prepare_project_files recipe
start_platform           -> start_platform_screens recipe
write_config             -> write_ops_file recipe
reload_nginx             -> reload_nginx recipe
```

这样 Planner 不需要直接绑定 `recipe_id`，只表达任务动作；Router 再负责执行路径选择。

### 3. Executor：负责按计划执行和记录 observation

Executor 是 Plan-Execute-Replan 的状态机核心。

理想流程：

```text
读取 next pending step
  -> 执行 action
  -> 记录 observation
  -> 标记 completed / blocked / failed / running
  -> 继续下一步或触发 replan
```

Executor 应支持：

```text
execute_until_blocked
execute_next
retry_step
skip_step
replace_step
append_step
```

当前系统已经有 `execute_until_blocked` 雏形，但还需要进一步把 blocked/failed 后的 Replan 机制做成显式状态流，而不是完全依赖模型自然语言判断。

### 4. Permission Guard：只负责能不能执行

Permission Guard 不判断任务本身是否合理，只判断某个 action 在当前上下文中是否允许执行。

规则示例：

```text
只读动作                 -> 直接允许
写配置/启动服务           -> 需要 plan confirm
销毁/删除/高影响重启       -> 需要 confirm-step
未知动作                 -> 拒绝
任意 shell               -> 拒绝
敏感路径或密钥文件         -> 拒绝
```

输出应是结构化结果：

```text
allowed
blocked
needs_confirm
needs_confirm_step
unsupported
```

这样权限系统不会污染 Planner 的人类可读计划，只在执行前作为安全闸门生效。

### 5. System Helper / Tools：真正操作系统

最底层才是真实系统操作：

```text
inspect_* tools
write_ops_file
klonet-agent-op helper
sudoers NOPASSWD
screen
nginx
docker_service.sh
```

这一层只能执行 Permission Guard 放行的动作，并且必须保持 allowlist、参数校验、敏感信息脱敏、失败状态可审计。

### 目标数据结构

未来 OperationPlan 更适合表达为任务计划，而不是 recipe 配置表：

```json
{
  "plan_id": "deploy-xxx",
  "mode": "ops",
  "objective": "部署 Klonet 平台",
  "status": "running",
  "steps": [
    {
      "step_id": "inspect-env",
      "title": "读取当前环境",
      "action_type": "inspect_ops_context",
      "risk": "readonly",
      "status": "completed",
      "observation": "检测到 nginx running，Redis missing"
    },
    {
      "step_id": "ensure-shared-services",
      "title": "确保共享基础服务可用",
      "action_type": "ensure_shared_services",
      "risk": "controlled_write",
      "permission": "plan_confirmed",
      "status": "pending"
    }
  ]
}
```

### 目标交互体验

理想体验应是：

```text
用户：帮我部署一个平台

Agent：
计划：
1. 检查环境
2. 确认源码
3. 确保基础服务
4. 写配置
5. 启动平台
6. 验证健康

用户：confirm deploy-xxx

Agent：
执行 1：完成，发现 Redis 缺失
执行 2：完成，源码就绪
执行 3：启动 docker_service.sh，完成
执行 4：写配置，完成
执行 5：启动 screen，完成
执行 6：验证失败，Web Terminal 端口冲突

Replan：
6a. 定位端口占用者
6b. 切换 web_terminal_port
6c. 重启 Web Terminal
```

### 总结

后续 Ops Agent 的关键优化方向是把三件事拆开：

```text
计划层：像运维人员一样思考和列计划。
执行层：按状态机执行、观察、重排。
权限层：作为安全闸门控制哪些动作能真执行。
```

当前 `OperationPlan + recipe + helper/sudoers` 是为了快速跑通安全执行底座；下一阶段应把 `recipe_id` 从计划层下沉到 Action Router / Executor 内部，让用户和模型主要面对任务级计划。
