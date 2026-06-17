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
