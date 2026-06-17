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
