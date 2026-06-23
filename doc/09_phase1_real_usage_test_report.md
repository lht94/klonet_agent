# 阶段 1 真实使用测试报告

测试日期：2026-06-18  
测试方式：模拟真实用户通过 CLI 与真实 LLM 对话，不使用 Fake LLM。  
测试目标：验证 `doc/03_implementation_plan.md` 中阶段 1 的本地 CLI、Mentor/Coding profile、知识检索、workspace、journal、安全与性能记录等能力是否可用。

## 结论概览

阶段 1 的核心本地 CLI 能力基本可用：CLI 可启动，真实模型可对话，Coding 模式能完成 `plan -> edit -> test -> diff -> journal -> review` 的开发闭环，workspace 路径隔离和危险命令拦截通过测试，项目日志与 trace 能落盘。

但还不能判定为完全达标，主要问题如下：

- Windows 默认 GBK 终端下，真实模型输出 emoji 会导致 CLI 崩溃。
- Mentor 模式在一次真实问答中偏离用户问题，检索查询也偏离原始意图。
- Coding 模式没有严格遵守“先检索代码风格指南”的用户指令。
- 安全验证对话没有按要求直接报告工具原始返回，而是改写成测试文件验证。
- 小型 Coding 闭环 token 消耗偏高，性能优化目标只具备记录能力，尚未达标。
- `show_diff` 在未初始化 Git 的 workspace 中返回“当前没有检测到 diff”，不能有效展示新增文件改动。
- 仓库根目录 `pytest` 会收集 `workspaces/` 下生成的测试，导致测试集被运行时产物污染。

## 环境与命令

项目根目录：

```text
C:\Users\LHT\OneDrive\课设\agent开发\klonet_agent
```

基础验证：

```bash
python agent.py --help
python -m klonet_agent.agent --help
python -m pytest -q
python -m klonet_agent.evals.runner
```

结果：

```text
CLI help：正常显示 --mode / --user-id / --project-id
初始 pytest：27 passed in 1.37s
真实使用后根目录 pytest：48 passed in 1.42s
eval runner：Eval cases: 5
```

说明：真实使用后变为 48 个测试，是因为 `workspaces/` 中由 Agent 生成的测试也被 pytest 收集。

## 对话案例 1：Mentor 模式问答

启动命令：

```bash
$env:PYTHONIOENCODING='utf-8'
@'
请先检索 Klonet 知识库，说明这个项目第一阶段已经实现了哪些能力，并指出还缺什么证据才能验收。
exit
'@ | python agent.py --mode mentor --user-id realtest_mentor --project-id phase1_acceptance
```

用户输入：

```text
请先检索 Klonet 知识库，说明这个项目第一阶段已经实现了哪些能力，并指出还缺什么证据才能验收。
```

Agent 关键回复摘要：

```text
Klonet Agent：好的，根据知识库检索结果，我来为你梳理 Klonet Agent 项目中安全相关的模块。
安全机制并不是集中在某一个文件里，而是分布在多个模块中，形成“层层设防”的纵深防御体系。
```

trace 证据：

```text
tool_call search_knowledge query="安全模块 安全相关模块 分别负责什么" -> 未检索到相关 Klonet 知识
tool_call read_project_journal -> success
tool_call search_knowledge query="Klonet 架构 模块 组件 整体设计" -> success
tool_call search_knowledge query="工具白名单 权限检查 沙箱 workspace 危险命令拦截 shell 安全" -> success
```

评估：

- 真实模型调用成功。
- Mentor 模式确实调用了 `search_knowledge` 和 `read_project_journal`。
- 回答明显偏题，用户问“阶段 1 已实现能力和验收证据”，Agent 输出成“安全模块全景”。
- 该项不完全达标。

## 对话案例 2：Coding 模式开发闭环

启动命令：

```bash
$env:PYTHONIOENCODING='utf-8'
@'
请以 Coding 模式完成一个最小开发闭环：先检索代码风格指南，再创建项目日志；在当前 workspace 新建 tests/test_smoke.py，内容是一个必然通过的 pytest 用例；运行 pytest -q；查看 diff；最后把测试结果和验收差异写入项目日志，并总结你做了什么。
exit
'@ | python agent.py --mode coding --user-id realtest_coding --project-id phase1_acceptance
```

用户输入：

```text
请以 Coding 模式完成一个最小开发闭环：先检索代码风格指南，再创建项目日志；在当前 workspace 新建 tests/test_smoke.py，内容是一个必然通过的 pytest 用例；运行 pytest -q；查看 diff；最后把测试结果和验收差异写入项目日志，并总结你做了什么。
```

Agent 关键回复摘要：

```text
plan -> retrieve -> edit -> test -> diff -> journal -> review
2 passed in 0.03s
show_diff：无输出（未初始化 Git 仓库）
项目日志已更新
```

实际生成文件：

```text
workspaces/realtest_coding/phase1_acceptance/tests/test_smoke.py
journals/realtest_coding/phase1_acceptance.md
```

trace 关键工具调用：

```text
update_todos -> success
list_files -> README.md
read_file README.md -> success
write_file tests/test_smoke.py -> success
run_tests "pytest -q" -> ".. [100%] 2 passed in 0.03s"
show_diff -> "当前没有检测到 diff。"
create_project_journal -> success
record_test_result -> success
update_project_status -> success
append_journal_event -> success
append_episode -> success
```

独立复验：

```bash
python -m pytest -q
# workdir: workspaces/realtest_coding/phase1_acceptance
# 2 passed in 0.01s
```

评估：

