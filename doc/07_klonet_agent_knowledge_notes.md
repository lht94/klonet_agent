# Klonet Agent 知识整理

## 版本记录

本文基于当前 Git 历史中的以下版本整理：

```text
987ba87 Initial Klonet agent implementation
23d0689 fix: make local cli entrypoints testable
b495a63 refactor: align prompts with klonet teaching agent
e3c04e9 fix: exclude runtime memory from knowledge index
```

重点代码位置：

- `agents/profile.py`
- `orchestrator.py`
- `tools/executor.py`
- `workspace/sandbox.py`
- `tools/file_ops.py`
- `tools/shell.py`
- `tools/registry.py`

## 核心问题

在 Agent 系统中，不能只依赖 prompt 告诉模型“不要做某件事”。

Prompt 的作用是行为引导，例如：

```text
Mentor 模式默认不修改代码。
Coding 模式修改代码后需要测试和 review。
```

但 prompt 本质上只是自然语言规则。模型可能误解、忘记、被用户诱导，或者在复杂任务中错误调用工具。

因此，真正可靠的约束应该放在代码执行链路里。这个项目的工具约束设计，就是把“模型应该做什么”和“模型实际能做什么”分开处理。

## 总体设计

当前工具约束可以理解为四道门：

```text
Profile 工具白名单
    ↓
只向模型暴露允许的工具 schema
    ↓
ToolExecutor 执行前二次检查
    ↓
具体工具内部做 workspace、路径、命令限制
```

这四层共同保证：

1. Mentor 模式主要用于解释、检索和教学。
2. Coding 模式才允许读写代码、运行测试、查看 diff。
3. 即使模型伪造了不该调用的工具，执行层也会拒绝。
4. 即使进入 Coding 模式，文件和命令仍被限制在安全范围内。

## 第一层：Profile 工具白名单

文件：`agents/profile.py`

`AgentProfile` 中有一个字段：

```python
allowed_tools: set[str]
```

它表示当前 Agent 模式允许使用哪些工具。

Mentor 模式的工具集合是：

```python
MENTOR_TOOLS = {
    "load_skill",
    "search_knowledge",
    "read_project_journal",
    "append_episode",
    "write_memory",
    "write_user",
    "web_fetch",
    "update_todos",
}
```

这些工具偏向：

- 加载技能。
- 检索知识库。
- 读取项目日志。
- 记录长期记忆。
- 获取网页文本。
- 维护任务计划。

Coding 模式在 Mentor 工具基础上增加：

```python
CODING_TOOLS = MENTOR_TOOLS | {
    "list_files",
    "read_file",
    "write_file",
    "run_tests",
    "show_diff",
    "create_project_journal",
    "append_journal_event",
    "update_project_status",
    "record_test_result",
    "record_acceptance_gap",
}
```

这些工具才涉及：

- 列文件。
- 读文件。
- 写文件。
- 跑测试。
- 查看 git diff。
- 写项目日志和验收记录。

所以 Mentor 模式不能改代码，不只是因为 prompt 里说了“默认不修改代码”，而是它的 `allowed_tools` 里根本没有 `write_file`。

## 第二层：只向模型暴露允许的工具

文件：`orchestrator.py`

模型能调用哪些工具，取决于发送给大模型 API 的 `tools` 参数。

当前代码没有把完整工具列表直接传给模型，而是调用：

```python
self.llm.complete(messages=history, tools=self._visible_tools())
```

`_visible_tools()` 会根据当前 profile 过滤工具：

```python
def _visible_tools(self) -> list[dict]:
    return [
        tool
        for tool in TOOLS
        if tool["function"]["name"] in self.profile.allowed_tools
    ]
```

这一步的意义非常大：

模型不是直接调用 Python 函数。模型只能从工具 schema 中选择函数名和参数。

如果当前是 Mentor 模式，那么传给模型的工具 schema 里没有 `write_file`、`run_tests`、`show_diff`。模型通常不知道这些工具存在，也无法通过正常工具调用流程选择它们。

这一层属于“可见性约束”。

## 第三层：ToolExecutor 执行前二次检查

文件：`tools/executor.py`

只隐藏工具还不够，因为系统要防御异常情况。例如：

- 模型输出了不在 schema 中的工具名。
- 未来某段代码绕过了 `_visible_tools()`。
- 外部调用者直接调用了 `ToolExecutor.run()`。

因此，执行器在真正执行工具前还有一次检查：

```python
if self.allowed_tools is not None and tool_name not in self.allowed_tools:
    return f"Error: 当前 Agent 模式不允许调用工具 {tool_name}"
```

而 `ToolExecutor` 初始化时拿到的是当前 profile 的白名单：

```python
ToolExecutor(
    session=self.session,
    allowed_tools=self.profile.allowed_tools,
)
```

这意味着：

即使 Mentor 模式下出现了一个伪造的 `write_file` 工具调用，执行器也会返回错误，不会真的写文件。

这一层属于“执行权限约束”。

## 第四层：具体工具内部的安全限制

即使是 Coding 模式，工具也不是无限制执行。

### 文件路径限制

文件：`workspace/sandbox.py`

路径解析通过 `WorkspaceSandbox.resolve_path()` 完成：

```python
raw = Path(path)
target = raw if raw.is_absolute() else self.workspace_path / raw
resolved = target.resolve()
if not self.is_path_allowed(resolved):
    raise PermissionError(...)
```

`is_path_allowed()` 的核心判断是：

```python
path.resolve().relative_to(self.workspace_path)
```

这表示目标路径必须能被证明位于当前 workspace 内。

因此，类似下面的路径逃逸会被拒绝：

```text
../outside.txt
C:\Users\...\其他项目\secret.py
```

文件工具 `list_files`、`read_file`、`write_file` 都通过 workspace manager 获取 sandbox，再解析路径。

### 测试命令白名单

文件：`workspace/sandbox.py`

测试命令不是随便运行 shell，而是先检查命令名：

```python
ALLOWED_TEST_COMMANDS = {
    "pytest",
    "python",
    "python3",
}
```

`validate_test_command()` 会拒绝不在白名单里的命令。

这能避免模型借 `run_tests` 执行下载、删除、系统修改等命令。

### 危险命令拦截

文件：`workspace/sandbox.py`

为了兼容旧的 `run_command`，系统还保留了危险命令检查：

```python
DANGEROUS_COMMANDS = {
    "rm",
    "sudo",
    "chmod",
    "chown",
    "mkfs",
    "dd",
    "shutdown",
    "reboot",
    "curl",
    "wget",
    "git",
    "pip",
    "npm",
}
```

`reject_dangerous_command()` 发现这些命令后，会返回拒绝原因。

这说明系统倾向于使用结构化工具，而不是让模型直接自由执行 shell。

## Prompt 约束和工具约束的区别

| 类型 | 作用 | 强度 | 例子 |
| :--- | :--- | :--- | :--- |
| Prompt 约束 | 告诉模型应该怎么做 | 软约束 | Mentor 模式默认不修改代码 |
| 工具可见性约束 | 决定模型能看到哪些工具 | 中强约束 | Mentor 模式不暴露 `write_file` |
| 执行权限约束 | 决定工具调用能否真正执行 | 强约束 | `ToolExecutor` 拒绝非白名单工具 |
| 沙箱约束 | 限制工具执行范围 | 强约束 | 文件路径不能逃出 workspace |

一句话总结：

```text
Prompt 是规则说明，工具约束是系统门禁。
```

## 一个具体例子

假设用户在 Mentor 模式下说：

```text
帮我直接修改代码。
```

系统会发生这些事情：

1. Prompt 会提醒模型：Mentor 模式默认不修改代码。
2. Mentor 的 `allowed_tools` 中没有 `write_file`。
3. `_visible_tools()` 不会把 `write_file` 传给模型。
4. 即使异常情况下模型请求 `write_file`，`ToolExecutor` 也会拒绝。

所以 Mentor 模式的限制不是单点规则，而是多层防线。

## 为什么这种设计重要

### 1. 降低模型误操作风险

大模型可能误判任务边界。如果只靠 prompt，它可能在解释问题时顺手调用写文件工具。

工具白名单可以把这类错误挡在系统层。

### 2. 支持同一底层多个 Agent

Mentor 和 Coding 共用：

- LLM client
- Orchestrator
- Memory
- Knowledge
- Tools
- Workspace

区别只体现在 profile 上。

这比复制两套 Agent 更容易维护。

### 3. 方便未来扩展更多角色

以后可以继续加：

- Review Agent：只读文件、看 diff、写 review，不写代码。
- Teacher Agent：只读日志、读知识库，生成评分建议。
- Experiment Agent：可运行 eval，但不能写业务代码。

每个角色只需要定义自己的 `allowed_tools`。

### 4. 更适合教学场景

教学 Agent 不应该一上来就替学生改完代码。

Mentor 模式负责解释和引导，Coding 模式负责开发执行。这个边界可以帮助学生理解：

```text
什么时候是在学习理解，什么时候是在进入开发改动。
```

## 当前实现的注意点

当前工具约束已经有基本框架，但还有一些可以继续增强的地方：

1. `run_command` 是旧兼容工具，未来最好逐步减少使用，改成更多结构化工具。
2. `write_memory` 和 `write_user` 在 Mentor 模式下仍然允许，因为 Mentor 需要维护长期记忆；但这也意味着它能写 memory 文件，需要后续明确隐私边界。
3. `web_fetch` 在 Mentor 模式下允许联网获取网页文本，后续如果部署到真实环境，需要考虑域名白名单和超时限制。
4. 当前工具权限主要按模式区分，后续还可以按用户、项目、课程阶段进一步细分。

## 设计结论

这一块设计的核心价值是：

```text
把 Agent 的行为边界从“模型自觉遵守”升级为“系统强制限制”。
```

Mentor/Coding 双模式并不是简单换 prompt，而是通过：

1. Profile 白名单。
2. 工具 schema 过滤。
3. 执行器二次校验。
4. workspace 和命令沙箱。

共同形成一套比较可靠的工具权限系统。

## RAG 基础工具与四层知识关系

### 当前不是向量 RAG

当前 `knowledge/indexer.py`、`knowledge/retriever.py`、`knowledge/rag.py` 组成的是一个轻量检索增强流程，但底层还不是标准向量 RAG。

原因是：

```text
当前没有 embedding。
当前没有向量数据库。
当前没有余弦相似度或 ANN 近似检索。
```

当前实现是：

```text
indexer.py
  读取文本文件
  按固定长度切 chunk
  写入 knowledge/index.jsonl

retriever.py
  对用户问题做关键词分词
  遍历 index.jsonl
  根据关键词出现次数和路径命中打分
  返回 top_k chunk

rag.py
  把 chunk 格式化成证据文本
  作为 search_knowledge 工具结果交给 Agent
```

所以更准确的说法是：

```text
当前是关键词检索增强生成范式。
```

它具备 RAG 的基本形态：

```text
检索外部知识
把检索结果注入上下文
让模型基于证据回答
```

但检索层还比较简单，不是向量检索，也不是 BM25。后续可以把 `retriever.py` 替换为 BM25、向量库或混合检索，而不必重写 Agent 主流程。

### 三个文件的职责边界

这三个文件可以理解为知识库底层工具：

```text
indexer.py   建索引
retriever.py 查索引
rag.py       包装增强上下文
```

它们和 RAG 的对应关系是：

