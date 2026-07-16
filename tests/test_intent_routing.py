"""结构化意图路由与知识目录过滤测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from tests.helpers import local_temp_dir


def test_query_intent_preserves_correction_and_exclusions():
    """模型输出应被清洗为稳定、不可变的结构化意图。"""

    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "operation_guide",
            "operation": "platform_start",
            "target": "klonet_platform",
            "excluded_intents": ["environment_setup", ""],
            "prerequisites": ["environment_ready"],
            "requires_environment_diagnosis": True,
            "is_correction": True,
            "confidence": 0.96,
        }
    )

    assert intent.scope == "klonet"
    assert intent.task_type == "operation_guide"
    assert intent.operation == "platform_start"
    assert intent.target == "klonet_platform"
    assert intent.excluded_intents == ("environment_setup",)
    assert intent.prerequisites == ("environment_ready",)
    assert intent.requires_environment_diagnosis is True
    assert intent.is_correction is True
    assert intent.confidence == 0.96


def test_query_intent_rejects_unknown_enum_values_and_clamps_confidence():
    """系统不能直接信任模型生成的枚举和值域。"""

    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "external",
            "task_type": "invented",
            "operation": "destroy_everything",
            "confidence": 4,
        }
    )

    assert intent.scope == "klonet"
    assert intent.task_type == "concept"
    assert intent.operation == "unknown"
    assert intent.confidence == 1.0


def test_troubleshooting_ops_terms_enable_environment_diagnosis():
    """模型漏填新字段时，运维类故障也应进入环境诊断分支。"""

    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "troubleshooting",
            "operation": "platform_start",
            "target": "nginx",
            "symptom": "port_conflict",
            "confidence": 0.91,
        }
    )

    assert intent.requires_environment_diagnosis is True


def test_platform_start_intent_filters_environment_setup():
    """启动意图应在 BM25 前排除环境安装文档。"""

    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        _row(
            "knowledge/klonet/ops/environment_setup.md",
            ["environment_setup", "dependency_install"],
            "Klonet 环境启动安装脚本",
        ),
        _row(
            "knowledge/klonet/ops/startup_shutdown.md",
            ["platform_start", "platform_stop", "platform_restart"],
            "Klonet Master Celery Worker 标准启动命令",
        ),
    ]
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        index_file.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        outcome = KnowledgeRetriever(index_file=index_file).search_request(
            SearchRequest(query="Klonet 启动", intent="platform_start", top_k=3)
        )

    assert [item.path for item in outcome.results] == [
        "knowledge/klonet/ops/startup_shutdown.md"
    ]
    assert outcome.results[0].intent_tags == ("platform_start", "platform_stop", "platform_restart")


def test_excluded_intent_filters_conflicting_documents():
    """模型识别出的明确否定必须成为检索硬过滤条件。"""

    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        _row(
            "knowledge/klonet/ops/environment_setup.md",
            ["environment_setup"],
            "Klonet 环境启动",
        ),
        _row(
            "knowledge/klonet/ops/startup_shutdown.md",
            ["platform_start"],
            "Klonet 平台启动",
        ),
    ]
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        index_file.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        outcome = KnowledgeRetriever(index_file=index_file).search_request(
            SearchRequest(
                query="不是环境配置，我要启动 Klonet",
                excluded_intents=("environment_setup",),
                top_k=3,
            )
        )

    assert all("environment_setup" not in item.intent_tags for item in outcome.results)


def test_indexer_carries_frontmatter_intent_tags_into_chunks():
    """知识目录标签应由文档 frontmatter 自动进入索引。"""

    from klonet_agent.knowledge.indexer import KnowledgeIndexer

    with local_temp_dir() as temp_dir:
        source = temp_dir / "knowledge" / "klonet" / "ops" / "startup.md"
        source.parent.mkdir(parents=True)
        source.write_text(
            """---
