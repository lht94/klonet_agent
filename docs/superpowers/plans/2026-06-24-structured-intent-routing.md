# Structured Intent Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用模型在首次知识检索工具调用中提交的结构化意图控制候选知识文档，使“环境安装”和“平台启动”等运行意图不再依赖关键词主路由。

**Architecture:** 保留现有关键词 Router 作为明确否定、安全边界和兼容兜底；新增受 Schema 约束的 `QueryIntent`，由 `search_knowledge` 工具参数承载。知识文档通过 `intent_tags` frontmatter 注册适用意图，Retriever 先按意图筛选文档，再执行现有 BM25；Orchestrator 在工具调用后使用已校验意图更新当前轮回答策略，不新增模型调用。

**Tech Stack:** Python 3、dataclasses、JSON Schema function tools、YAML frontmatter、BM25、pytest。

---

### Task 1: 结构化意图模型

**Files:**
- Create: `knowledge/intent.py`
- Modify: `knowledge/models.py`
- Test: `tests/test_intent_routing.py`

- [ ] **Step 1: 写失败测试定义意图解析接口**

```python
def test_query_intent_preserves_correction_and_exclusions():
    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping({
        "scope": "klonet",
        "task_type": "operation_guide",
        "operation": "platform_start",
        "excluded_intents": ["environment_setup"],
        "prerequisites": ["environment_ready"],
        "is_correction": True,
        "confidence": 0.96,
    })

    assert intent.operation == "platform_start"
    assert intent.excluded_intents == ("environment_setup",)
    assert intent.is_correction is True
```

- [ ] **Step 2: 运行测试确认因模块缺失失败**

Run: `python -m pytest tests/test_intent_routing.py -q`

Expected: FAIL with `ModuleNotFoundError: klonet_agent.knowledge.intent`。

- [ ] **Step 3: 实现有限枚举、校验与安全降级**

`QueryIntent.from_mapping()` 只接受允许的 scope、task_type 和 operation；非法值降级为 `unknown`，confidence 限制在 0 到 1，数组字段清洗为空字符串之外的 tuple。允许的运行意图至少包含 `environment_setup/platform_start/platform_stop/platform_restart/unknown`。

- [ ] **Step 4: 运行意图模型测试**

Run: `python -m pytest tests/test_intent_routing.py -q`

Expected: PASS。

### Task 2: 知识目录 metadata 与意图过滤

**Files:**
- Modify: `knowledge/indexer.py`
- Modify: `knowledge/models.py`
- Modify: `knowledge/retriever.py`
- Modify: `knowledge/klonet/ops/environment_setup.md`
- Modify: `knowledge/klonet/ops/startup_shutdown.md`
- Test: `tests/test_intent_routing.py`

- [ ] **Step 1: 写失败测试证明启动意图排除环境文档**

```python
def test_platform_start_intent_filters_environment_setup(tmp_path):
    rows = [
        _row("environment_setup.md", ["environment_setup"], "Klonet 环境安装"),
        _row("startup_shutdown.md", ["platform_start"], "Klonet 启动命令"),
    ]
    outcome = KnowledgeRetriever(index_file=_write(rows)).search_request(
        SearchRequest(query="Klonet 启动", intent="platform_start", top_k=3)
    )
    assert [item.path for item in outcome.results] == ["startup_shutdown.md"]
```

- [ ] **Step 2: 运行测试确认 SearchRequest 尚不支持 intent**

Run: `python -m pytest tests/test_intent_routing.py -q`

Expected: FAIL because `SearchRequest` has no `intent` argument。

- [ ] **Step 3: 实现 metadata 贯通和过滤**

`KnowledgeChunk`、索引 JSONL、`RetrievedChunk` 增加 `intent_tags`；frontmatter 中字符串或数组统一为 tuple/list。`SearchRequest` 增加 `intent` 和 `excluded_intents`。当 request.intent 不是 `unknown` 时，只允许包含该 tag 的文档；同时排除与 excluded_intents 相交的文档。

- [ ] **Step 4: 为两个 Runbook 注册意图**