```text
indexer.py
  属于检索前准备阶段。
  它构建可检索数据库。当前是 JSONL 关键词索引，未来可以升级为向量数据库或混合索引。

retriever.py
  对应 Retrieval。
  它根据用户问题查找相关知识。当前是关键词匹配，不是余弦近似。

rag.py
  对应 Augmentation 的接口层。
  它把检索结果整理成可注入 prompt/history 的证据文本。

orchestrator.py + llm/client.py
  对应 Generation。
  它们把用户问题、工具结果和历史上下文交给模型生成最终回答。
```

完整链路是：

```text
资料
  ↓
indexer.py
  ↓
knowledge/index.jsonl

用户问题
  ↓
search_knowledge 工具
  ↓
rag.py
  ↓
retriever.py
  ↓
相关 chunk
  ↓
rag.py 格式化为证据文本
  ↓
orchestrator.py 放回 history
  ↓
LLM 生成最终回答
```

### 四层知识对底层工具是否透明

四层知识包括：

```text
1. 人工知识层
2. 机器索引层
3. 原始证据层
4. 运行经验层
```

从“底层检索工具”的角度看，四层知识应该尽量保持透明。

也就是说，`indexer.py`、`retriever.py`、`rag.py` 不应该写死大量业务判断，例如：

```text
如果是拓扑部署问题，就必须查 flows/topology_deploy.md。
如果是运维问题，就必须查 ops/common_troubleshooting.md。
```

这些判断更适合放在：

```text
工具参数
查询策略
metadata 过滤
orchestrator 主流程
Agent prompt 或 task template
```

底层工具更应该负责：

```text
把知识资料索引出来。
根据 query 和过滤条件找到候选证据。
按统一格式返回给上层。
```

### 四层知识不只是打分机制

四层知识可以通过“资料来源 + metadata + 打分机制”参与检索，但它们本质上不只是打分。

更准确地说，四层知识包含三类东西：

```text
1. 存储位置
   例如 knowledge/klonet、knowledge/klonet_index、raw_manifest.jsonl、knowledge/klonet_experience。

2. 治理规则
   例如是否需要脱敏、是否能进入公共索引、是否需要人工 review、是否是 source_of_truth。

3. 检索策略
   例如优先查人工知识层，必要时查机器索引层，再回到原始证据层核对。
```

打分机制只解决第三类问题的一部分：

```text
同一批候选证据里，哪些更相关？
```

它不能完全表达：

```text
这份资料是否敏感。
这份资料是否过时。
这份资料是否只是原始证据，不能直接作为总结。
这份运行经验是否已经被验证。
```

因此，四层知识应该依赖 metadata，而不是只依赖关键词分数。

`raw_manifest.jsonl` 已经开始表达这类 metadata：

```text
collection
category
priority
quality
sensitivity
target_layers
derived_text
action
```

这些字段后续应该进入索引结果，供 `retriever.py` 或更上层的路由逻辑使用。

### 主流程什么时候调用哪一层

四层知识的调用策略更适合由上层决定。

例如 Mentor 问答：

```text
1. 先查人工知识层，获得稳定解释。
2. 再查运行经验层，找真实案例和常见坑。
3. 如果需要定位代码，再查机器索引层。
4. 如果仍有不确定，再回到原始证据层核对。
```

Coding 开发：

```text
1. 先查人工知识层，确认开发规范和业务流程。
2. 查机器索引层，定位路由、函数、类、配置项。
3. 读取原始证据层中的源码。
4. 查运行经验层，避免重复踩坑。
```

所以四层知识不是要求底层函数“自动理解所有业务”，而是要求底层函数能够按层、按 metadata、按 query 检索到正确候选资料。至于什么时候优先使用哪一层，应由 Agent 的任务流程决定。

### 当前实现与目标状态的差距

当前实现中，`KnowledgeChunk` 只有：

```text
source
path
title
content
```

这意味着当前检索结果还没有显式携带：

```text
layer
collection
priority
quality
sensitivity
target_layers
```

因此，当前底层工具对四层知识是“弱透明”的：

```text
它能检索文本。
但它还不知道这段文本属于哪一层知识。
```

目标状态应该是：

```text
raw_manifest.jsonl
  ↓
manifest-driven indexer
  ↓
带 metadata 的 chunk/index
  ↓
retriever 支持 layer、priority、quality、sensitivity 过滤和加权
  ↓
rag 按层组织证据文本
```

例如未来的 chunk 可以扩展为：

```json
{
  "source": "klonet",
  "path": "knowledge/klonet/flows/topology_deploy.md",
  "title": "拓扑部署流程#1",
  "content": "...",
  "layer": "human_knowledge",
  "collection": "02_vemu_uestc_code",
  "priority": "P0",
  "quality": "source_of_truth",
  "sensitivity": "normal"
}
```

这样底层检索仍然保持通用，但上层可以明确控制：

```text
这次只查 human_knowledge。
这次允许查 machine_index。
这次不要返回 review_required。
这次优先 P0 source_of_truth。
```

### 结论

当前理解可以整理为：

```text
indexer.py 用来构建检索数据库。
当前只是 JSONL 关键词索引，后续可以升级为向量数据库或混合索引。

retriever.py 根据用户问题检索数据库。
当前是关键词查找，不是余弦相似度。

rag.py 把检索结果整理成增强上下文。
最终回答由 orchestrator.py 和 llm/client.py 调用模型生成。
```

四层知识对底层工具应该尽量透明，但不能完全只靠打分机制实现。更合理的设计是：

```text
四层知识通过目录结构和 raw_manifest metadata 表达。
indexer 负责把这些 metadata 写进索引。
retriever 负责按 query + metadata 检索和排序。
rag 负责按层组织证据。
orchestrator 决定在不同任务阶段调用哪一层知识。
```

### 例子：用户问“拓扑部署进度条卡住怎么办”

假设用户在 Mentor 模式下提问：

```text
拓扑部署时前端进度条一直卡住，应该怎么排查？
```

#### 当前系统实际流程

当前代码里的流程比较简单：

```text
用户问题
  ↓
orchestrator.py
  ↓
模型根据 prompt 决定调用 search_knowledge
  ↓
tools/executor.py
  ↓
knowledge/rag.py
  ↓
knowledge/retriever.py
  ↓
knowledge/index.jsonl
  ↓
返回相关 chunk
  ↓
rag.py 格式化为证据文本
  ↓
orchestrator.py 把证据文本作为 tool result 放回 history
  ↓
LLM 生成回答
```

在当前实现里，系统并不知道“进度条卡住”一定应该优先查运行经验层、再查人工知识层、最后查源码证据层。

它现在主要依赖两件事：

```text
1. prompt 要求 Mentor 回答前优先 search_knowledge。
2. retriever.py 用关键词在 index.jsonl 里找包含“拓扑”“部署”“进度条”等词的 chunk。
```

也就是说，当前系统是“关键词驱动”的：

```text
query = "拓扑部署时前端进度条一直卡住，应该怎么排查？"
terms = ["拓扑部署", "前端进度条", "一直卡住", "排查"] 附近的关键词
```

然后 `retriever.py` 遍历 `knowledge/index.jsonl`，看哪些 chunk 命中这些词。

当前各文件的作用是：

```text
orchestrator.py
  负责对话主流程。
  它接收用户问题、保存 history、处理模型工具调用，并把工具结果再交回模型。

tools/registry.py
  定义 search_knowledge 这个工具的 schema，让模型知道可以调用它。

tools/executor.py
  收到模型请求 search_knowledge 后，真正执行对应 Python 逻辑。

knowledge/rag.py
  提供 search_knowledge 的统一入口。
  它调用 retriever，并把结果包装成模型可读的证据文本。

knowledge/retriever.py
  真正执行关键词检索。
  它从 index.jsonl 找相关 chunk，并按 score 排序。

knowledge/indexer.py
  事先构建 index.jsonl。
  没有索引文件时，retriever 会触发它构建索引。

knowledge/index.jsonl
  当前机器可检索数据库。
  里面是一条条 chunk。

llm/client.py
  最后调用大模型，让模型基于用户问题和检索证据生成回答。
```

当前可能被检索到的文件包括：

```text
knowledge/task_templates.md
doc/10_klonet_knowledge_base_generation_route.md
doc/06_current_implementation_notes.md
README.md
```

如果这些文档里没有“进度条卡住”的真实经验，那么模型只能基于已有证据给出泛化排查建议，并说明证据不足。

#### 目标四层知识系统中的流程

目标系统应该更精确。

同样的问题：

```text
拓扑部署时前端进度条一直卡住，应该怎么排查？
```

理想流程应该是：

```text
1. 判断问题类型
   这是 Mentor 问答 + 报错排查 + 运维经验问题。

2. 先查人工知识层
   查 knowledge/klonet/flows/topology_deploy.md
   查 knowledge/klonet/ops/common_troubleshooting.md

3. 再查运行经验层
   查 knowledge/klonet_experience/common_errors.md
   查 knowledge/klonet_experience/cases/*progress*.md

4. 如果需要定位代码，再查机器索引层
   查 routes.jsonl、celery_tasks.jsonl、domain_map.jsonl
   定位 topo deploy、process_bar、worker heartbeat 相关代码。

5. 如果证据仍不足，回到原始证据层
   根据 raw_manifest.jsonl 找原始运维文档、源码、历史文档。
```

这里“系统怎么知道查哪层”不是靠 `retriever.py` 自己凭空判断，而是由上层策略决定。

策略来源可以有三类：

```text
1. Agent 模式
   Mentor 模式优先查人工知识层和运行经验层。
   Coding 模式更早查机器索引层和原始源码证据。

2. 任务模板
   knowledge/task_templates.md 里“报错排查”模板规定：
   先复述现象，再检索相似问题，再定位错误类型，再给验证命令。

3. metadata 过滤
   raw_manifest.jsonl 和未来 chunk metadata 标明：
   layer、category、priority、quality、sensitivity。
```

目标状态下，检索请求不应该只是：

```python
search_knowledge(query="拓扑部署进度条卡住")
```

而应该逐渐演进为类似：

```python
search_knowledge(
    query="拓扑部署进度条卡住",
    layers=["human_knowledge", "experience"],
    categories=["ops", "topology", "troubleshooting"],
    min_priority="P1",
    exclude_sensitivity=["review_required"]
)
```

如果第一轮找不到，再放宽条件：

```python
search_knowledge(
    query="topology deploy process bar worker heartbeat",
    layers=["machine_index", "raw_evidence"],
    categories=["code", "ops_teaching"],
    include_review_required=True
)
```

#### 四层知识中每类文件的作用

以“拓扑部署进度条卡住”为例，四层文件可以这样分工：

```text
人工知识层
  文件：
    knowledge/klonet/flows/topology_deploy.md
    knowledge/klonet/ops/common_troubleshooting.md

  作用：
    给稳定解释。
    说明拓扑部署的正常流程、关键组件、进度条含义、常见排查顺序。

运行经验层
  文件：
    knowledge/klonet_experience/common_errors.md
    knowledge/klonet_experience/cases/2026-xx-topology-progress-stuck.md

  作用：
    给真实案例。
    告诉 Agent 以前遇到过什么现象、最后怎么定位、根因是什么。

机器索引层
  文件：
    knowledge/klonet_index/routes.jsonl
    knowledge/klonet_index/celery_tasks.jsonl
    knowledge/klonet_index/domain_map.jsonl
    knowledge/klonet_index/symbols.jsonl

  作用：
    给定位能力。
    快速告诉 Agent：相关 API、任务函数、类、配置项在哪些源码文件。

原始证据层
  文件：
    raw_manifest.jsonl
    knowledge/extracted_docs/*.raw.md
    外部 Klonet 源码和原始文档

  作用：
    给最终证据。
    当人工知识或经验文档不足时，回到源码和原始材料核对事实。
```

