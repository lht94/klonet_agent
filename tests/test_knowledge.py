"""知识库第一版测试。"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def test_knowledge_index_and_search(tmp_path):
    from klonet_agent.knowledge.indexer import KnowledgeIndexer
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# Klonet\n\n项目日志用于记录验收差异。", encoding="utf-8")
    index_file = tmp_path / "index.jsonl"

    count = KnowledgeIndexer(root=root, index_file=index_file).build()
    results = KnowledgeRetriever(index_file=index_file).search("项目日志 验收", top_k=3)

    assert count > 0
    assert results
    assert "验收" in results[0].snippet
