"""检索评估 runner 测试。"""

import json

from tests.helpers import local_temp_dir


def test_retrieval_eval_runner_calculates_metrics():
    from klonet_agent.evals.retrieval_runner import RetrievalEvalRunner
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        row = {
            "chunk_id": "route",
            "layer": "machine_index",
            "source": "machine_index",
            "path": "knowledge/klonet_index/routes.jsonl",
            "title": "/master/topo/",
            "content": '{"route": "/master/topo/", "view_class": "TopoDeployAPI"}',
            "domain": "topology",
            "priority": "P1",
            "status": "current",
            "quality": "generated",
            "sensitivity": "public",
            "last_verified": "",
        }
        index_file.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

        cases = [
            {
                "id": "hit",
                "query": "/master/topo/ TopoDeployAPI 在哪里实现",
                "expected_scope": "klonet",
                "expected_task_type": "code_lookup",
                "expected_paths": ["knowledge/klonet_index/routes.jsonl"],
                "should_retrieve": True,
            },
            {
                "id": "general",
                "query": "与 Klonet 无关，只讲 Docker 网络",
                "expected_scope": "general",
                "expected_task_type": "general",
                "expected_paths": [],
                "should_retrieve": False,
            },
        ]
        case_file = temp_dir / "cases.jsonl"
        case_file.write_text(
            "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
            encoding="utf-8",
        )
        metrics = RetrievalEvalRunner(
            case_file=case_file,
            output_file=temp_dir / "summary.md",
            retriever=KnowledgeRetriever(index_file=index_file),
        ).run(write_summary=False)

    assert metrics["scope_accuracy"] == 1.0
    assert metrics["recall_at_3"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["abstention_accuracy"] == 1.0