#### 当前和目标的关键差别

当前系统：

```text
所有可索引文本基本被放进同一个 index.jsonl。
retriever.py 主要按关键词打分。
系统不能严格区分“这是人工知识层”还是“这是运行经验层”。
```

目标系统：

```text
每条知识 chunk 都带 layer、category、priority、quality、sensitivity 等 metadata。
orchestrator 或 search_knowledge 工具可以按任务类型选择优先检索哪几层。
retriever.py 根据 query + metadata 做过滤和加权。
rag.py 按层组织证据，让模型知道哪些是总结、哪些是案例、哪些是原始证据。
```

也就是说，底层文件不是自己决定业务流程，而是提供能力：

```text
indexer.py
  把四层知识和 metadata 写入索引。

retriever.py
  按 query 和 metadata 检索。

rag.py
  把不同层的结果组织成证据文本。

orchestrator.py
  根据当前 Agent 模式和任务类型，决定先查哪层、后查哪层。
```

## 四层知识的设计定位

Klonet 知识库的四层不是简单的“从浅到深”，也不是固定的“从可信到不可信”。它们代表四类用途不同的知识资产：

```text
1. 人工知识层
2. 机器索引层
3. 原始证据层
4. 运行经验层
```

不同任务下，它们的查询优先级不同。

### 人工知识层：整理好的 Klonet 教材

人工知识层是经过人工整理、适合长期维护的 Markdown 知识。

它可以理解为：

```text
关于 Klonet 的教材、项目手册和精华知识。
```

它应该覆盖：

```text
项目原理
系统架构
核心业务流程
开发规范
维护方法
常见概念
接口使用方式
```

典型目录可以是：

```text
knowledge/klonet/
  00_project_overview.md
  01_architecture.md
  flows/topology_deploy.md
  dev/backend_api_development.md
  ops/common_troubleshooting.md
  user_api/vemu_api_usage.md
```

这一层的主要作用是解释。

例如用户问：

```text
Klonet 的拓扑部署流程是什么？
为什么要区分 master 和 worker？
新同学怎么理解后端 API 开发方式？
```

应该优先查人工知识层。

它的优点是：

```text
可读性强
结构稳定
适合教学
适合直接给 Agent 作为总结性依据
```

它的风险是：

```text
可能过期
需要人工维护
不能替代原始源码核对
```

### 机器索引层：源码定位地图

机器索引层是自动生成的结构化索引。

它可以理解为：

```text
源码和系统结构的定位表。
```

它不主要负责解释原理，而是告诉 Agent：

```text
某个功能模块在哪里。
某个 API 路由在哪里注册。
某个类或函数在哪个文件。
某个 Celery 任务在哪定义。
某个业务域涉及哪些目录。
```

典型文件可以是：

```text
knowledge/klonet_index/
  files.jsonl
  symbols.jsonl
  routes.jsonl
  celery_tasks.jsonl
  config_items.jsonl
  data_models.jsonl
  domain_map.jsonl
```

这一层的主要作用是定位。

例如用户问：

```text
拓扑部署接口在哪个文件？
worker 心跳任务在哪里定义？
链路延迟查询相关代码在哪些模块？
```

应该优先查机器索引层。

它的优点是：

```text
查找快
结构明确
适合 Coding Agent
避免 LLM 在大量源码里盲目翻找
```

它的风险是：

```text
只告诉位置，不解释原因
需要自动生成，手写容易过期
索引不等于源码事实，最终仍可能需要回原始证据层核对
```

### 原始证据层：完整事实来源

原始证据层是完整的原始资料库。

它包括：

```text
Klonet 源码
旧项目文档
PDF/DOCX/PPTX
实验材料
运维原文档
抽取出的 raw markdown
raw_manifest.jsonl
```

它可以理解为：

```text
最终事实依据。
```

这一层不一定适合第一时间直接给 LLM 大量阅读，因为它通常：

```text
体量大
噪声多
格式杂
可能包含敏感信息
可能包含历史遗留和生成产物
```

但它非常重要，因为当人工知识层和机器索引层不足时，最终要回到原始证据层核对。

需要注意：原始证据层不只是“任务失败后才查”。它还应该用于：

```text
高风险事实核对
代码修改前确认真实实现
人工知识层可能过期时的验证
回答涉及具体接口、参数、配置、命令时的确认
```

例如用户问：

```text
这个接口真实参数名是什么？
这个函数现在到底怎么实现？
运维手册里原文怎么写？
```

就应该回到原始证据层。

它的优点是：

```text
最接近事实来源
可以核对人工总结是否正确
可以支持高可信回答
```

它的风险是：

```text
检索和阅读成本高
需要脱敏
需要过滤生成产物和无关文件
不适合直接全量进入 prompt
```

### 运行经验层：真实场景维护记录

运行经验层来自真实开发、问答、排错、运维和项目维护过程。

它可以理解为：

```text
各类具体场景下的维护记录和案例库。
```

典型目录可以是：

```text
knowledge/klonet_experience/
  faq.md
  common_errors.md
  ops_cases.md
  dev_cases.md
  review_cases.md
  accepted_solutions.md

knowledge/klonet_experience/cases/
  2026-06-xx-topology-progress-stuck.md
  2026-06-xx-worker-register-failed.md
```

它的来源可以包括：

```text
项目日志
真实问答
trace
测试失败记录
运维排错记录
代码 review 记录
验收差异记录
```

这一层的主要作用是复用经验。

例如用户问：

```text
worker 注册不上，怎么排查？
拓扑部署进度条卡住以前遇到过吗？
web terminal 连不上通常是什么原因？
```

运行经验层应该有很高优先级。

它的优点是：

```text
贴近真实问题
适合排错
能沉淀团队经验
能让 Agent 越用越懂 Klonet
```

它的风险是：

```text
可能依赖具体环境
可能只适用于某个版本或某台服务器
需要标注是否验证过
需要从个案中提炼可复用结论
```

### 不同任务下的查询优先级

四层知识的优先级不是固定的，而是由任务类型决定。

#### Mentor 概念解释

```text
人工知识层
运行经验层
机器索引层
原始证据层
```

原因：

```text
先给用户稳定、可读的解释。
如果问题涉及真实场景，再补充案例。
需要定位时再查索引。
事实不确定时回原始证据核对。
```

#### Coding 开发任务

```text
人工知识层
机器索引层
原始证据层
运行经验层
```

原因：

```text
先理解开发规范和业务流程。
再用机器索引定位源码。
修改前读取原始源码确认实现。
最后查经验层避免重复踩坑。
```

#### 报错排查和运维问题

```text
运行经验层
人工知识层
机器索引层
原始证据层
```

原因：

```text
真实排错经验最可能直接命中问题。
人工知识层提供系统化排查顺序。
机器索引层帮助定位相关代码。
原始证据层用于最终核对。
```

#### 事实核对

```text
机器索引层
原始证据层
人工知识层
运行经验层
```

原因：

```text
先定位具体文件、接口、函数。
再读取原始证据确认事实。
人工知识层只作为解释补充。
运行经验层只作为历史参考。
```

### 设计结论

四层知识可以这样概括：

```text
人工知识层：负责解释。
机器索引层：负责定位。
原始证据层：负责核对。
运行经验层：负责排错和复用真实案例。
```

因此，它们不是简单的可信度排序。

更准确的原则是：

```text
可信度取决于任务类型。
优先级取决于任务目标。
最终事实需要能追溯到原始证据层。
高频经验需要沉淀到运行经验层。
稳定结论需要上升到人工知识层。
代码定位需要交给机器索引层。
```

## 意图路由与知识检索的职责边界

### 路由和检索不是同一件事

可以把知识库理解成一座图书馆：

```text
路由：判断应该去哪个书架，以及这次要解决什么任务。
检索：在选定书架中找到最相关的具体页面和证据片段。
```

例如用户问：

```text
不是配置环境，我要启动 Klonet。
```

正确的路由结果应该表达：

```text
问题范围：Klonet
任务类型：操作指导
操作意图：启动已有平台
排除意图：首次安装环境
前置状态：环境已经准备完成
本轮是对上一轮理解的纠正
```

路由完成后，检索器才去启动 Runbook 中查找 Master、Celery、Web Terminal、Worker 和 Nginx 的具体启动命令。

### 当前实现为什么容易跑偏

当前系统采用：

```text
关键词路由 + BM25 关键词检索
```

关键词路由主要输出 `scope`、`task_type` 和 `domains`。当前 `runtime` 领域同时包含部署、启动、关闭和环境等词，`concept` 任务也无法区分“安装环境”和“启动平台”。因此下面两个问题可能得到相同路由：

```text
我怎么部署 Klonet？
环境已经装好了，我怎么启动 Klonet？
```

第二个问题中的“不是环境配置”还可能让 BM25 同时命中“环境”和“启动”。如果路由没有保存否定条件，环境部署文档反而可能得到更高分。

所以当前问题首先是走错知识范围，而不代表 BM25 完全不可用。

### 目标架构

建议逐步升级为：

```text
原始用户输入
  -> 少量确定性边界检查
  -> 大模型结构化意图解析
  -> Schema 与置信度校验
  -> 根据意图生成检索计划
  -> BM25 在候选知识范围内检索具体章节
  -> 证据组织
  -> 按任务结构生成回答
```

这里优先替换的是“关键词主路由”，不是立即替换 BM25。BM25 仍然负责在已经选定的文档范围内找到具体内容。后续只有真实 Eval 证明存在明显语义漏检时，才需要增加向量召回。

### 结构化意图

意图解析模型不应该输出一段自由文本，而应该返回受 Schema 约束的数据，例如：

```json
{
  "scope": "klonet",
  "task_type": "operation_guide",
  "operation": "platform_start",
  "target": "klonet_platform",
  "excluded_intents": ["environment_setup"],
  "prerequisites": ["environment_ready"],
  "is_correction": true,
  "confidence": 0.96
}
```

字段职责如下：

```text
scope：Klonet、通用技术或 mixed。
task_type：概念解释、操作指导、故障排查、源码定位或项目进度。
operation：环境安装、平台启动、停止、重启等具体动作。
target：动作针对的平台、服务或模块。
excluded_intents：用户明确否定的方向。
prerequisites：用户已经说明的前置状态。
is_correction：是否在纠正上一轮理解。
confidence：意图解析置信度。
```

这种结构把开放的自然语言收敛为有限、可校验的系统状态。不同说法，例如“把平台跑起来”“环境装好了接下来呢”和“不是安装，我要启动”，都可以归一化为 `platform_start`。

### 意图模块如何知道需要哪些文档

意图模型不应该在每个问题到来时读取全部 README，也不应该记住仓库中的物理路径。更稳定的方式是维护一个紧凑的知识目录，由文档 frontmatter 或构建索引自动生成。

知识目录可以包含：

```json
{
  "document_id": "ops.startup_shutdown",
  "path": "knowledge/klonet/ops/startup_shutdown.md",
  "domains": ["operations", "runtime"],
  "intent_tags": ["platform_start", "platform_stop", "platform_restart"],
  "task_types": ["operation_guide", "troubleshooting"],
  "priority": "P0",
  "status": "current_runbook"
}
```

环境部署文档则可以注册为：

