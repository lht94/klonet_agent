# Prompt、Context 与 Harness 工程设计

## 为什么需要这些工程设计

Agent 的效果不只取决于模型本身，还取决于：

- 给模型什么角色和规则。
- 给模型什么上下文。
- 给模型什么工具。
- 如何记录和评估运行过程。

因此本项目需要同时设计：

- Prompt 工程
- Context 工程
- Harness 工程

## Prompt 工程

Prompt 工程负责告诉模型：

- 它是谁。
- 当前是什么模式。
- 可以做什么。
- 不可以做什么。
- 应该遵循什么风格。
- 遇到多步骤任务怎么处理。

### 当前分层

```text
CORE_SYSTEM_PROMPT
SAFETY_PROMPT
MENTOR_PROMPT
CODING_PROMPT
STYLE_PROMPT
TASK_PROMPT
```

### 设计原则

1. 分层，而不是一个巨大 prompt。
2. 通用规则和模式规则分开。
3. 安全规则始终注入。
4. Mentor 和 Coding 使用不同模式 prompt。
5. 风格 prompt 独立维护，方便后续调整代码风格。

### Mentor Prompt 重点

- 回答 Klonet 问题前优先检索知识库。
- 默认不修改代码。
- 解释要适合同学学习。
- 有证据就给来源。
- 无证据就说明不确定。

### Coding Prompt 重点

- 先计划，再执行。
- 写代码前检索规范和相似实现。
- 文件操作限制在 workspace 内。
- 修改后必须测试。
- 完成后记录 journal。
- 最后做轻量 review。

## Context 工程

Context 工程负责决定每轮模型调用时给模型看什么。

如果上下文太少，模型会瞎猜；如果上下文太多，会浪费 token，也可能干扰模型。

### 推荐上下文优先级

1. 安全规则。
2. 当前 Agent Profile。
3. 当前用户和项目。
4. 当前任务和 todo。
5. RAG 检索证据。
6. 相关项目日志。
7. 最近对话。
8. 长期记忆。
9. 工具说明。

### Mentor 模式上下文

Mentor 更需要：

- Klonet 文档。
- 源码解释。
- 历史踩坑。
- 项目日志。
- 报错上下文。

Mentor 不需要：

- 大量无关文件内容。
- 完整 diff。
- 修改代码工具输出。

### Coding 模式上下文

Coding 更需要：

- 当前需求。
- todo。
- 相关源码。
- 相似实现。
- 代码规范。
- 测试结果。
- diff。
- 项目日志摘要。

Coding 不需要：

- 大量闲聊历史。
- 与当前任务无关的长期记忆。

### Token 优化策略

- RAG 只返回 top-k。
- 工具输出设置 `max_chars`。
- 长文件只读取相关片段。
- 历史对话压缩。
- 项目日志按章节摘要。
- 常用规范用短 prompt 表达，详细内容按需检索。

## Harness 工程

Harness 工程负责 Agent 运行时外壳和评估。

它不是模型本身，而是让 Agent 可控、可复盘、可评估的系统。

### 需要管理的内容

- `user_id`
- `project_id`
- `mode`
- `workspace_path`
- `journal_path`
- `todos`
- 工具调用记录
- token 使用
- 测试结果
- diff
- 最终回答

### 当前已有雏形

- `AgentSession`
- `ToolExecutor`
- `ProjectJournal`
- `KnowledgeBase`
- `WorkspaceSandbox`
- `evals/*.jsonl`
- `tests/*.py`

### 后续需要补充

- `TraceLogger`
- token 统计
- eval runner
- case 回放
- 自动评分
- 实验结果表格

## 三者关系

```text
Prompt 工程：告诉模型应该怎么想
Context 工程：决定模型能看到什么
Harness 工程：控制模型能做什么，并记录做得怎么样
```

对 Klonet Agent 来说，三者都需要。

原因：

- 只有 prompt，模型可能仍然拿不到 Klonet 证据。
- 只有 RAG，模型可能不知道教学和安全边界。
- 只有工具，无法证明效果是否比通用 agent 好。

因此本项目应该把三者都作为架构设计的一部分。