```yaml
# environment_setup.md
intent_tags: environment_setup, dependency_install

# startup_shutdown.md
intent_tags: platform_start, platform_stop, platform_restart
```

- [ ] **Step 5: 运行索引与检索测试**

Run: `python -m pytest tests/test_intent_routing.py tests/test_retrieval_architecture.py tests/test_knowledge_pipeline.py -q`

Expected: PASS。

### Task 3: search_knowledge 结构化意图工具参数

**Files:**
- Modify: `tools/registry.py`
- Modify: `tools/executor.py`
- Modify: `knowledge/rag.py`
- Modify: `prompts.py`
- Test: `tests/test_intent_routing.py`
- Test: `tests/test_prompt_style.py`

- [ ] **Step 1: 写失败测试约束工具 Schema 和执行传递**

测试 `search_knowledge` schema 含必需的 `intent` object，其字段包含 `scope/task_type/operation/excluded_intents/prerequisites/is_correction/confidence`；Executor 将 `operation` 转为 SearchRequest.intent，并把排除意图传入 KnowledgeBase。

- [ ] **Step 2: 运行测试确认旧 Schema 失败**

Run: `python -m pytest tests/test_intent_routing.py tests/test_prompt_style.py -q`

Expected: FAIL because schema has no structured `intent`。

- [ ] **Step 3: 实现工具 Schema、校验和检索传递**

`search_knowledge` 要求 `query` 和 `intent`。Executor 使用 `QueryIntent.from_mapping()` 清洗模型参数；KnowledgeBase 优先使用已校验 `task_type/operation/excluded_intents`，但仍保留原始 query 的明确 Klonet 否定硬规则。

- [ ] **Step 4: 更新 Mentor Prompt**

明确要求模型在检索前保留否定、前置条件和纠正信息，并在 `search_knowledge.intent` 中提交结构化结果；不确定“部署”指环境安装还是平台启动时先澄清。

- [ ] **Step 5: 运行工具与 Prompt 测试**

Run: `python -m pytest tests/test_intent_routing.py tests/test_prompt_style.py tests/test_knowledge.py -q`

Expected: PASS。

### Task 4: Orchestrator 使用模型意图更新本轮策略

**Files:**
- Modify: `answer_policy.py`
- Modify: `orchestrator.py`
- Modify: `tests/test_orchestrator_controls.py`
- Test: `tests/test_intent_routing.py`

- [ ] **Step 1: 写失败的多轮纠正测试**

Fake LLM 首次调用 `search_knowledge` 时提交 `operation=platform_start`、`excluded_intents=[environment_setup]` 和 `is_correction=true`。断言 Executor 收到结构化意图，第二次模型调用中的回答策略是“启动前提、标准启动命令、验证方式”，且工具结果不包含 `environment_setup.md`。

- [ ] **Step 2: 运行测试确认当前编排器仍只使用关键词 route**

Run: `python -m pytest tests/test_orchestrator_controls.py tests/test_intent_routing.py -q`

Expected: FAIL because answer policy is not updated from tool intent。

- [ ] **Step 3: 实现意图驱动的回答策略刷新**

在执行 `search_knowledge` 前校验 intent；Mentor 当前轮保存最后一个可靠 `QueryIntent`。工具调用后原地更新临时回答策略消息。`platform_start` 使用“启动前提、标准启动命令、验证方式”，并禁止环境安装步骤和无证据的 `start.sh` 推测。

- [ ] **Step 4: 运行编排回归**

Run: `python -m pytest tests/test_orchestrator_controls.py tests/test_answer_policy.py tests/test_intent_routing.py -q`

Expected: PASS。

### Task 5: 完整验证

**Files:**
- Test: `tests/`

- [ ] **Step 1: 重建临时索引并执行真实检索断言**

使用临时索引验证 `platform_start` 只返回带启动意图的 Runbook 章节，`environment_setup` 只返回环境文档。

- [ ] **Step 2: 运行完整测试**

Run: `python -m pytest -q`

Expected: all tests pass。

- [ ] **Step 3: 检查改动范围和空白错误**

Run: `git diff --check`，并确认未覆盖用户已有的 `workspace/git_ops.py` 和其他并发修改。