```json
{
  "document_id": "ops.environment_setup",
  "path": "knowledge/klonet/ops/environment_setup.md",
  "domains": ["operations", "environment"],
  "intent_tags": ["environment_setup", "dependency_install"],
  "task_types": ["operation_guide"],
  "priority": "P0",
  "status": "current_runbook"
}
```

模型主要负责把用户语言解析为 `operation=platform_start`。系统再使用知识目录把意图映射到候选文档：

```text
platform_start
  -> ops.startup_shutdown
  -> 在该文档内检索 Master、Celery、Terminal、Worker 的启动命令

environment_setup
  -> ops.environment_setup
  -> 在该文档内检索依赖、基础容器、镜像仓库和环境验收
```

因此，文档定位依据不是一个总 README，而是统一、可生成、可校验的知识目录。新增知识文档时主要维护 frontmatter 或索引 metadata，不需要把所有文档名称硬编码进 Prompt。

当前知识文档已有 `domains`、`priority`、`status` 等 metadata，但还不足以稳定区分“环境安装”和“平台启动”。目标状态需要补充更细的 `intent_tags` 或等价字段。

### 为什么不会再次变成无限规则库

大模型负责理解开放的用户表达，代码只维护有限的业务意图和安全边界：

```text
大模型：理解用户究竟想做什么。
Schema：限制模型可以输出哪些结构化状态。
知识目录：声明每份文档适用于哪些意图。
检索器：在候选文档中找到具体证据。
硬规则：保护明确否定、安全权限和精确路径等确定性边界。
```

需要维护的是相对稳定的业务动作，例如环境安装、启动、停止和重启，而不是穷举“怎么开平台”“如何跑起来”等所有自然语言说法。

### 是否需要额外增加一次模型调用

不一定。当前 RAG 工具循环本来通常包含：

```text
第一次模型调用：判断是否调用知识检索工具。
第二次模型调用：读取工具证据并生成回答。
```

可以让第一次模型调用同时完成意图解析，并把结构化意图放入检索工具参数：

```text
第一次调用：理解意图 + 生成结构化检索请求。
工具执行：校验意图 + 选择候选文档 + BM25 检索章节。
第二次调用：根据可靠证据回答。
```

只有当模型输出不符合 Schema、置信度过低或用户问题本身存在关键歧义时，系统才应该降级或请求用户澄清。

### 设计结论

目标方案可以概括为：

```text
从“关键词决定去哪里找”升级为“大模型理解意图，系统决定可检索范围”。
从“在整个知识库碰关键词”升级为“先选对文档，再在文档内找具体证据”。
从“丢失否定和纠正条件”升级为“把约束保存为结构化状态”。
```

路由解决的是“找哪类知识”，检索解决的是“找到哪些具体内容”。意图解析提高前者的准确性，BM25 继续承担后者；两者职责清晰后，回答才不容易因为走错知识范围而跑偏。

## Mentor 回答质量提升的三道约束

这一版 Mentor 回答质量明显提升，不是因为单纯把 prompt 写长了，而是把“不确定的模型行为”拆成了三道可控制的系统约束：

```text
1. 意图约束：先约束模型要查什么。
2. 检索约束：再约束系统拿哪些证据。
3. 回答约束：最后约束模型怎么组织答案。
```

完整链路可以理解为：

```text
用户问题
  ↓
orchestrator 路由与预算控制
  ↓
Mentor 临时回答策略
  ↓
search_knowledge 工具调用，必须提交 intent
  ↓
knowledge/intent.py 校验、降级和保留否定条件
  ↓
query expansion + intent filter + BM25 排序
  ↓
RAG 证据文本
  ↓
按回答策略生成最终回答
```

### 第一道：意图约束

意图约束解决的是：

```text
这次到底应该查什么？
```

过去如果只把用户问题原文丢给检索器，系统容易被关键词带偏。例如用户说：

```text
不是配置环境，我要启动 Klonet。
```

如果系统只看关键词，可能同时命中“环境”“配置”“启动”，最后误查环境安装文档。

现在更合理的做法是让 `search_knowledge` 工具参数携带结构化 `intent`，例如：

```json
{
  "scope": "klonet",
  "task_type": "operation_guide",
  "operation": "platform_start",
  "excluded_intents": ["environment_setup"],
  "prerequisites": ["environment_ready"],
  "confidence": 0.96
}
```

这一步的核心不是让模型自由解释，而是让模型把自然语言压缩进有限字段。随后由 Python 代码校验这些字段：

```text
unknown enum 拒绝或降级。
confidence 过低时不盲信。
用户明确否定的方向进入 excluded_intents。
改写后的 query 不能覆盖原始问题中的否定条件。
```

所以第一道约束把“模型觉得用户想问什么”变成了“系统可检查的结构化状态”。

### 第二道：检索约束

检索约束解决的是：

```text
应该把哪些证据交给模型？
```

BM25 仍然负责关键词相关性排序，但它不应该独自决定答案质量。更稳的检索流程是：

```text
先根据 intent 限定候选范围。
再用 query expansion 扩展同义表达。
再用 excluded_intents 排除明确错误方向。
最后让 BM25 在正确范围内排序具体片段。
```

例如 `platform_start` 场景下，系统应该优先找启动 Runbook 中的阶段性步骤：

```text
Redis
Master
Gunicorn
Celery
Web Terminal
Worker
Nginx
启动后验证
```

同时过滤掉：

```text
环境安装
首次部署
停止平台
故障排查中不相关的失败分支
```

这说明检索质量不是只靠“分数最高的 chunk”，而是靠：

```text
意图标签
任务类型
操作类型
排除意图
层级权重
BM25 分数
```

共同决定候选证据。

### 第三道：回答约束

回答约束解决的是：

```text
模型拿到证据后，应该怎样回答？
```

同样的检索结果，如果没有回答策略，模型可能会：

```text
复述用户问题。
展示内部检索过程。
机械列出一堆来源路径。
把概念解释写成排查步骤。
把操作指导写成空泛建议。
证据不足时继续编造。
```

因此系统需要根据任务类型注入临时回答策略。例如：

```text
troubleshooting：先给排查顺序，再给验证点。
code_lookup：先给位置，再解释为什么是这些位置。
deployment/platform_start：按执行阶段组织命令和检查项。
concept：先给定义，再解释机制和边界。
```

这个策略最好由 `answer_policy.py` 生成，并由 `orchestrator.py` 在本轮 Mentor 回答中临时注入。临时注入的好处是：

```text
只影响当前回答。
不会污染长期 history。
可以在 search_knowledge 返回高置信 intent 后刷新策略。
```

回答策略的重点不是替模型写答案，而是限制答案形态：

```text
结论先行。
不要重复用户问题。
不要暴露内部检索报告。
不要无意义地建议“去看源码路径”。
证据不足时说明不确定，而不是编造 Klonet 架构。
```

### 三道约束的分工

可以把三道约束对应到三个风险点：

| 风险点 | 对应约束 | 作用 |
| :--- | :--- | :--- |
| 查错方向 | 意图约束 | 把用户问题结构化，保留否定条件和任务类型 |
| 拿错证据 | 检索约束 | 用 intent、metadata、BM25 共同筛选证据 |
| 答错形态 | 回答约束 | 按任务类型控制最终答案结构 |

一句话总结：

```text
意图约束决定“查什么”。
检索约束决定“拿什么证据”。
回答约束决定“怎么讲给用户”。
```

### 和 prompt 工程的区别

这三道约束不是普通 prompt 工程。

它们分别落在不同层级：

```text
工具 schema
  要求模型提交结构化 intent。

Python 校验
  检查 intent 是否可信，是否需要降级。

检索算法
  根据 intent 做 query expansion、过滤、加权和 BM25 排序。

临时 system message
  按任务类型控制本轮回答结构。
```

所以它不是“告诉模型你要回答好一点”，而是把回答质量拆成几个可验证、可调试、可替换的工程环节。

### 设计结论

Mentor 回答质量提升的核心原因是：

```text
系统不再只依赖模型自己理解问题、自己找证据、自己组织答案。
而是把理解、检索、表达分别加上结构化约束。
```

这和前面 Mentor/Coding 工具权限设计是一致的：

```text
Prompt 负责引导。
工具 schema 负责收敛动作。
Python 代码负责校验和执行。
检索层负责证据边界。
回答策略负责输出形态。
```

因此，好的 Agent 设计不是把所有规则塞进 prompt，而是把关键边界放到系统链路里。

## 有限意图 Schema 与规则兜底

这一节记录“问题理解”应该如何优化。它属于意图解析优化，但目标不是无限增加关键词规则，而是把用户问题解析成一张固定格式的意图表。

### 1. 什么是有限意图 Schema

有限意图 Schema 可以理解为：系统预先定义好一组字段和候选值，模型或解析器只能在这些字段里填写结果，不能随意发明新的分类。

例如用户问：

```text
启动 web-terminal 的时候报错 address already in use，为什么？
```

系统不应该只得到一句自然语言判断：

```text
用户大概是在问 Web Terminal 启动失败。
```

而应该得到结构化意图：

```json
{
  "scope": "klonet",
  "task_type": "troubleshooting",
  "operation": "platform_start",
  "target": "web_terminal",
  "symptom": "address_already_in_use",
  "excluded_intents": [],
  "prerequisites": [],
  "requires_retrieval": true,
  "confidence": 0.92
}
```

这里的“有限”很重要。因为如果不限制候选值，模型可能输出很多近义词：

```text
start_platform
startup
launch_klonet
run_klonet
boot_platform
```

这些词对人来说差不多，但对代码来说会变成很多兼容分支。因此系统应该统一成固定值，例如：

```json
"operation": "platform_start"
```

有限 Schema 的作用是把自然语言问题转成稳定、可验证、可路由的数据结构。

### 2. Schema 里可以有哪些字段

第一版不需要设计得很复杂，但至少应该覆盖以下信息：

```text
scope
  用户问题属于 Klonet、通用技术，还是 mixed。

task_type
  用户是在问概念、排障、源码定位、部署指导、凭据边界，还是项目进度。

operation
  如果是操作类问题，具体是环境安装、依赖安装、平台启动、停止、重启，还是验收检查。

target
  用户关注的对象，例如 master、worker、redis、web_terminal、nginx、frontend、topology、container。

symptom
  如果是故障排查，记录关键症状，例如 address_already_in_use、connection_refused、worker_unreachable。

excluded_intents
  用户明确排除的方向，例如“不是配置环境”“不需要 Klonet”。

prerequisites
  用户已经说明的前提，例如“已经跑完 docker_service.sh”“环境已经装好了”。

requires_retrieval
  是否需要查 Klonet 知识库。

confidence
  意图解析结果的置信度。
```

这些字段不是为了让模型“看起来更聪明”，而是为了让后面的路由、检索和回答结构都能有明确输入。

### 3. 为什么这不是无限加规则

无限加规则的写法是：

```python
if "启动" in query:
    operation = "platform_start"
if "部署" in query:
    operation = "environment_setup"
if "密码" in query:
    task_type = "credential_boundary"
if "web-terminal" in query and "address already in use" in query:
    symptom = "address_already_in_use"
```

这种方式的问题是：真实用户表达会不断变化，规则会越堆越多，最后很难维护。

有限 Schema 的思路不同：

```text
先定义系统能理解哪些字段和候选值。
再让模型或解析器把用户问题填成这张表。
规则只负责安全边界、低置信度兜底和检索增强。
```

也就是说，主路径是结构化意图解析，不是关键词 if/else。

