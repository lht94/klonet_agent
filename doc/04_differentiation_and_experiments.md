# 差异化价值与实验设计

## 为什么有 Claude Code 还要做 Klonet Agent

Claude Code 是通用 coding agent，目标是帮助用户在任意项目中完成软件工程任务。

Klonet Agent 是专用教学协作 agent，目标是帮助同学在固定 Klonet 项目中学习、开发、维护和沉淀知识。

二者目标函数不同。

### Claude Code 更擅长

- 任意项目的通用代码理解。
- 通用代码编辑。
- 通用测试和调试。
- 面向个人开发者的自动化协作。

### Klonet Agent 更擅长

- 固定 Klonet 项目的知识问答。
- 固定规范下的代码生成。
- 面向同学的教学解释。
- 面向老师的项目过程记录。
- 长期沉淀团队经验。
- 复用 Klonet 专属上下文，减少重复理解成本。

## 核心差异化点

### 1. 记忆系统更有针对性

Claude Code 的记忆通常围绕当前项目和当前开发者。

Klonet Agent 的记忆包括：

- 用户偏好。
- 项目进度。
- 项目日志。
- 历史踩坑。
- 团队规范。
- 老师验收关注点。
- Klonet 架构知识。

也就是说，它的记忆不是简单服务一个用户，而是逐步变成团队知识资产。

### 2. 项目范围固定，token 理论上更少

Klonet Agent 面向固定项目，因此可以：

- 预先索引 Klonet 文档和源码。
- 缓存常用规范。
- 使用固定开发模板。
- 只检索当前任务相关模块。
- 避免每次从零理解陌生项目。

这可能减少：

- prompt token。
- 文件读取 token。
- 反复解释项目结构的 token。
- 工具调用次数。

### 3. 常见任务更快

Klonet 中的常见任务可以模板化，例如：

- 新增功能。
- 修复固定类型报错。
- 写测试。
- 更新项目日志。
- 检查验收差异。
- 解释固定模块。

通用 Agent 需要先理解项目习惯，而 Klonet Agent 可以直接套用项目内规范。

### 4. 教学和管理价值

通用 coding agent 主要关注完成任务。

Klonet Agent 还关注：

- 同学有没有理解。
- 同学卡在哪里。
- 开发过程是否记录。
- 功能和预期是否有差异。
- 老师能否查看进度。
- 知识是否沉淀给后来的同学。

这是专用教学协作 Agent 的核心价值。

## 实验设计

后续需要用实验验证以下假设。

### 假设 1：Klonet Agent token 更少

对比对象：

1. 通用 Claude Code / Codex。
2. 无 RAG 的 Klonet Agent。
3. 有 RAG 的 Klonet Agent。
4. 有 RAG + 项目日志的 Klonet Agent。

指标：

- 总 token 数。
- 输入 token 数。
- 输出 token 数。
- 工具返回内容 token 估计。

### 假设 2：Klonet Agent 完成任务更快

指标：

- 总耗时。
- 工具调用次数。
- 文件读取次数。
- 修改轮数。
- 测试失败到修复的轮数。

### 假设 3：Klonet Agent 质量更稳定

指标：

- 测试通过率。
- 人工 review 问题数。
- 是否符合 Klonet 风格。
- 是否更新项目日志。
- 是否说明验收差异。

### 假设 4：Mentor Agent 答疑更可靠

指标：

- 是否引用证据。
- 是否命中正确源码/文档。
- 是否承认不确定。
- 是否给出可执行排查步骤。
- 同学主观评分。

## Eval Case 设计

### Mentor Cases

用于测试导师问答。

示例：

- Klonet 某模块是做什么的？
- 某个报错应该怎么排查？
- 项目日志应该记录什么？
- 新同学应该如何学习某个功能？

每个 case 包含：

- `id`
- `question`
- `expected_behavior`
- `needs_code_change`
- `acceptance`

### Coding Cases

用于测试开发能力。

示例：

- 新增一个小功能。
- 修复一个测试失败。
- 为已有模块补充日志记录。
- 根据规范重构某段代码。

每个 case 包含：

- `id`
- `task`
- `expected_behavior`
- `needs_code_change`
- `acceptance`

### Error Cases

用于测试报错排查。

示例：

- `ModuleNotFoundError`
- 测试失败。
- 配置文件缺失。
- workspace 路径错误。

每个 case 包含：

- `id`
- `error`
- `expected_behavior`
- `acceptance`

## 实验记录需要保存什么

每次实验建议保存：

- 输入任务。
- 使用的 agent 类型。
- 使用的 prompt 版本。
- 检索结果。
- 工具调用序列。
- token 使用。
- 耗时。
- 最终答案。
- diff。
- 测试结果。
- journal 是否更新。
- 人工评分。

## 论文或答辩中可以强调的点

可以这样表述本项目与 Claude Code 的区别：

> Claude Code 解决的是任意项目上的通用软件工程自动化；Klonet Agent 解决的是固定领域项目中的教学、规范化开发、知识传承与过程可审计。前者强调通用能力，后者强调领域约束、组织记忆和教学闭环。

这句话可以作为项目差异化的核心论点。