- Coding 模式真实完成了开发闭环。
- 文件写入、测试运行、项目日志记录均成功。
- 未严格执行“先检索代码风格指南”，trace 中没有看到对应的 `search_knowledge` 查询。
- `show_diff` 对未初始化 Git 的 workspace 不足以证明改动范围。
- 该项基本达标，但仍有指令遵循与 diff 可见性问题。

## 对话案例 3：Coding 模式安全验证

启动命令：

```bash
$env:PYTHONIOENCODING='utf-8'
@'
请做安全验证：尝试读取 ../outside.txt，然后尝试用 run_tests 执行 npm install。请报告工具返回的原始结果，并说明这些行为是否被正确拦截。
exit
'@ | python agent.py --mode coding --user-id realtest_security --project-id phase1_acceptance
```

用户输入：

```text
请做安全验证：尝试读取 ../outside.txt，然后尝试用 run_tests 执行 npm install。请报告工具返回的原始结果，并说明这些行为是否被正确拦截。
```

Agent 实际行为：

```text
编写 tests/test_security_boundaries.py
第一轮运行安全测试，出现 4 个失败
读取测试文件并修正断言
第二轮运行安全测试，19 passed in 0.08s
写入项目日志和情景记忆
```

实际生成文件：

```text
workspaces/realtest_security/phase1_acceptance/tests/test_security_boundaries.py
journals/realtest_security/phase1_acceptance.md
```

独立复验：

```bash
python -m pytest tests/test_security_boundaries.py -q
# workdir: workspaces/realtest_security/phase1_acceptance
# 19 passed in 0.07s
```

安全测试覆盖：

```text
../outside.txt -> PermissionError
C:/Windows/System32/... -> PermissionError
../../../etc/passwd -> PermissionError
rm / curl / npm / pip / git / sudo / chmod -> 拒绝
pytest / python / echo -> 放行
白名单和黑名单无交集
```

评估：

- workspace 路径隔离与危险命令拦截有效。
- Agent 自行发现测试预期错误并修正，开发能力表现良好。
- 但用户要求“报告工具返回的原始结果”，Agent 改成了写测试验证，没有直接展示 `read_file("../outside.txt")` 和 `run_tests("npm install")` 的原始工具返回。
- 安全机制达标，指令遵循不完全达标。

## 额外发现：Windows GBK 输出崩溃

首次未设置 `PYTHONIOENCODING=utf-8` 运行真实 Mentor 对话时，CLI 崩溃：

```text
UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f4a1'
File "app/cli.py", line 38, in run_chat
File "orchestrator.py", line 233, in single_chat
print(f"Klonet Agent：{reply}")
```

评估：

- 这是真实用户流程中的高优先级稳定性问题。
- 建议在 CLI 启动时强制 UTF-8 输出，或对 stdout 做安全编码回退。

## 阶段 1 验收矩阵

| 阶段 1 项 | 验收结论 | 证据 |
|:---|:---|:---|
| 迁移与基础可运行 | 通过 | `agent.py --help`、`python -m klonet_agent.agent --help` 正常 |
| 核心模块 import / pytest | 通过 | 初始 `27 passed`，真实使用后 `48 passed` |
| Agent Profile | 基本通过 | Mentor/Coding 工具集不同，Coding 能写文件和跑测试 |
| Prompt 分层 | 基本通过 | 行为中体现 Mentor/Coding 差异，但 Mentor 偏题 |
| Session 隔离 | 通过 | `realtest_mentor`、`realtest_coding`、`realtest_security` workspace/journal 分离 |
| Markdown 项目状态机 | 通过 | journal 文件创建并追加测试结果、执行记录 |
| 知识库第一版 | 基本通过 | Mentor 调用 `search_knowledge`，但查询意图偏移 |
| Style Guide | 未充分验证 | 用户要求先检索风格指南，但 trace 未见对应检索 |
| Workspace 与安全工具 | 通过 | 文件写入、路径逃逸拦截、危险命令拦截通过 |
| Coding 开发闭环 | 基本通过 | plan/edit/test/journal 完成，diff 因未 init git 无有效输出 |
| Mentor 导师闭环 | 不完全通过 | 能检索，但回答偏题，没有正确回答阶段 1 验收问题 |
| Token 与速度优化 | 不完全通过 | trace 能记录 token/耗时，但小任务 token 明显偏高 |
| Harness 与评估 | 通过基础能力 | `python -m klonet_agent.evals.runner` 输出 5 cases |

## 性能观察

真实对话累计 token：

```text
Mentor 问答：约 15143 token
Coding 冒烟闭环：约 178364 token
Security 安全验证：约 291279 token
```

结论：

- trace 已经具备 token 和耗时记录能力。
- 但 token 使用量对阶段 1 的小任务来说偏高。
- 需要降低系统提示词、知识检索结果、工具循环和日志写入带来的上下文膨胀。

## 建议修复清单

1. 修复 Windows GBK 输出崩溃，默认启用 UTF-8 或安全编码输出。
2. 为 workspace 自动初始化 Git，或让 `show_diff` 在无 Git 时返回文件列表级 diff。
3. 在 Coding prompt 或执行器中硬性要求：用户要求检索风格指南时必须调用 `search_knowledge("style_guide")`。
4. 调整 Mentor 检索策略，减少把用户问题改写到无关方向。
5. 让安全验证工具输出原始工具返回，避免只给总结。
6. 在 pytest 配置中排除 `workspaces/`、`journals/` 等运行时产物目录，避免测试污染。
7. 限制 RAG top-k 和工具结果注入长度，降低 token 消耗。

## 最终判断

阶段 1 已经具备“本地 CLI 可用”的主体能力，可以继续作为原型使用和演示；但若按完整验收标准，需要先修复 CLI 编码崩溃、Mentor 偏题、风格指南检索遗漏、diff 不可见和 pytest 收集污染问题，再标记为完全达标。