### 4. 什么是规则兜底

规则兜底不是主路径，而是保险丝。它只在模型不可靠、问题有硬边界，或者检索需要增强时介入。

#### 4.1 安全兜底

如果用户问：

```text
虚拟机用户名和密码是什么？
```

即使模型没有正确识别，只要规则检测到“用户名、密码、凭据、token、真实 IP”等敏感方向，就应该强制进入：

```json
{
  "task_type": "credential_boundary",
  "requires_retrieval": false
}
```

这类问题不能交给模型自由发挥，因为一旦输出真实凭据就是安全事故。

#### 4.2 否定兜底

如果用户说：

```text
不需要 Klonet，我只是想问 Docker Compose。
```

即使模型因为看到“Klonet”这个词想查知识库，规则也应该拦住：

```json
{
  "scope": "general",
  "excluded_intents": ["klonet_rag"]
}
```

用户明确否定的方向不能被查询改写或后续工具调用覆盖。

#### 4.3 低置信度兜底

如果模型输出：

```json
{
  "task_type": "concept",
  "confidence": 0.42
}
```

说明系统并不确定用户到底要什么。此时可以采取保守策略：

```text
降低检索范围。
不要强行套某个专门回答结构。
必要时先问澄清问题。
```

例如“怎么部署 Klonet？”可能同时指：

```text
安装环境
启动已经安装好的平台
部署前端或后端服务
```

如果上下文不足，系统可以先问用户是在问哪一种，而不是直接生成一长串安装步骤。

#### 4.4 检索增强兜底

如果结构化意图是：

```json
{
  "task_type": "troubleshooting",
  "target": "web_terminal",
  "symptom": "address_already_in_use"
}
```

检索层可以把查询扩展成：

```text
web_terminal address already in use 端口占用 screen lsof ss
```

这不是在判断用户意图，而是在帮助检索找到更稳定的证据。

### 5. 推荐链路

整体流程应该是：

```text
用户问题
  ↓
LLM / 解析器填写有限意图 Schema
  ↓
规则兜底校验
  - 安全边界
  - 否定条件
  - 低置信度
  - 检索增强
  ↓
根据 Schema 路由到相关知识文档
  ↓
在文档内检索具体证据
  ↓
按回答结构输出
```

### 6. 设计结论

有限意图 Schema 的核心价值是让“问题理解”变成稳定的数据结构，而不是依赖模型在回答时临场发挥。

规则兜底的核心价值是保护系统边界，而不是替代意图解析。

一句话总结：

```text
有限意图 Schema 负责让模型填一张固定格式的意图表。
规则兜底负责在安全、否定、低置信度和检索增强场景下保护系统。
两者配合，才能避免无限堆关键词规则。
```

## 当前 Mentor 前置意图链路

### 1. 用户输入如何变成结构化 QueryIntent

当前优化后的 Mentor 链路里，`IntentAnalyzer` 会在正式回答和工具调用之前先调用一次 LLM。这个 LLM 调用不负责回答用户，而是只负责把自然语言问题解析成稳定 JSON，也就是 `QueryIntent`。

例如用户问：

```text
启动 web-terminal 的时候报 address already in use，为什么？
```

前置意图解析器应该输出类似：

```json
{
  "scope": "klonet",
  "task_type": "troubleshooting",
  "operation": "platform_start",
  "target": "web_terminal",
  "symptom": "address_already_in_use",
  "excluded_intents": [],
  "prerequisites": [],
  "requires_retrieval": true,
  "clarification_required": false,
  "clarification_question": "",
  "is_correction": false,
  "confidence": 0.92
}
```

如果用户问：

```text
我已经安装完环境了，怎么启动 Klonet？
```

解析结果应该更接近：

```json
{
  "scope": "klonet",
  "task_type": "deployment_guidance",
  "operation": "platform_start",
  "target": "klonet_platform",
  "symptom": "",
  "excluded_intents": ["environment_setup"],
  "prerequisites": ["environment_ready"],
  "requires_retrieval": true,
  "clarification_required": false,
  "clarification_question": "",
  "is_correction": false,
  "confidence": 0.9
}
```

如果用户明确排除 Klonet：

```text
不要 Klonet，我只想知道 Docker Compose 网络怎么配置
```

解析结果应该是：

```json
{
  "scope": "general",
  "task_type": "general",
  "operation": "unknown",
  "target": "docker_compose_network",
  "symptom": "",
  "excluded_intents": ["klonet"],
  "prerequisites": [],
  "requires_retrieval": false,
  "clarification_required": false,
  "clarification_question": "",
  "is_correction": false,
  "confidence": 0.95
}
```

因此，`QueryIntent` 不是“关键词命中结果”，而是把用户自然语言问题转成系统可消费的结构化意图表。

### 2. 为什么还有 LLM 前的确定性澄清

当前链路里有两层澄清：

```text
第一层：确定性安全 / 澄清边界，发生在 LLM 前。
第二层：LLM 意图解析后的 clarification_required 判断，发生在 LLM 后。
```

LLM 前的确定性澄清只处理很少数“系统已经能确定不能直接回答”的情况。它不是替代意图解析，而是作为安全边界和高精度歧义保护。

例如：

```text
怎么部署 Klonet？
```

在当前 Klonet 语境下，“部署”可能至少表示：

- 安装基础环境；
- 启动已经安装好的 Klonet 平台服务；
- 启动某个前端、后端或 Worker 组件。

如果系统直接进入检索和回答，很容易又把“启动平台”答成“安装环境”。所以这类问题会先追问：

```text
你说的“部署 Klonet”是指安装基础环境，还是启动已经安装好的 Klonet 平台服务？
```

这类问题称为“明显需要追问的问题”：不补充信息就会导致系统选择错误路径，而且系统不应该自行猜测。

但不是所有模糊问题都靠规则处理。比如：

```text
Klonet 起不来怎么办？
```

这个问题可能涉及 Redis、Gunicorn、Celery、端口、依赖、目录、权限等多种原因。它不适合靠固定规则判断，而应该交给前置 LLM 输出：

```json
{
  "task_type": "troubleshooting",
  "operation": "platform_start",
  "target": "klonet_platform",
  "symptom": "",
  "clarification_required": true,
  "clarification_question": "启动失败时的具体报错或失败服务是什么？",
  "confidence": 0.55
}
```

也就是说，规则只拦“高确定性、低覆盖面”的边界；普通意图理解仍然由 LLM 完成。

### 3. 当前 Mentor 链路

当前 Mentor 主链路可以理解为：

```text
user_input
  ↓
确定性安全 / 澄清边界
  ↓
IntentAnalyzer 前置解析 QueryIntent
  ↓
clarification_required 判断
  ↓
根据 intent 生成 route / answer_policy / tool visibility
  ↓
知识检索与最终回答
```

各层含义如下：

- 确定性安全 / 澄清边界：不用 LLM 也能确定必须先停下的问题，例如账号、密码、token、真实 IP，或者“部署 Klonet”这种高风险歧义。
- `IntentAnalyzer` 前置解析 `QueryIntent`：就是 LLM 意图解析。它把自然语言问题转成固定字段。
- `clarification_required` 判断：判断是否需要先问用户补充信息。这个判断既可以来自确定性规则，也可以来自 LLM 输出的结构化字段。
- 根据 `intent` 生成 `route / answer_policy / tool visibility`：
  - `route` 决定当前问题是 Klonet、general 还是 mixed，以及检索预算和工具边界；
  - `answer_policy` 决定回答结构，例如故障排查用“最可能原因、排查顺序、判断依据”；
  - `tool visibility` 决定本轮是否能看到 `search_knowledge`、`read_project_journal` 等工具。

### 4. 当前还没做到什么

当前已经从“关键词路由”升级成“结构化意图驱动检索”，但还没有做到非常完整的：

```text
intent → 文档目录 / README → 指定文档集合
```

现在更准确的链路是：

```text
intent
  → route / domains / operation
  → 检索查询增强 + 检索过滤 + 检索预算控制
  → 在统一知识索引里找具体 chunk
```

也就是说，当前系统会根据 `scope`、`task_type`、`operation`、`target`、`symptom` 等字段影响检索方向，但还没有一个显式的“文档目录路由表”告诉系统：

```text
platform_start → 只去 deployment/startup README 和 runbook 文档集合
web_terminal 故障 → 只去 web-terminal 故障文档集合
topology deploy → 只去 topology deployment 文档集合
```

下一步如果继续升级，可以为每组文档建立 README / manifest，描述该文档集合适合哪些 `task_type`、`operation`、`target` 和 `symptom`。这样系统就能先选文档集合，再在集合内部做 BM25 或向量检索，检索命中会更稳定。

## 前置意图解析的上下文与追问权限优化

### 1. 问题现象

在多轮对话中，用户可能使用“第一种”“上面那个”“你刚说的场景一”等指代词。如果前置意图解析器只看当前用户输入，就会把本来能由上下文消解的问题误判为不明确。

典型例子：

```text
Assistant: 场景一：你在浏览器里用 Klonet（普通用户）。
Assistant: 场景二：你要部署运行 Klonet（管理员/开发者）。
User: 是第一种，我怎么使用？
```

如果只看最后一句，“第一种”确实不完整；但结合上一轮回答，它明确指向“浏览器普通用户使用 Klonet”。因此不应该追问“第一种是什么”。

### 2. 根因

之前链路中，`IntentAnalyzer` 只接收当前 `user_input`：

```text
IntentAnalyzer.analyze(user_input)
```

同时，模型只要输出：

```json
{
  "clarification_required": true
}
```

系统就会直接中断主回答并追问。也就是说，前置意图解析器上下文不足，但拥有过大的追问权限。

### 3. 优化方式

现在分两层修复：

```text
recent_history
  → IntentAnalyzer.analyze(user_input, recent_history=...)
  → QueryIntent
  → ClarificationPolicy 二次校验
  → 决定是否真的追问
```

#### 3.1 IntentAnalyzer 接收短上下文

只传最近几轮 `user/assistant` 消息，不传完整历史，避免 token 膨胀和旧上下文污染。

构造给意图解析器的输入类似：

```text
最近对话上下文：
assistant: 场景一：你在浏览器里用 Klonet（普通用户）。
assistant: 场景二：你要部署运行 Klonet（管理员/开发者）。

当前用户输入：
是第一种，我怎么使用？

如果当前输入包含“第一种/第二种/场景一/场景二/上面那个/你刚说的/继续”等指代，必须结合最近对话解析，不得直接追问这些指代是什么意思。
```

#### 3.2 clarification_required 不再一票否决

模型仍然可以建议追问，但系统会先检查最近上下文是否能消解指代。

如果用户输入包含：

```text
第一种 / 第二种 / 场景一 / 场景二 / 上面那个 / 你说的 / 刚说的 / 继续
```

并且最近 assistant 消息中存在对应选项或场景定义，则忽略失忆式追问，继续进入主回答。

### 4. 设计边界

这次优化不是取消追问，而是把追问权限收回到策略层：

- 安全边界问题仍然可以直接拦截，例如账号、密码、token、真实 IP。
- 真正上下文不足的问题仍然可以追问。
- 能由最近上下文消解的指代问题，不应该追问“你说的是什么”。

### 5. 结论

前置意图解析器可以站在主链路前面，但不能只看当前一句，也不能单独决定是否中断回答。更稳的结构是：

```text
短上下文意图解析 + 代码层 ClarificationPolicy 二次校验
```

