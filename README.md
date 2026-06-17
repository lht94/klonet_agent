# Klonet Agent
**Klonet 专用教学协作 Agent**

## 更新：Klonet 专用教学协作 Agent 第一版
这一版开始从通用的 `agent_v7` 迁移成 `klonet_agent`，目标也从“个人对话 agent”变成“面向 Klonet 的教学协作 agent”。

核心思路是：**两个 agent 共用同一个底层框架，但是通过 profile 区分行为**。

- Mentor Agent：负责 Klonet 知识问答、源码解释、报错排查和项目进度理解
- Coding Agent：负责 Klonet 项目开发、代码修改、测试验证、diff 检查和项目日志记录

### 这一版主要做了什么
1. 修复迁移问题
   - 把代码里的 `agent_v7` 引用统一迁移成 `klonet_agent`
   - 新增命令行参数，可以通过 `--mode mentor` 或 `--mode coding` 选择 agent 模式
   - 增加最小导入测试，保证迁移后核心模块可以正常 import

2. 新增 Agent Profile 机制
   - 新增 `agents/profile.py`
   - 用 `AgentProfile` 描述不同 agent 的行为差异
   - Mentor 和 Coding 共用 `orchestrator`、`tools`、`memory`、`knowledge` 等底层模块
   - 不同 profile 只开放不同工具，避免 Mentor 模式误改代码

3. 重构系统提示词
   - 把原来单一的 `SYSTEM_PROMPT` 拆成多层 prompt
   - 清理旧个人 Agent 口吻，统一为 Klonet 教学协作 Agent 表达
   - 现在包含：
     - `CORE_SYSTEM_PROMPT`：核心身份与通用规则
     - `SAFETY_PROMPT`：安全与权限规则
     - `MENTOR_PROMPT`：导师模式规则
     - `CODING_PROMPT`：开发模式规则
     - `STYLE_PROMPT`：代码与注释风格规则
     - `TASK_PROMPT`：任务规划规则
   - 这样后续改某一种规则时，不需要改一整坨系统提示词

4. 完善会话隔离
   - `AgentSession` 现在保存 `user_id`、`project_id`、`mode`、`todos`、`workspace_path`、`journal_path`
   - 原来的全局 `TODOS` 已经迁移到 session 内部
   - 后续多用户部署时，不同同学的项目进度不会混在一起

5. 实现项目 Markdown 状态机
   - 新增 `journal/project_journal.py`
   - 每个用户、每个项目对应一个 md 文件：
     ```
     journals/{user_id}/{project_id}.md
     ```
   - 固定记录以下内容：
     - 项目目标
     - 当前状态
     - 需求与预期功能
     - 开发计划
     - 执行记录
     - 遇到的问题
     - 测试与验证
     - 功能差异与验收建议
     - 下一步
   - 这就是后续做“项目管理、老师验收、进度对齐”的核心文件

6. 实现 Klonet 知识库第一版
   - 新增 `knowledge/indexer.py`
   - 新增 `knowledge/retriever.py`
   - 新增 `knowledge/rag.py`
   - 当前先使用本地 JSONL 索引，不上复杂向量数据库
   - 检索对象包括 README、prompt、knowledge、journal、workspace、tools、memory 等文本资料
   - 后续可以逐步替换成 BM25 + 向量检索的混合 RAG

7. 新增代码风格指南
   - 新增 `knowledge/style_guide.md`
   - 用来约束 Coding Agent 生成代码和注释时尽量贴合当前项目风格
   - 目前原则是：中文注释、模块职责清楚、实现直观、不为了抽象而抽象

8. 初步实现 workspace 和安全工具
   - 新增 `workspace/manager.py`
   - 新增 `workspace/sandbox.py`
   - 文件读写被限制在当前用户的 workspace 内
   - 结构化工具包括：
     - `list_files`
     - `read_file`
     - `write_file`
     - `run_tests`
     - `show_diff`
   - 旧的 `run_command` 只保留兼容能力，并增加危险命令拦截

9. 新增评估集雏形
   - 新增 `evals/mentor_cases.jsonl`
   - 新增 `evals/coding_cases.jsonl`
   - 新增 `evals/error_cases.jsonl`
   - 后续可以用这些 case 做对比实验，例如：通用 Claude Code / 无 RAG 版本 / 有 RAG + 项目日志版本

