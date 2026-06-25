# Mentor Answer Structure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Mentor 增加按任务类型动态注入的回答结构和长度策略，同时消除默认机械追加下一步的行为。

**Architecture:** 新建纯函数模块 `answer_policy.py`，把现有 Router 的 `task_type` 和原始输入转换成当前轮系统消息。`AgentOrchestrator` 将该消息与现有范围消息一起注入工具循环，并在回答结束后移除；静态 Prompt 只保留跨任务通用规则。

**Tech Stack:** Python 3、dataclasses/纯函数、pytest、现有 AgentOrchestrator。

---

### Task 1: 回答策略纯函数

**Files:**
- Create: `answer_policy.py`
- Create: `tests/test_answer_policy.py`

- [ ] **Step 1: 写失败测试，定义策略模块公开接口**

```python
from klonet_agent.answer_policy import build_answer_policy


def test_troubleshooting_policy_uses_diagnostic_structure():
    text = build_answer_policy("troubleshooting", "拓扑部署卡住怎么排查")
    assert "最可能原因、排查顺序、判断依据" in text
    assert "500 至 1000 字" in text


def test_deployment_development_policy_uses_deployment_structure():
    text = build_answer_policy("development", "Klonet 应该怎么部署")
    assert "推荐方案、当前前提、执行步骤、验证方式" in text


def test_unknown_policy_falls_back_safely():
    text = build_answer_policy("unknown", "解释一下")
    assert "第一段直接给出结论" in text
```

- [ ] **Step 2: 运行测试并确认因模块缺失失败**

Run: `python -m pytest tests/test_answer_policy.py -q`

Expected: FAIL，错误为 `ModuleNotFoundError: klonet_agent.answer_policy`。

- [ ] **Step 3: 实现最小策略映射**

```python
def build_answer_policy(task_type: str, user_input: str) -> str:
    structure, length = _select_policy(task_type, user_input)
    return "\n".join([
        "【本轮回答策略】",
        f"- 回答结构：{structure}",
        f"- 建议长度：{length}",
        "- 第一段直接给出结论，只解释理解结论所必需的原因。",
        "- 不重复用户问题，不汇报内部检索过程。",
        "- 不机械追加学习建议、源码路径或下一步。",
        "- 没有可靠证据时说明不确定，不生成 Klonet 架构推测。",
    ])
```

策略表覆盖 `concept`、`troubleshooting`、`code_lookup`、`development`、`project_progress`、`general` 和未知值；部署与简单事实使用局部关键词识别。

- [ ] **Step 4: 运行策略测试并确认通过**

Run: `python -m pytest tests/test_answer_policy.py -q`

Expected: PASS。

### Task 2: Prompt 和 Profile 消除冲突规则

**Files:**
- Modify: `prompts.py`
- Modify: `agents/profile.py`
- Modify: `tests/test_prompt_style.py`

- [ ] **Step 1: 写失败测试约束通用规则和默认 workflow**

```python
def test_mentor_prompt_requires_direct_concise_evidence_based_answers():
    from klonet_agent.agents import get_profile
    from klonet_agent.prompts import MENTOR_PROMPT

    assert "第一段直接给出结论" in MENTOR_PROMPT
    assert "不机械追加学习建议、源码路径或下一步" in MENTOR_PROMPT
    assert "不生成 Klonet 架构推测" in MENTOR_PROMPT
    assert "suggest next step" not in get_profile("mentor").default_workflow
```

- [ ] **Step 2: 运行测试并确认旧 Prompt 导致失败**

Run: `python -m pytest tests/test_prompt_style.py -q`

Expected: FAIL，缺少新的回答规则或 workflow 仍含 `suggest next step`。

- [ ] **Step 3: 最小修改 Prompt 和 workflow**

将五条通用回答规则写入 `MENTOR_PROMPT`，删除强制追加来源路径、下一步学习建议和知识沉淀建议的要求；将 Mentor workflow 改为 `route -> retrieve if needed -> answer directly`。

- [ ] **Step 4: 运行 Prompt 测试并确认通过**

Run: `python -m pytest tests/test_prompt_style.py -q`

Expected: PASS。

### Task 3: Orchestrator 当前轮策略注入

**Files:**
- Modify: `orchestrator.py`
- Modify: `tests/test_orchestrator_controls.py`

- [ ] **Step 1: 写失败测试验证注入和清理**

在已有假 LLM/Executor 测试夹具上增加断言：第一次 LLM 调用的 system messages 包含 `【本轮回答策略】` 和当前任务结构；`single_chat()` 返回的 history 不再包含该消息。

```python
policy_prompts = [
    message["content"]
    for message in llm.calls[0]["messages"]
    if message["role"] == "system" and "本轮回答策略" in message["content"]
]
assert policy_prompts
assert "最可能原因、排查顺序、判断依据" in policy_prompts[0]
assert not any("本轮回答策略" in message.get("content", "") for message in history)
```

- [ ] **Step 2: 运行目标测试并确认因策略未注入失败**

Run: `python -m pytest tests/test_orchestrator_controls.py -q`

Expected: FAIL，`policy_prompts` 为空。

- [ ] **Step 3: 实现策略注入和身份清理**

在 `single_chat()` 完成路由后调用 `build_answer_policy(route.task_type, user_input)`，作为 system message 加入 history。结束时使用与范围消息相同的对象身份过滤方式，同时移除范围消息与策略消息。

- [ ] **Step 4: 运行 Orchestrator 测试并确认通过**

Run: `python -m pytest tests/test_orchestrator_controls.py -q`

Expected: PASS。

### Task 4: 回归验证

**Files:**
- Test: `tests/test_answer_policy.py`
- Test: `tests/test_prompt_style.py`
- Test: `tests/test_orchestrator_controls.py`
- Test: `tests/`

- [ ] **Step 1: 运行回答结构相关测试**

Run: `python -m pytest tests/test_answer_policy.py tests/test_prompt_style.py tests/test_orchestrator_controls.py -q`

Expected: PASS。

- [ ] **Step 2: 运行完整测试套件**

Run: `python -m pytest -q`

Expected: PASS；若出现与本次改动无关的既有失败，记录具体测试和失败原因。

- [ ] **Step 3: 检查 diff 范围**

Run: `git diff --check` 和 `git status --short`

Expected: 无空白错误；仅包含计划内文件以及用户原有的未提交文件。
