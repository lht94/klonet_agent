# Public README Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将根目录 README 重构为面向项目老师和技术面试官的公开仓库首页，同时保留可查阅的技术演进信息。

**Architecture:** README 采用双层阅读结构：前半部分是可快速浏览的项目叙事，后半部分用主题化章节和折叠区块承载实现细节。所有能力描述以源码、测试、配置或仓库文档为依据，快速开始只使用公开、安全、可复制的示例。

**Tech Stack:** Markdown、Mermaid、PowerShell、Python、pytest

---

### Task 1: 建立 README 事实清单

**Files:**
- Read: `README.md`
- Read: `agent.py`
- Read: `requirements.txt`
- Read: `docs/DEVELOPMENT_GUIDELINES.md`
- Read: `doc/00_project_overview.md`
- Read: `doc/02_architecture_design.md`
- Read: `evals/summary.md`
- Read: `evals/context_management_summary.md`

- [ ] **Step 1: 核对公开入口与模式**

运行 `python -m klonet_agent.agent --help`。预期帮助文本列出 `mentor`、`coding`、`ops` 模式，以及 `--user-id`、`--project-id` 等公开参数。

- [ ] **Step 2: 核对模块与文档入口**

运行：

```powershell
rg -n "class |def |--mode|Mentor|Coding|Ops|RAG|ContextCompiler|Postgres" agent.py agents context knowledge memory runtime tools tracing doc docs evals -g "*.py" -g "*.md"
```

预期：能为多角色、检索、上下文、记忆、治理、工具与追踪描述找到对应实现或文档证据。

- [ ] **Step 3: 记录工作区边界**

运行 `git status --short`，识别用户已有未提交文件。后续只修改 `README.md` 和本计划文档，不覆盖其他内容。

### Task 2: 重构公开仓库首页

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 重写首屏与导航**

README 开头采用以下信息顺序：

```markdown
# Klonet Agent

面向 Klonet 教学、开发与运维场景的多角色智能协作 Agent。

项目简介 · 核心能力 · 系统架构 · 快速开始 · 测试与评估 · 技术演进
```

首屏解释项目面向的具体场景、三种 Agent 角色，以及相较通用聊天助手的差异；不得使用无法证明的“行业领先”“生产级”等表述。

- [ ] **Step 2: 编写亮点与角色边界**

用紧凑列表描述六项已实现能力：Mentor / Coding / Ops 多角色协作、Klonet 本地知识库与混合 RAG、ContextCompiler、Markdown / PostgreSQL + pgvector 记忆路径、受控 workspace 与运行治理、trace 与回归评测。分别写清三种模式的职责、典型任务和修改权限。

- [ ] **Step 3: 添加系统架构图**

使用 Mermaid `flowchart` 表示用户输入进入模式/Profile 与 Orchestrator，随后连接 ContextCompiler、RAG、Memory、Tools、Journal、Runtime Governance，最终调用 LLM Provider 并写入 Trace。图中只出现仓库已有组件。

- [ ] **Step 4: 重写快速开始**

快速开始包含克隆、创建虚拟环境、安装依赖、复制环境变量、启动三种模式和运行测试。命令采用仓库相对路径：

```bash
git clone <your-repository-url>
cd klonet_agent
python -m venv .venv
python -m pip install -r requirements.txt
python -m klonet_agent.agent --mode mentor --user-id demo --project-id demo
python -m pytest -q
```

环境变量只展示 `CHAT_LLM_API_KEY=<your-api-key>` 等占位值，并解释 OpenAI-compatible endpoint 的配置方式。

- [ ] **Step 5: 整理工程信息**

新增或重写项目结构、技术栈、测试与评估、文档索引、当前限制、贡献说明和许可证状态。许可证章节明确仓库尚未提供许可证，不能暗示允许任意复制或再分发。

- [ ] **Step 6: 保留并整理技术档案**

把原 README 中仍有效的 ContextCompiler、供应商路由、RAG、记忆、治理、Ubuntu 部署等细节移动到“技术实现与演进记录”。长配置和历史说明用 `<details>` / `<summary>` 包裹；删除重复段落、旧的本机绝对路径和已被当前架构替代的首屏叙述。

### Task 3: 验证 README

**Files:**
- Verify: `README.md`

- [ ] **Step 1: 检查 Markdown 基本结构**

运行 `rg -n "^# |^## |^### |<details>|</details>" README.md`。预期只有一个一级标题，折叠标签成对出现，标题层级连续。

- [ ] **Step 2: 检查本地链接**

运行以下只读脚本：

```powershell
@'
import pathlib, re
text = pathlib.Path("README.md").read_text(encoding="utf-8")
missing = []
for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
    path = target.split("#", 1)[0]
    if path and "://" not in path and not pathlib.Path(path).exists():
        missing.append(target)
print("missing:", missing)
raise SystemExit(bool(missing))
'@ | python -
```

预期输出 `missing: []`，退出码为 0。

- [ ] **Step 3: 检查敏感信息和本机路径**

运行：

```powershell
rg -n "C:\\Users|BEGIN (RSA|OPENSSH|PRIVATE) KEY|vemu[0-9]" README.md
```

预期无匹配；所有密钥字段均为显式占位符。

- [ ] **Step 4: 验证公开命令**

运行 `python -m klonet_agent.agent --help` 和 `python -m pytest -q`。预期 CLI 正常退出，完整测试套件通过；若因外部服务缺失而跳过测试，记录实际结果。

- [ ] **Step 5: 检查差异范围**

运行 `git diff --check -- README.md`、`git diff --stat -- README.md` 和 `git status --short`。预期 README 无空白错误，用户原有文件保持不变。

- [ ] **Step 6: 提交 README**

```powershell
git add -- README.md docs/superpowers/plans/2026-10-12-public-readme-redesign.md
git commit -m "docs: redesign README for public repository"
```

预期提交只包含 README 和实施计划，不包含用户已有未提交文件。