10. 新增测试
    - 新增 `tests/test_imports.py`
    - 新增 `tests/test_cli_entry.py`
    - 新增 `tests/test_prompt_style.py`
    - 新增 `tests/test_session.py`
    - 新增 `tests/test_journal.py`
    - 新增 `tests/test_knowledge.py`
    - 新增 `tests/test_workspace_tools.py`
    - 当前验证结果：
      ```bash
      python -m pytest -q
      # 17 passed
      ```

### 现在的运行方式
现在可以直接在项目根目录下运行：
```bash
cd C:\Users\LHT\OneDrive\课设\agent开发\klonet_agent
python -m klonet_agent.agent --mode mentor --user-id default --project-id default
python -m klonet_agent.agent --mode coding --user-id default --project-id demo
```

也可以用脚本方式查看帮助或启动：
```bash
python agent.py --help
```

### 这一版还没有做的事情
- Klonet 真正源码仓库还没有接入 workspace
- RAG 目前只是本地关键词检索，还不是向量数据库
- ReviewAgent 目前只是 prompt 层面的轻量 review，还没有独立子 agent
- Web/API 服务还没有做，当前目标仍然是本地 CLI 可用
- token/速度优化目前只搭了基础结构，后续需要通过 evals 做对比实验

## 更新：项目架构
把项目封装成了一个个类

具体文件架构如下：
```
agent.py                         # CLI 启动入口，尽量很薄

klonet_agent/
├── __init__.py
├── config.py                    # 配置：模型名、路径、token限制、开关
├── prompts.py                   # 系统提示词、角色提示词、任务提示词
│
├── session.py                   # AgentSession：一次用户会话
├── orchestrator.py              # AgentOrchestrator：主循环/编排器
│
├── llm/
│   ├── __init__.py
│   ├── client.py                # LLMClient：模型调用封装
│   └── schemas.py               # 消息、工具调用、响应结构
│
├── tools/
│   ├── __init__.py
│   ├── registry.py              # ToolRegistry：工具注册表
│   ├── executor.py              # ToolExecutor：工具执行入口
│   ├── shell.py                 # ShellTool：命令执行
│   ├── file_ops.py              # 文件读写工具
│   └── web.py                   # web_fetch 等联网工具
│
├── memory/
│   ├── __init__.py
│   ├── store.py                 # MemoryStore：长期记忆、用户画像、history
│   ├── compactor.py             # 记忆压缩
│   └── models.py                # 记忆数据结构
│
├── journal/
│   ├── __init__.py
│   ├── project_journal.py       # ProjectJournal：项目 Markdown 状态机
│   └── templates.py             # md 模板
│
├── knowledge/
│   ├── __init__.py
│   ├── skill_loader.py          # 你现在的 skills.py 可迁移到这里
│   ├── rag.py                   # KnowledgeBase：Klonet RAG
│   ├── indexer.py               # 索引 Klonet 源码/文档
│   └── retriever.py             # 检索相关上下文
│
├── workspace/
│   ├── __init__.py
│   ├── manager.py               # WorkspaceManager：为用户创建隔离代码副本
│   ├── git_ops.py               # clone/worktree/diff/patch
│   └── sandbox.py               # 执行限制、路径限制
│
├── subagents/
│   ├── __init__.py
│   ├── manager.py               # SubAgentManager
│   └── agents.py                # ResearchAgent / CodingAgent / ReviewAgent
│
├── tracing/
│   ├── __init__.py
│   └── logger.py                # TraceLogger：工具调用、失败原因、产物记录
│
└── app/
    ├── cli.py                   # 命令行运行
    └── service.py               # 以后部署成 Web/API 服务时用
```
没有完成的模块用占位符填充，其他模块的对应关系如下：
```
client.py -> llm/client.py
history.py -> memory/store.py
skills.py -> knowledge/skill_loader.py
tools.py -> tools/*
run.py -> 
```

## 更新：任务规划模块的设计
实际上很简单
1. 在系统提示词中告诉它应该什么时候分步做任务，如何调用工具分步做任务
2. 定义一个任务规划工具，让llm输出完整的计划列表
3. 再定义一个工具函数，用来查看与更新任务执行状态
4. 最后做一个检验，模型说完成任务之后再手动二次检验，避免llm出错而导致需要用户自己提醒