title: 启动
domains: operations, runtime
intent_tags: platform_start, platform_restart
---
# 启动
标准启动命令。
""",
            encoding="utf-8",
        )
        index_file = temp_dir / "index.jsonl"
        KnowledgeIndexer(root=temp_dir, index_file=index_file).build()
        rows = [json.loads(line) for line in index_file.read_text(encoding="utf-8").splitlines()]

    assert rows
    assert rows[0]["intent_tags"] == ["platform_start", "platform_restart"]


def test_search_tool_requires_structured_intent_schema():
    """模型应在第一次检索调用中同时提交结构化意图。"""

    from klonet_agent.tools.registry import TOOLS

    tool = next(
        item for item in TOOLS if item["function"]["name"] == "search_knowledge"
    )
    parameters = tool["function"]["parameters"]
    intent_schema = parameters["properties"]["intent"]

    assert "intent" in parameters["required"]
    assert intent_schema["type"] == "object"
    assert {
        "scope",
        "task_type",
        "operation",
        "target",
        "excluded_intents",
        "prerequisites",
        "requires_environment_diagnosis",
        "is_correction",
        "confidence",
    } <= set(intent_schema["properties"])
    assert {"scope", "task_type", "operation", "confidence"} <= set(
        intent_schema["required"]
    )


def test_source_code_tools_are_registered_for_llm():
    """Mentor 需要能看到源码 grep/read 工具 schema。"""

    from klonet_agent.tools.registry import TOOLS

    tool_names = {item["function"]["name"] for item in TOOLS}

    assert "search_code" in tool_names
    assert "read_source_file" in tool_names
    assert "list_source_files" in tool_names


def test_executor_validates_intent_before_calling_knowledge_base():
    """不可信工具参数必须在执行层转换为 QueryIntent。"""

    from klonet_agent.tools.executor import ToolExecutor

    recorder = _RecordingKnowledgeBase()
    args = {
        "query": "不是配置环境，我要启动 Klonet",
        "intent": {
            "scope": "klonet",
            "task_type": "operation_guide",
            "operation": "platform_start",
            "target": "klonet_platform",
            "excluded_intents": ["environment_setup"],
            "prerequisites": ["environment_ready"],
            "is_correction": True,
            "confidence": 0.97,
        },
    }

    with patch("klonet_agent.tools.executor.KNOWLEDGE_BASE", recorder):
        result = ToolExecutor(allowed_tools={"search_knowledge"}).run(
            "search_knowledge",
            args,
        )

    assert result == "evidence"
    assert recorder.intent.operation == "platform_start"
    assert recorder.intent.excluded_intents == ("environment_setup",)
    assert recorder.intent.is_correction is True


class _RecordingKnowledgeBase:
    def __init__(self):
        self.intent = None

    def search_knowledge(self, query, top_k, **kwargs):
        self.intent = kwargs["intent"]
        return "evidence"


def test_old_index_rows_require_intent_metadata_migration():
    """旧索引不能在新意图过滤逻辑下静默返回空结果。"""

    from klonet_agent.knowledge.retriever import _needs_intent_metadata_migration

    assert _needs_intent_metadata_migration([{"path": "old.md"}]) is True
    assert _needs_intent_metadata_migration(
        [{"path": "new.md", "intent_tags": []}]
    ) is True
    assert _needs_intent_metadata_migration(
        [{"path": "old-v2.md", "intent_tags": [], "index_schema_version": 2}]
    ) is True
    assert _needs_intent_metadata_migration(
        [{"path": "new.md", "intent_tags": [], "index_schema_version": 3}]
    ) is False


def test_markdown_chunks_keep_parent_heading_context_and_skip_empty_parents():
    """命令子章节必须继承“启动 Master”等父级语义。"""

    from klonet_agent.knowledge.indexer import _split_markdown_sections

    sections = _split_markdown_sections(
        """# Klonet 启停
## 第四步：启动 Master
### 服务器路径
```bash
gunicorn -c gun.py master_main:flask_app
```
"""
    )

    assert sections == [
        (
            "Klonet 启停 / 第四步：启动 Master / 服务器路径",
            "### 服务器路径\n\n```bash\ngunicorn -c gun.py master_main:flask_app\n```",
        )
    ]


def test_markdown_chunker_ignores_hash_comments_inside_code_fences():
    """Shell 注释不能被误识别为知识章节标题。"""

    from klonet_agent.knowledge.indexer import _split_markdown_sections

    sections = _split_markdown_sections(
        """# 正常停止