这样既保留前置意图解析的收益，又避免多轮对话中的“失忆式追问”。

## Intent Case RAG 的定位、收益与风险

### 1. 它确实是在意图解析前增加一层 RAG

方案 4 里的“前置意图 RAG 召回”可以理解为：

```text
用户最新输入 + 短历史上下文
  ↓
检索相似的意图样例 Case
  ↓
把命中的 Case 作为动态 few-shot 注入 IntentAnalyzer Prompt
  ↓
LLM 输出 intent + slots + decision
  ↓
进入 ClarificationPolicy / SemanticDecisionPlanner / 后续业务逻辑
```

所以它本质上是在意图解析模块之前加了一层 RAG，但这个 RAG 不是用来回答业务事实的，而是用来给意图解析器提供“相似对话如何理解”的参考样例。

它和普通知识 RAG 的区别是：

- 普通知识 RAG 检索的是事实、步骤、文档证据，用于最终回答。
- Intent Case RAG 检索的是对话样例、语义角色、意图、槽位和处理方式，用于帮助模型理解当前用户到底在问什么。

因此，Intent Case RAG 的输出不应该直接变成最终答案，而应该只影响意图理解、槽位抽取、上下文指代消解和是否追问。

### 2. 为什么它有用

Klonet Agent 的很多误判不是“缺少文档”，而是“用户话术太口语化、多轮上下文太省略、角色和机器边界不清”。例如：

```text
用户: 我电脑里需要下什么软件吗？
```

如果没有相似样例，模型可能把“电脑”理解成目标服务器，于是回答 Docker、Redis、MySQL、RabbitMQ。

如果 Intent Case RAG 命中类似样例：

```text
历史提问: 我想在自己笔记本上操作服务器
最新提问: 我电脑里需要下什么软件吗？
意图: local_tool_preparation
槽位: machine_role=operator_pc
处理方式: 回答 SSH、浏览器、VS Code、SFTP、数据库客户端等操作者电脑工具
```

模型就更容易把当前问题理解为“操作者个人电脑准备工具”，而不是“目标服务器安装依赖”。

它的预期收益主要有三类：

- 提升口语化表达、反问、情绪化说法、上下文省略场景下的意图识别稳定性。
- 在一次 LLM 调用中同时完成 intent、slots、semantic_frame、clarification decision，减少多次调用。
- 让多轮对话中的“B”“第一种”“上面那个”“这台电脑”更容易结合历史语境被正确消解。

### 3. 最大风险是 Case 锚定偏差

这个方案最大的风险确实是：如果召回的 case 不好，或者相似度只是表面相似，LLM 会被 few-shot 样例强行带偏，把当前用户问题往错误意图上靠。

典型风险包括：

- 用户问“部署平台”，命中了“首次环境部署”样例，模型忽略上下文里用户其实选了 B，错误进入环境安装流程。
- 用户说“电脑”，命中了“目标服务器依赖安装”样例，模型把个人电脑误判成服务器。
- 用户已经切换新话题，但历史拼接召回了旧意图样例，导致意图残留。
- Case 里带了过细的命令路径，模型把样例路径当成当前机器事实，生成错误命令。

所以 Intent Case RAG 不能被设计成“召回到哪个 case 就判成哪个 intent”。它只能提供参考，不应该拥有最终决策权。

### 4. 必须加的防护边界

为了避免样例锚定，Intent Case RAG 需要几条硬约束：

- Case 只作为 few-shot 参考，不作为事实证据；最终回答仍然必须从正式知识库、工具结果或当前上下文取证。
- 检索分数低、top case 互相冲突、或 case 与用户显式条件冲突时，不注入或降级为弱参考。
- Prompt 中明确要求：不得因为 case 相似就覆盖用户当前明确表达；当前用户输入和最近上下文优先级高于样例。
- Case 中的命令、路径、IP、用户名、机器名只能作为样例字段，不得直接迁移到当前答案。
- 最终 intent 仍需经过 `SemanticDecisionPlanner` 和 `ClarificationPolicy` 二次校验。
- `direct_answer` 类型 case 只能用于低风险、高确定性、短答案场景；部署、启动、故障排查等高风险流程不应直接绕过 LLM。
- 样例入库前要有人工审核和回归测试，避免错误 case 被批量放大。

### 5. 更稳的工程定位

Intent Case RAG 在系统里的合理定位是“意图解析增强器”，不是“意图分类器本体”：

```text
Intent Case RAG 负责提供相似理解范式。
IntentAnalyzer 负责结构化抽取。
SemanticDecisionPlanner 负责语义决策。
ClarificationPolicy 负责是否追问。
业务知识 RAG 负责最终回答证据。
```

这样做的好处是：样例能帮助模型理解复杂口语和多轮语境，但不会让某个命中 case 单独决定用户意图，更不会把样例里的历史命令、路径和机器环境误当成当前事实。

### 6. 当前落地结构

Intent Case RAG 先落地为一个“可向量化、可门控、可观测”的模块，而不是直接批量灌入大量 case：

```text
knowledge/intent_cases.py
  IntentCase
  IntentCaseRetriever
  build_intent_case_query

llm/embeddings.py
  EmbeddingClient

IntentAnalyzer
  build_intent_case_query(user_input, recent_history)
  → IntentCaseRetriever.search_for_prompt(...)
  → _append_intent_cases(...)
  → LLM structured intent parsing
```

`IntentCaseRetriever` 支持两种模式：

- `keyword`：没有 embedding provider 时的本地兜底，保证测试和开发环境不断链。
- `hybrid`：注入 `EmbeddingClient.embed_text` 后，使用 `semantic_score + keyword_score` 混合打分。

这里保留 keyword fallback 不是说生产上继续依赖关键词，而是为了让系统在没有 embedding 服务时仍然可运行。生产环境应注入真实 embedding provider，并通过 prompt 中的 `retrieval_mode: hybrid`、`keyword_score`、`semantic_score` 观测是否真的走了语义召回。

Prompt 注入前增加了 `search_for_prompt` 门控：

- 低分 case 不注入。
- 非 `intent_parse` 或显式 `block_prompt_injection` 的 case 不注入。
- top case 分数接近但 intent / semantic_frame 冲突时，不注入任何 case，避免把模型锚定到错误样例。

所以现在的设计不是：

```text
召回 case → 直接判定 intent
```

而是：

```text
召回 case 候选
  → 质量门控 / 冲突检测
  → 仅作为 few-shot 参考注入
  → LLM 结构化解析
  → SemanticDecisionPlanner / ClarificationPolicy 再决策
```

这个结构为后续生成高质量 case 做准备：case 库质量会影响意图解析质量，但单个 case 不应该拥有最终裁决权。

## 结构化意图不能替代原始输入和会话状态

### 1. intent 不是原始 prompt 的替代品

前置意图解析的目标是把用户输入转换成结构化字段，例如：

```json
{
  "task_type": "troubleshooting",
  "operation": "platform_start",
  "target": "web_terminal",
  "symptom": "address_already_in_use"
}
```

但结构化 intent 本质上是对原始问题的压缩。如果后续检索只使用这些字段，而不再携带用户原始问题，就会丢失用户原文里的细节、语气、限定条件和错误描述。

错误链路是：

```text
用户原始问题
  → intent 结构化输出
  → 只拿 intent 去检索
```

更稳的链路应该是：

```text
用户原始问题
  + intent
  + semantic_frame
  + slots
  + 必要的会话状态
  → QueryBuilder 构造检索 query
```

也就是说，结构化 intent 是检索增强信号，不是原始 query 的替代品。

例如用户问：

```text
我启动 web-terminal 的时候报 address already in use，怎么处理？
```

检索 query 不应该只剩：

```text
platform_start web_terminal address_already_in_use
```

而应该包含：

```text
原始问题：我启动 web-terminal 的时候报 address already in use，怎么处理？
任务类型：troubleshooting
操作阶段：platform_start
目标组件：web_terminal
故障现象：address_already_in_use
排除方向：environment_setup
```

这样既保留原始表达，又利用结构化字段收敛检索范围。

### 2. 短历史不是完整记忆

IntentAnalyzer 接收最近几轮对话，是为了处理“B”“第一种”“上面那个”“继续”等近距离指代。它不应该被理解为 Agent 只有两轮记忆。

如果系统只依赖最近一两轮原文，会出现明显失忆：

- 用户早前已经确认“服务器已安装好，只需要启动”，后面系统又追问首次安装还是启动。
- 用户早前说明自己是学习者、在个人电脑上操作，后面系统又把“电脑”理解成目标服务器。
- 用户切换过任务后，旧任务残留又污染新任务。

更稳的多轮上下文应拆成三层：

```text
短上下文：最近 N 轮原文，用于指代消解。
会话状态：当前任务的结构化状态，例如用户角色、机器角色、部署阶段、已确认槽位。
长期摘要：更早但仍有价值的信息压缩，例如用户环境、已完成步骤、明确排除项。
```

意图解析时输入的不是完整历史原文，也不是只有最近两轮，而应该是：

```text
latest_query
  + recent_history
  + session_state
  + long_term_summary
```

其中：

- `recent_history` 解决近距离指代。
- `session_state` 保存当前任务状态和已确认槽位。
- `long_term_summary` 保存更早但仍有用的信息。

### 3. 推荐的后续架构

后续更完整的链路应该是：

```text
用户输入
  ↓
ConversationStateManager
  - recent_history
  - session_state
  - long_term_summary
  - confirmed_slots
  ↓
Intent Case RAG
  - 用 latest_query + state + recent_history 召回相似 case
  ↓
IntentAnalyzer
  - 输出 intent / slots / semantic_frame / decision
  ↓
QueryBuilder
  - 原始 prompt
  - intent
  - semantic_frame
  - slots
  - session_state
  一起构造检索 query
  ↓
业务知识 RAG
  ↓
回答
  ↓
更新 ConversationStateManager
```

这里有两个核心原则：

```text
结构化 intent 不替代原始问题。
短历史不替代会话状态。
```

因此，下一步需要补两个模块：

- `QueryBuilder`：把原始 prompt、intent、semantic_frame、slots、session_state 合成检索 query，避免原文信息丢失。
- `ConversationStateManager`：维护跨多轮的结构化状态，避免 Agent 只依赖最近几轮原文。

## 三段链路里不是每一段都必须调用 LLM

当前前置意图架构容易被误解成“三个 LLM 调用”：

```text
1. 意图识别部分：喂短上下文，生成结构化意图。
2. 检索增强部分：喂短上下文 + 结构化意图，找到对应知识。
3. 回答问题部分：喂长上下文 + 检索增强后的 prompt，生成最终回答。
```

这个理解大方向对，但第 2 步需要修正：检索增强部分不一定是一次 LLM 调用。

更准确的链路是：

```text
1. IntentAnalyzer
   输入：当前用户输入 + 短上下文 + 少量 intent case
   输出：intent / slots / semantic_frame / clarification decision

2. QueryBuilder + KnowledgeRetriever
   输入：原始用户输入 + intent + semantic_frame + slots + 必要短上下文/session_state
   输出：检索 query + 知识证据

3. Answer LLM
   输入：当前工作上下文 + 当前用户问题 + 本轮 intent/scope 约束 + 检索证据
   输出：最终自然语言回答
```

其中第 2 步优先设计成确定性模块，而不是额外 LLM 调用：