## 更新：记忆模块的设计
现在简单的把history数组无线增长显示是不可以的，不仅会忘记之前的目标，还会导致记忆窗口经常溢出

人的记忆分为三种：
- 目前的记忆（工作记忆）：记着我们当前在做什么，记得十分清除
- 之前一段时间的记忆（情景记忆）：记着我们前一段时间做了什么，但是记不太清具体细节，越往前追忆记忆越模糊
- 一直以来的记忆（核心记忆）：记着我们的一些主线目标与人生规划，会记特别久

所以我们agent的记忆系统设计也可以仿照着来：
- 工作记忆：最近几轮的原文（即原本的history数组，但是只记录最近几轮对话）
- 情景记忆：之前对话的简单记录，在压缩时触发写入，可以以时间为维度来进行记录，llm可以按需检索（即history过大时就把他整理成一个小记录，然后清空之前的）
- 核心记忆：常驻任务提示，每轮都要输入进去。同时字数要有限制，以防膨胀（记录包括用户偏好，核心目标等等）

**实际上也就是文本记录**，具体实现如下，用几个文件来分别实现几个记忆：
| 文件 | 作用 |
| :--- | :--- |
| `history.jsonl` | 记录完整的对话 |
| `YYYY-MM-DD.md` | 记录当天事件的摘要 |
| `MEMORY.md` | 记录核心的目标 |
| `USER.md` | 记录用户偏好 |
| `tokens.jsonl` | 记录token用量 |

### history数组
上个版本的history数组，现在依旧保留，只不过当检测到溢出时，就会触发压缩机制，清空数组

### history.jsonl
上个版本的history是个单独定义的列表，代码一停，history就完全丢失了（即history存在内存里，没有实现永久性存储）。

.jsonl 的好处是读写极快，文件再大也不需要像普通 .json 那样把整个文件加载到内存里解析。

和上个版本相同，每生成一条消息，就追加一行到文件末尾，形成永久存储。开始对话之前，先读取这个文件，恢复之前的上下文。（不是全读，而是去寻找最后一个归档压缩标记，把这之后的对话加载到记忆里）

### USER.md
.md 的好处是，大模型处理非结构化的 Markdown 比处理严格的 JSON 键值对更自然，不容易出错

注意USER.md是全覆盖更新，因为用户偏好可能会改变

### YYYY-MM-DD.md
新增一个工具，如果大模型在对话中遇到有价值的进展就追加到 YYYY-MM-DD.md 里

### MEMORY.md
新增一个工具，如果大模型积累了好几天的日志，或者某个模块彻底做完、或者你明确要求它做阶段性复盘时，它再去通读近期的日志，把提炼出来的核心状态和规则覆盖更新到 MEMORY.md

### 记忆压缩
判断当前的对话轮数是否超过了阈值（比如 10 轮），或者计算 Token 数是否过高。如果达标，用系统级 Prompt，让大模总结：“请总结上述对话中的关键技术结论或任务进度，并调用对应的工具更新到 MEMORY.md 中。”

总结完毕后，手动打上标签

## 旧版架构记录
把项目拆成了三层
1. 基础设施层：client，history，tools，skills
2. 编排层：runner （即把各个模块拼接在一起）
3. 入口层：main，agent （main提供拼装与导出，agent作为方便运行的启动脚本）

具体文件架构如下：
```
agent.py                    项目入口
agent_v5/
    ├── __init__.py      
    ├── client.py           大模型客户端初始化
    ├── prompts.py          系统提示词
    ├── history.py          负责对话记忆
    ├── tools.py            工具定义和工具执行
    ├── skills.py           技能系统
    ├── runner.py           真正的对话流程
    ├── main.py             实例化各个模块
```

## 拆分实现的关键步骤
1. 把原本的一段段代码封装成一个个函数
2. 然后在主函数里import这个函数，再调用即可
3. 目前以面向过程编程为原则，主要是比较简单。后续做大后，为了维护方便可以改成面向对象编程
   
## 运行方法
当前项目建议在仓库根目录运行：
``` bash
cd C:\Users\LHT\OneDrive\课设\agent开发\klonet_agent
python -m klonet_agent.agent --help
python agent.py --help
```
