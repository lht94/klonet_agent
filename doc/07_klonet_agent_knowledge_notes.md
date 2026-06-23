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