- `QueryBuilder` 用代码把原始问题、结构化意图、语义角色、槽位和会话状态拼成聚焦检索 query。
- `KnowledgeRetriever` 或 `search_knowledge` 根据 query 和 intent metadata 检索业务知识。
- 只有未来需要复杂 query rewrite、rerank 或语义重排时，才考虑给第 2 步加 LLM。

这样可以避免系统变成：

```text
意图解析 LLM
  → 检索改写 LLM
  → 回答 LLM
```

导致成本、延迟和不确定性同时上升。

推荐理解是：

```text
IntentAnalyzer 负责“理解用户要什么”。
QueryBuilder + RAG 负责“找到该用什么证据”。
Answer LLM 负责“组织成用户能看懂的回答”。
```

也就是说，当前系统里明确需要的 LLM 调用主要是第 1 步和第 3 步；第 2 步应先做成可测试、可解释的确定性检索准备层。

### 1. LLM 和 embedding 能力的分工

如果按最终理想版来设计，三段链路可以理解为：

```text
1. IntentAnalyzer：理解用户要什么
   - LLM：把当前输入 + 短上下文解析成 intent / slots / semantic_frame。
   - embedding：从 intent case 库召回相似对话样例，辅助 LLM 理解。

2. QueryBuilder + RAG：找到该用什么证据
   - QueryBuilder：通常不用 LLM，用代码把原始问题 + intent + slots + state 拼成检索 query。
   - embedding：用于知识库向量召回。
   - keyword / BM25 / exact match：用于脚本名、接口名、报错、路径等精确匹配。
   - 输出：知识证据。

3. Answer LLM：组织成用户能看懂的回答
   - LLM：读取用户问题、当前上下文、intent 约束、检索证据，生成最终回答。
```

简化后是：

```text
IntentAnalyzer = LLM + intent case embedding retrieval
QueryBuilder + RAG = deterministic query builder + knowledge hybrid retrieval
Answer = LLM
```

也就是：

```text
第一段：LLM + 向量召回
第二段：向量 / 关键词 / BM25 / 精确匹配的混合检索，不一定需要 LLM
第三段：LLM
```

这里要区分两个概念：

```text
LLM：负责理解、推理、生成文本。
Embedding model：负责把文本变成向量，用于相似度检索。
```

两者都属于模型能力，但职责不同。Intent Case RAG 和 Knowledge RAG 可以共用同一个 embedding provider，但应该分开建索引、分开设阈值、分开做门控：

```text
同一个 embedding provider
  → intent_case_index：检索“相似问法应该如何理解”
  → knowledge_index：检索“回答依据在哪里”
```

因此，系统最终需要的是：

```text
两个 LLM 使用点：
  1. IntentAnalyzer
  2. Answer LLM

一个可复用 embedding 能力：
  1. 给 intent case retrieval 用
  2. 给 knowledge retrieval 用
```

## 源码机器索引与真实源码读取

源码机器索引可以理解为“源码地图”，而不是真实源码本身。

它通常记录：

- 哪些源码文件存在；
- 文件路径在哪里；
- 文件大概属于哪个业务领域；
- 有哪些函数、类、路由、配置项、Celery 任务；
- 它们位于第几行；
- 有时还会包含简短 summary；
- 路由对应哪个实现文件；
- 配置项来自哪个配置文件。

例如 `knowledge/klonet_index/symbols.jsonl` 中的一条记录可能表示：

```json
{
  "kind": "function",
  "symbol": "post_worker_init",
  "module": "mains.gun",
  "path": "mains/gun.py",
  "line": 46,
  "end_line": 57,
  "summary": "gunicorn master 初始化时的回调函数"
}
```

这表示源码中存在一个叫 `post_worker_init` 的函数，它在 `mains/gun.py` 的第 46 到 57 行。

但是这条索引并没有保存完整函数源码。它只是告诉 Agent：

```text
如果你要理解 post_worker_init，应该去 mains/gun.py 这个文件附近找。
```

所以，机器索引负责“定位”，真实源码负责“确认”。

### 如果 workspace 里没有真实源码

如果只有机器索引，而没有真实源码，Agent 仍然可以知道：

- 可能有哪些文件；
- 函数叫什么；
- 路由在哪个文件；
- 配置项可能在哪；
- 调用入口大概在哪里。

但它不能可靠回答源码细节，例如：

- 函数内部具体做了什么；
- 参数如何校验；
- 异常分支如何处理；
- 返回值结构是什么；
- 某个报错到底由哪一行触发；
- 当前代码与文档是否不一致。

因此，如果 workspace 或可访问源码目录中没有真实源码，Agent 最多只能“根据源码索引推断”，不能做到源码级确认。

### 如果同时有机器索引和真实源码

这是最理想的状态。

推荐链路是：

```text
用户问题
  ↓
机器索引定位候选文件 / 函数 / 路由 / 配置项
  ↓
按需读取真实源码
  ↓
用源码内容确认答案
  ↓
最终回答用户
```

例如用户问：

```text
/file/dload/ 这个接口是干什么的？
```

系统可以先查 `routes.jsonl`，找到：

```json
{
  "route": "/file/dload/",
  "implementation": "vemu_uestc/webserver/api/file_load/master_download.py",
  "methods": ["POST"]
}
```

然后再读取真实源码：

```text
klonet_knowledge/02_vemu_uestc_code/vemu_uestc/webserver/api/file_load/master_download.py
```

最终回答时，应该以真实源码为准，而不是只根据索引中的 route 或 summary 推断。

### 机器索引如何影响源码读取

机器索引不是直接“让 Agent 理解源码”，而是影响 Agent 的搜索方向。

没有机器索引时，Agent 可能只能在大量源码中盲目搜索关键词。

有机器索引后，Agent 可以先缩小范围：

```text
用户问 web-terminal address already in use
  ↓
机器索引定位 web_terminal_main.py、启动脚本、端口配置项
  ↓
读取相关源码和配置
  ↓
判断到底是端口占用、screen 旧进程未退出，还是配置端口重复
```

所以正确理解是：

```text
机器索引 = 找到应该读哪里
真实源码 = 证明答案是否正确
```

后续 Mentor 的源码理解能力应该采用混合方案：

```text
代码类问题
  → 查机器索引定位候选文件
  → search_code / read_source_file 读取真实源码
  → 结合知识库文档组织回答
```

不能只依赖机器索引，也不应该把机器索引当成源码本身。

### 知识库优先还是源码优先

源码理解链路不应该固定成“永远先读源码”或“永远先查知识库”，而应该由问题意图决定证据优先级。

如果用户问的是流程类、概念类、部署指导类问题，通常应该先查知识库，再用源码校验关键事实。

例如：

```text
Klonet 怎么启动？
部署环境和启动平台有什么区别？
为什么 Worker 不需要启动 Celery？
```

这类问题的推荐链路是：

```text
用户问题
  ↓
意图解析
  ↓
知识库检索，找到流程文档、部署文档或经验总结
  ↓
如果涉及具体命令、脚本、端口、源码路径
  ↓
读取源码或脚本确认关键事实
  ↓
最终回答
```

这样回答会更完整，因为知识库通常已经整理了背景、流程、前提和注意事项。源码在这里主要负责校验命令、脚本、路径、配置项等事实。

如果用户问的是代码类、接口类、配置类、报错类问题，最终标准通常应该是源码。

例如：

```text
web_terminal_main.py 是怎么启动的？
/file/dload/ 这个接口在哪里？
为什么启动时报 address already in use？
Celery 任务是在哪注册的？
```

这类问题的推荐链路是：

```text
用户问题
  ↓
意图解析
  ↓
源码机器索引定位候选文件 / 函数 / 路由 / 配置项
  ↓
按需 grep / read_source_file 读取真实源码
  ↓
必要时补充知识库背景
  ↓
最终回答
```

原因是代码问题的事实标准是当前源码，而不是文档。文档可能过期、漏写或简化，源码才代表系统真实行为。

因此更准确的原则是：

```text
流程类问题：知识库优先，源码校验。
代码类问题：源码优先，知识库补充。
混合类问题：知识库定位流程，源码确认关键事实。
```

这里的重点不是谁一定先于谁，而是不同问题需要不同的证据优先级。意图解析模块应该输出任务类型和证据需求，后续检索层再根据 evidence priority 决定先查知识库还是先读源码。

### 按需 grep 源码与源码全文数据库 / 向量库的区别

按需 grep 源码和把源码全文存入数据库或向量库，解决的是不同层次的问题。

按需 grep 源码的本质是：

```text
直接在真实源码文件里搜索关键词、函数名、路由、报错文本或配置项。
```

例如用户问：

```text
address already in use 是哪里来的？
```

系统可以搜索：

```text
address already in use
web_terminal
port
socket
```

然后读取命中的真实源码文件。

按需 grep 的优点是：

- 读的是当前真实源码；
- 不容易因为索引过期而答错；
- 对函数名、接口路由、报错文本、配置项、脚本名非常准确；
- 实现简单；
- 适合作为代码事实校验手段。

它的缺点是：

- 对自然语言问题不够强；
- 用户描述的是现象时，源码里可能没有完全相同的词；
- 大代码库中多轮 grep 可能较慢；
- 需要 Agent 能把用户问题转成合适的搜索词。

源码全文数据库或向量库的本质是：

```text
提前把源码切成 chunk，建立全文索引或向量索引，然后用关键词或语义相似度召回相关代码片段。
```

例如用户问：

```text
网页终端连接失败是哪里处理的？
```

即使源码中没有“连接失败”这个原词，向量检索也可能召回：

- websocket handler；
- terminal session；
- web_terminal_main.py；
- socket 连接逻辑；
- terminal 端口配置。

源码全文数据库 / 向量库的优点是：

- 对自然语言问题更友好；
- 可以做语义搜索；
- 大规模代码库检索更快；
- 适合用户不知道准确关键词、只描述现象的场景。

它的缺点是：

- 索引可能过期；
- 需要定期重建；
- 向量召回可能找出“相似但不正确”的代码；
- 最终仍然需要回到真实源码确认；
- 实现和维护成本更高。

因此，两者不应该简单二选一。更合理的架构是：

```text
第一层：源码机器索引
用途：快速知道有哪些文件、函数、路由、配置项。

第二层：按需 grep / read_source_file
用途：读取真实源码，确认事实。

第三层：源码全文数据库 / 向量库，可选
用途：自然语言语义召回，解决用户不知道关键词的问题。
```

最终原则是：

```text
源码索引告诉 Agent 去哪里找。
grep / read_source_file 让 Agent 看真实代码。
向量库帮助 Agent 在不知道关键词时也能找到候选代码。
最终答案，尤其是代码问题，仍然应该以真实源码为准。
```

### 当前读代码工具的实现原理与调用链路

当前第一版源码理解能力采用的是“LLM 生成搜索词 + grep 定位 + 读取真实源码”的两层工具链。

它不是向量检索，也不是让大模型直接扫描硬盘。大模型本身不能直接读取本地文件，它只能根据问题决定是否调用工具、调用哪个工具、传什么参数。真正访问源码目录的是工具层。

当前源码工具固定面向规范源码树：

```text
klonet_knowledge/02_vemu_uestc_code
```

也就是说，源码工具不是读取任意 workspace 文件，而是专门读取 Klonet 源码证据目录。

#### 1. LLM 先判断是否需要源码证据

例如用户问：

```text
web_terminal 是怎么启动的？
```

Mentor 会先判断：

```text
这是 Klonet 域内问题
这是启动 / 源码解释问题
需要真实源码证据
```

因此它应该优先调用源码工具，而不是只根据知识库文档回答。

