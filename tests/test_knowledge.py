"""知识库第一版测试。"""

import sys
from pathlib import Path

from tests.helpers import local_temp_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_knowledge_index_and_search():
    from klonet_agent.knowledge.indexer import KnowledgeIndexer
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    with local_temp_dir() as temp_dir:
        root = temp_dir / "repo"
        root.mkdir()
        (root / "README.md").write_text("# Klonet\n\n项目日志用于记录验收差异。", encoding="utf-8")
        index_file = temp_dir / "index.jsonl"

        count = KnowledgeIndexer(root=root, index_file=index_file).build()
        results = KnowledgeRetriever(index_file=index_file).search("项目日志 验收", top_k=3)

    assert count > 0
    assert results
    assert "验收" in results[0].snippet


def test_knowledge_index_skips_runtime_memory_files():
    """运行时记忆不应该进入 Klonet 知识库索引。"""

    from klonet_agent.knowledge.indexer import KnowledgeIndexer

    with local_temp_dir() as temp_dir:
        root = temp_dir / "repo"
        memory_dir = root / "memory"
        memory_dir.mkdir(parents=True)
        (root / "README.md").write_text("# Klonet\n\n当前项目文档。", encoding="utf-8")
        (memory_dir / "MEMORY.md").write_text("小鸡毛 和 小白 的旧长期记忆", encoding="utf-8")
        (memory_dir / "USER.md").write_text("小白 的旧用户画像", encoding="utf-8")
        (memory_dir / "2026-05-19.md").write_text("小鸡毛 的旧情景记忆", encoding="utf-8")
        (memory_dir / "store.py").write_text('"""记忆源码模块。"""', encoding="utf-8")
        index_file = temp_dir / "index.jsonl"

        count = KnowledgeIndexer(root=root, index_file=index_file).build()
        text = index_file.read_text(encoding="utf-8")

    assert count > 0
    assert "memory/store.py" in text
    assert "MEMORY.md" not in text
    assert "USER.md" not in text
    assert "2026-05-19.md" not in text
    assert "小鸡毛" not in text
    assert "小白" not in text


def test_task_templates_are_available_to_knowledge_index():
    """常见任务模板应该能被知识库索引。"""

    from klonet_agent.knowledge.indexer import KnowledgeIndexer
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    with local_temp_dir() as temp_dir:
        root = temp_dir / "repo"
        knowledge_dir = root / "knowledge"
        knowledge_dir.mkdir(parents=True)
        (root / "README.md").write_text("# Klonet\n", encoding="utf-8")
        (knowledge_dir / "task_templates.md").write_text(
            "# 常见任务模板\n\n## 修复测试失败\n先读取失败信息，再最小修改。\n",
            encoding="utf-8",
        )
        index_file = temp_dir / "index.jsonl"

        KnowledgeIndexer(root=root, index_file=index_file).build()
        results = KnowledgeRetriever(index_file=index_file).search("修复测试失败", top_k=3)

    assert results
    assert results[0].path == "knowledge/task_templates.md"


def test_satellite_platform_overview_is_retrievable_from_curated_knowledge():
    """卫星平台介绍应优先命中 curated 概览，而不是只靠源码索引兜底。"""

    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    results = KnowledgeRetriever().search(
        "卫星平台 是什么 架构 功能 接管",
        top_k=5,
        task_type="concept",
    )

    assert results
    assert results[0].path == "knowledge/klonet/flows/satellite_platform.md"


def test_satellite_query_routes_to_satellite_domain():
    """卫星问题应带 satellite domain，帮助检索收窄证据范围。"""

    from klonet_agent.knowledge import route_query

    route = route_query("卫星平台是什么")

    assert "satellite" in route.domains


def test_multi_platform_startup_runbook_is_retrievable():
    """Platform startup should prefer the generic conflict-aware runbook."""

    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    results = KnowledgeRetriever().search(
        "103 平台 启动 screen 103_m /usr/local/python3/bin gunicorn celery",
        top_k=3,
        task_type="deployment_guidance",
        domains=("runtime",),
    )

    assert results
    assert results[0].path == "knowledge/klonet/ops/multi_platform_startup.md"
    assert "/usr/local/python3/bin/gunicorn" in results[0].snippet


def test_general_query_does_not_force_klonet_results():
    """明确排除 Klonet 的通用问题不应该返回 Klonet 证据。"""

    import json

    from klonet_agent.knowledge.rag import KnowledgeBase
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        row = {
            "source": "curated",
            "path": "knowledge/klonet/ops/startup.md",
            "title": "Klonet 启动",
            "content": "Klonet 使用 Docker 和 screen 启动服务",
        }
        index_file.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        knowledge = KnowledgeBase(KnowledgeRetriever(index_file=index_file))
        result = knowledge.search_knowledge(
            "不需要 Klonet，只需要 Linux VM Docker Compose DinD Rust",
        )

    assert "不属于 Klonet 知识库" in result


def test_retriever_rejects_low_coverage_match():
    """长查询只碰巧命中一个通用词时不应返回证据。"""

    import json

    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        row = {
            "source": "curated",
            "path": "knowledge/klonet/ops/startup.md",
            "title": "Klonet 启动",
            "content": "Klonet 的 Docker 资源执行层。",
        }
        index_file.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        results = KnowledgeRetriever(index_file=index_file).search(
            "Linux VM Docker Compose DinD Rust 自定义网络",
        )

    assert results == []