```bash
screen -r worker
# 按 Ctrl+C
```
## 常见问题
正文。
"""
    )

    assert [title for title, _ in sections] == ["正常停止", "正常停止 / 常见问题"]
    assert "# 按 Ctrl+C" in sections[0][1]


def test_platform_start_builds_component_complete_retrieval_plan():
    """平台启动意图应生成覆盖所有核心服务的检索请求。"""

    from klonet_agent.knowledge.intent import QueryIntent
    from klonet_agent.knowledge.rag import KnowledgeBase

    recorder = _RecordingRetriever()
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "operation_guide",
            "operation": "platform_start",
            "confidence": 0.98,
        }
    )

    KnowledgeBase(retriever=recorder).search_knowledge(
        "怎么启动 Klonet",
        top_k=3,
        intent=intent,
    )

    assert recorder.request.intent == "platform_start"
    assert recorder.request.top_k >= 7
    for term in ("Redis", "Master", "Celery", "Web Terminal", "Worker", "Nginx"):
        assert term in recorder.request.query


def test_low_confidence_intent_does_not_filter_retrieval():
    """低置信度模型意图必须在策略和检索两侧同时降级。"""

    from klonet_agent.knowledge.intent import QueryIntent
    from klonet_agent.knowledge.rag import KnowledgeBase

    recorder = _RecordingRetriever()
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "operation_guide",
            "operation": "platform_start",
            "confidence": 0.4,
        }
    )

    KnowledgeBase(retriever=recorder).search_knowledge(
        "Klonet 是什么",
        top_k=3,
        intent=intent,
    )

    assert recorder.request.intent == "unknown"
    assert recorder.request.top_k == 3
    assert recorder.request.query == "Klonet 是什么"




def test_intent_routes_to_document_collection_before_bm25():
    """前置意图应先收敛到文档集合，再在集合内做具体检索。"""

    from klonet_agent.knowledge.intent import QueryIntent
    from klonet_agent.knowledge.rag import KnowledgeBase

    recorder = _RecordingRetriever()
    startup_intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "deployment_guidance",
            "operation": "platform_start",
            "target": "klonet_platform",
            "confidence": 0.95,
        }
    )

    KnowledgeBase(retriever=recorder).search_knowledge(
        "怎么启动 Klonet",
        top_k=3,
        intent=startup_intent,
    )

    assert recorder.request.collections == ("klonet_runtime_startup",)
    assert recorder.request.allowed_paths == (
        "knowledge/klonet/ops/source_acquisition_git.md",
        "knowledge/klonet/ops/multi_platform_startup.md",
        "knowledge/klonet/ops/startup_shutdown.md",
    )

    environment_intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "deployment_preparation",
            "operation": "environment_setup",
            "target": "klonet_environment",
            "confidence": 0.95,
        }
    )

    KnowledgeBase(retriever=recorder).search_knowledge(
        "怎么安装 Klonet 环境",
        top_k=3,
        intent=environment_intent,
    )

    assert recorder.request.collections == ("klonet_environment_setup",)
    assert recorder.request.allowed_paths == ("knowledge/klonet/ops/environment_setup.md",)


def test_platform_usage_routes_to_user_operation_collection():
    """普通用户平台使用问题应先路由到用户操作手册，而不是启动 runbook。"""

    from klonet_agent.knowledge.intent import QueryIntent
    from klonet_agent.knowledge.rag import KnowledgeBase

    recorder = _RecordingRetriever()
    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "operation_guide",
            "operation": "unknown",
            "target": "klonet_platform platform_usage normal_user browser",
            "excluded_intents": ["platform_start", "environment_setup"],
            "confidence": 0.92,
        }
    )

    KnowledgeBase(retriever=recorder).search_knowledge(
        "是第一种，我怎么使用",
        top_k=3,
        intent=intent,
    )

    assert recorder.request.collections == ("klonet_platform_usage",)
    assert "klonet_runtime_startup" not in recorder.request.collections
    assert recorder.request.allowed_paths == (
        "knowledge/klonet/usage/platform_usage.md",
    )


def test_platform_usage_query_retrieves_indexed_user_guide_evidence():
    """Platform usage collections must point at indexed curated documents."""

    from klonet_agent.knowledge.intent import QueryIntent
    from klonet_agent.knowledge.rag import KNOWLEDGE_BASE

    intent = QueryIntent(
        scope="klonet",
        task_type="operation_guide",
        operation="unknown",
        target="klonet platform_usage normal_user browser experiment topology",
        excluded_intents=("platform_start", "environment_setup"),
        confidence=0.92,
    )

    evidence = KNOWLEDGE_BASE.search_knowledge(
        "我想使用klonet去做实验",
        top_k=3,
        intent=intent,
    )

    assert "未检索到可靠 Klonet 证据" not in evidence
    assert "platform_usage.md" in evidence


def test_topology_node_type_query_retrieves_curated_node_guide():
    """Node palette questions should not rely only on partial symbol hits."""

    from klonet_agent.knowledge.intent import QueryIntent
    from klonet_agent.knowledge.rag import KNOWLEDGE_BASE

    intent = QueryIntent(
        scope="klonet",
        task_type="operation_guide",
        operation="unknown",
        target="topology node_types host switch router controller kvm",
        excluded_intents=("environment_setup", "dependency_install", "platform_start"),
        confidence=0.92,
    )

    evidence = KNOWLEDGE_BASE.search_knowledge(
        "拓扑里能放置哪些节点呢？",
        top_k=4,
        intent=intent,
    )

    assert "未检索到可靠 Klonet 证据" not in evidence
    assert "topology_node_types.md" in evidence
    for term in ("Host", "Switch", "Router", "Controller", "KVM"):
        assert term in evidence


def test_platform_start_filters_stop_restart_and_failure_sections():
    """启动指南不应被同一 Runbook 中的停止和故障章节占据。"""

    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        _row_with_title("常见问题 / Master 启动失败", "Master 启动失败"),
        _row_with_title("正常停止", "Master Worker 停止命令"),
        _row_with_title(
            "第四步：启动 Master / 服务器路径",
            "Master 标准启动命令 gunicorn master_main:flask_app",
        ),
    ]
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        index_file.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        outcome = KnowledgeRetriever(index_file=index_file).search_request(
            SearchRequest(
                query="Klonet Master 启动命令",
                task_type="operation_guide",
                intent="platform_start",
                top_k=3,
            )
        )

    assert [item.title for item in outcome.results] == [
        "第四步：启动 Master / 服务器路径"
    ]


def test_platform_start_selects_distinct_runbook_stages():
    """候选不能被同一服务的服务器/虚拟机变体重复占满。"""

    from klonet_agent.knowledge.models import SearchRequest
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    rows = [
        _row_with_title("服务器角色与启动顺序", "Master Celery Worker 启动顺序"),
        _row_with_title(
            "第五步：启动 Celery / 服务器内虚拟机路径",
            "Celery Master Worker 启动命令",
        ),
        _row_with_title(
            "第五步：启动 Celery / 服务器路径",
            "Celery Master Worker 启动命令",
        ),
        _row_with_title(
            "第四步：启动 Master / 服务器路径",
            "Master 启动命令 gunicorn",
        ),
    ]
    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        index_file.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        outcome = KnowledgeRetriever(index_file=index_file).search_request(
            SearchRequest(
                query="Master Celery Worker 启动命令",
                task_type="operation_guide",
                intent="platform_start",
                top_k=3,
            )
        )

    assert [item.title for item in outcome.results] == [
        "服务器角色与启动顺序",
        "第四步：启动 Master / 服务器路径",
        "第五步：启动 Celery / 服务器路径",
    ]


class _RecordingRetriever:
    def __init__(self):
        self.request = None

    def search_request(self, request):
        from klonet_agent.knowledge.models import SearchOutcome

        self.request = request
        return SearchOutcome(status="none", reason="recorded")


def _row_with_title(title: str, content: str) -> dict:
    row = _row(
        "knowledge/klonet/ops/startup_shutdown.md",
        ["platform_start", "platform_stop", "platform_restart"],
        content,
    )
    row["title"] = title
    return row


def _row(path: str, intent_tags: list[str], content: str) -> dict:
    return {
        "chunk_id": path,
        "layer": "curated",
        "source": "curated",
        "path": path,
        "title": content,
        "content": content,
        "domain": "runtime",
        "intent_tags": intent_tags,
        "priority": "P0",
        "status": "current",
        "quality": "verified",
        "sensitivity": "public",
        "last_verified": "2026-06-24",
    }