#### 2. LLM 生成 search_code 调用

模型会把用户问题转换成更适合源码检索的关键词。

例如用户原话可能是：

```text
网页终端为什么启动失败？
```

但源码里未必有“网页终端”这个词。模型需要推导出更可能命中的源码关键词，例如：

```text
web_terminal
terminal
create_web_terminal_app
web_terminal_port
```

然后生成工具调用：

```json
{
  "name": "search_code",
  "arguments": {
    "query": "web_terminal",
    "max_results": 5
  }
}
```

所以当前第一层仍然是关键词 grep，但关键词不一定等于用户原话，而是由 LLM 根据意图生成。

#### 3. ToolExecutor 检查权限并分发工具

模型生成 tool call 后，不会直接执行。

系统会先经过 `ToolExecutor`：

```text
tool_name = search_code
tool_args = {"query": "web_terminal", "max_results": 5}
```

执行前会检查当前 Agent 是否允许调用这个工具。

Mentor 现在允许调用：

```text
search_code
read_source_file
list_source_files
```

如果工具不在白名单里，会被拒绝。

#### 4. search_code 在源码目录中 grep

`search_code` 的核心行为是：

```text
在 klonet_knowledge/02_vemu_uestc_code 下搜索 query
返回 文件路径:行号:命中行
```

优先使用 `rg`，也就是 ripgrep。

如果当前运行环境没有 `rg`，则降级为 Python 逐文件扫描。

简化理解：

```text
有 rg：
  用 rg 做快速关键词搜索

没有 rg：
  用 Python 遍历源码文件并逐行匹配
```

工具会跳过明显不适合检索的目录，例如：

```text
.git
.history
__pycache__
node_modules
logs
tmp
test-results
output
```

并限制返回条数，避免一次把大量源码塞进上下文。

#### 5. search_code 返回候选位置

例如搜索：

```json
{
  "query": "web_terminal",
  "max_results": 5
}
```

可能返回：

```text
mains/web_terminal_main.py:1: from vemu_uestc.webserver.app_factory import create_web_terminal_app
mains/web_terminal_main.py:6: app = create_web_terminal_app()
vemu_config/config.py:479: web_terminal_port = 5005
```

这一步只说明：

```text
这些文件和 web_terminal 相关。
```

它还不能完整解释源码逻辑，因为 grep 只返回命中行。

#### 6. LLM 再调用 read_source_file 读取真实源码

拿到候选文件后，Mentor 应该继续读取关键文件。

例如：

```json
{
  "name": "read_source_file",
  "arguments": {
    "path": "mains/web_terminal_main.py",
    "start_line": 1,
    "end_line": 30
  }
}
```

`read_source_file` 会读取真实源码片段，并带上文件路径和行号：

```text
源码文件：mains/web_terminal_main.py（第 1-30 行）
mains/web_terminal_main.py:1: ...
mains/web_terminal_main.py:3: if __name__ == "__main__":
mains/web_terminal_main.py:4: ...
```

这一步才是“看代码具体写了什么”。

#### 7. LLM 根据源码证据组织回答

工具结果会进入模型上下文。

之后模型根据真实源码回答：

```text
web_terminal 的入口在 mains/web_terminal_main.py。
它通过 create_web_terminal_app() 创建 app。
运行主程序时引入 gevent.pywsgi 和 WebSocketHandler。
端口配置需要继续查看 vemu_config/config.py 中的 web_terminal_port。
```

如果还需要端口、配置或调用链，就继续搜索并读取：

```text
search_code("web_terminal_port")
read_source_file("vemu_config/config.py", start_line=470, end_line=520)
```

#### 8. 当前完整调用链路

```text
用户提出代码 / 接口 / 配置 / 启动脚本 / 报错问题
  ↓
Mentor 判断需要源码证据
  ↓
LLM 生成 search_code 工具调用
  ↓
ToolExecutor 检查工具权限
  ↓
search_code 在 klonet_knowledge/02_vemu_uestc_code 中 grep
  ↓
返回 文件路径:行号:命中行
  ↓
LLM 选择关键候选文件
  ↓
LLM 生成 read_source_file 工具调用
  ↓
read_source_file 读取真实源码片段
  ↓
必要时再结合 search_knowledge 查知识库背景
  ↓
最终回答以源码事实为准，知识库用于解释背景
```

核心分工是：

```text
LLM：理解问题、生成搜索词、选择候选文件、解释源码。
search_code：用关键词 grep 找到源码位置。
read_source_file：读取真实源码内容。
search_knowledge：补充流程、背景、经验和文档证据。
```

因此，当前第一版源码理解能力的优势是简单、准确、可验证；短板是第一层仍依赖关键词命中。如果用户只描述现象、不知道源码关键词，效果取决于 LLM 能否生成合适的搜索词。后续向量库或语义源码索引可以补足这一点。

## 短链路 RAG 与 Claude Code 式 tool loop 的区别

当前 Mentor 更接近“证据增强问答架构”，Claude Code 更接近“任务执行型 Agent 工具循环”。

### 当前 Mentor：短链路 RAG 回答

大多数 Mentor 回答链路是：

```text
用户问题
  ↓
前置意图解析
  ↓
检索知识库 / 搜索源码 / 读取源码
  ↓
把证据放回上下文
  ↓
生成最终回答
```

它的核心目标是：

```text
快速找到回答所需证据，然后组织成用户能理解的答案。
```

所以它通常不会天然产生很长的中间过程。即使调用工具，也多是有限的短链路：

```text
search_knowledge
  → final answer
```

或者：

```text
search_code
  → read_source_file
  → final answer
```

这种架构适合：

- 概念解释；
- 部署指导；
- 源码定位；
- 常见故障排查；
- 项目进度说明；
- 有明确证据来源的问答。

它的风险是：如果证据链路太短，系统可能在证据不足时过早回答。因此需要额外设计“思考摘要 / 证据摘要”，让用户知道本轮到底判断了什么、查了什么、依据是什么。

### Claude Code 类架构：多轮 tool loop

Claude Code 更像典型的：

```text
Thought / Action / Observation loop
```

简化成中文链路是：

```text
用户目标
  ↓
模型判断下一步
  ↓
调用工具
  ↓
观察工具结果
  ↓
模型重新判断
  ↓
继续调用工具
  ↓
直到任务完成或被阻塞
```

例如开发任务中可能是：

```text
读文件
  ↓
发现入口不对
  ↓
grep 其他文件
  ↓
找到调用链
  ↓
修改代码
  ↓
运行测试
  ↓
测试失败
  ↓
继续修复
  ↓
最终总结
```

这种架构天然会产生更多中间状态，因为它解决的是“完成任务”，而不是单次回答。

它适合：

- 代码修改；
- 调试；
- 复杂环境排查；
- 部署执行；
- 需要根据工具结果不断改变策略的任务。

它的风险是：成本更高、状态更复杂、容易跑偏，也需要更强的权限边界和停止条件。

### 两种架构的核心区别

| 维度 | 当前 Mentor 短链路 RAG | Claude Code 式 tool loop |
|---|---|---|
| 目标 | 回答问题 | 完成任务 |
| 工具调用 | 少量、证据检索型 | 多轮、行动观察型 |
| 循环深度 | 通常 1 到 2 步 | 可持续多步 |
| 中间状态 | 意图、检索、证据 | 计划、行动、观察、修正 |
| 用户可见过程 | 需要额外生成摘要 | 更自然产生过程 |
| 适合场景 | 知识问答、解释、轻量排查 | 改代码、调试、部署、复杂诊断 |
| 主要风险 | 证据不足时过早回答 | 工具循环跑太远、成本高、权限复杂 |

### Mentor 的合理演进方向

不建议把 Mentor 全量改成 Claude Code 式重型循环。

更合理的方式是按任务复杂度分层：

```text
简单事实问题
  → 不检索或一次检索

源码 / 配置 / 报错问题
  → search_code → read_source_file → 可选 search_knowledge

复杂故障排查
  → 小型 tool loop：源码、知识库、日志、配置之间做 2 到 4 次验证

开发 / 修复 / 部署执行
  → 切换到 Coding Agent 或执行型 Agent loop
```

因此，Mentor 默认输出的“思维链”不应该是完整内部推理，而应该是用户可见的执行轨迹摘要：

```text
思考摘要：
1. 问题类型是什么。
2. 需要什么证据。
3. 调用了哪些工具。
4. 命中了哪些关键证据。
5. 最终回答以什么为准。
```

这更符合 Mentor 当前短链路 RAG 架构，也能满足学习场景中“看懂 Agent 为什么这样答”的需求。

## 阶段一遗留问题：意图链路还没有完全统一

这里的“意图链路没有完全统一”，不是说系统没有意图解析，而是说现在有多个模块都在参与理解用户问题，但它们还没有完全收束成一个唯一的控制中枢。

当前链路大致是：

```text
用户输入 + 最近历史
  ↓
IntentAnalyzer：LLM 前置意图解析
  ↓
SemanticFrame / SemanticDecisionPlanner：细化语义、阶段和澄清判断
  ↓
route_from_intent / route_query：生成 QueryRoute
  ↓
clarification.py：决定是否追问
  ↓
QueryBuilder：拼检索 query
  ↓
collection_router：按 manifest 选文档集合
  ↓
retriever：在集合内检索 chunk
  ↓
answer_policy：决定回答结构
```

这些模块各自都合理，但现在的问题是：不同模块都有一定“解释用户意图”的权力。如果它们判断一致，系统表现就好；如果不一致，就会出现跑偏。

例如：

```text
用户：NH
```

它本质是低信息 / 不可理解输入。但模型可能误判成：

```json
{
  "task_type": "deployment_guidance",
  "target": "klonet_platform",
  "clarification_required": true
}
```

如果 clarification 层直接信任这个结果，就会错误追问：

```text
你是想首次安装 Klonet 环境，还是启动已经安装好的平台服务？
```

这说明“模型意图输出”和“澄清策略”之间还缺少统一的可信边界。后续已经通过低信息输入保护做了局部修复，但从架构上看，仍然应该继续收束。

再例如：

```text
用户：继续
```

它本质是上下文续接，不是新问题。当前系统通过 orchestrator 里的 `_resume_state_for()` 恢复上一轮状态来避免串台。这个方案有效，但也说明“继续”还没有完全被统一建模进 QueryIntent / TurnIntent，而是作为编排层特殊逻辑存在。

理想状态应该是：

```text
用户输入 + 最近历史
  ↓
统一生成 TurnIntent
  - scope
  - task_type
  - operation
  - target
  - symptom
  - user_role
  - machine_role
  - phase
  - context_ref
  - excluded_meanings
  - confidence
  - clarification_type
  ↓
统一决策
  - 是否追问
  - 用哪种追问
  - 是否继续上一轮
  - 选哪些 collection
  - 是否必须读源码
  - 用什么回答结构
  ↓
检索 / 源码 / 回答
```

也就是说，后续目标不是继续堆更多零散规则，而是让一个统一的本轮意图对象决定澄清、路由、检索、源码工具和回答策略。

可以这样理解：

```text
当前状态：
LLM 意图解析 + 语义规则 + 关键词路由 + 澄清规则 + 上下文恢复
共同决定下一步

目标状态：
统一 TurnIntent
决定澄清、路由、检索、源码工具和回答策略
```

阶段一后续优化重点之一，就是把这些分散的判断逐步收束到统一 TurnIntent，减少不同模块之间互相覆盖、互相纠偏导致的跑偏问题。
