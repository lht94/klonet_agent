"""Klonet 四层知识资产构建与索引接入测试。"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

from tests.helpers import local_temp_dir


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_generate_klonet_machine_indexes():
    from scripts.generate_klonet_indexes import generate_indexes

    with local_temp_dir() as temp_dir:
        source = temp_dir / "vemu"
        output = temp_dir / "indexes"
        (source / "webserver" / "api" / "topo").mkdir(parents=True)
        (source / "webserver" / "tasks" / "topo").mkdir(parents=True)
        (source / "vemu_config").mkdir(parents=True)

        (source / "webserver" / "api" / "topo" / "master_topo.py").write_text(
            "class TopoDeployAPI:\n"
            "    \"\"\"部署拓扑。\"\"\"\n"
            "    def post(self):\n"
            "        return {'code': 1}\n",
            encoding="utf-8",
        )
        (source / "webserver" / "app_factory.py").write_text(
            "def create_master_app():\n"
            "    from .api.topo import master_topo\n"
            "    register_api(master_topo.TopoDeployAPI, 'master_topo', '/master/topo/')\n",
            encoding="utf-8",
        )
        (source / "webserver" / "tasks" / "topo" / "tasks.py").write_text(
            "class Celery:\n"
            "    def task(self, **kwargs):\n"
            "        return lambda fn: fn\n"
            "celery = Celery()\n"
            "@celery.task(track_started=True)\n"
            "def master_deploy_topo(data):\n"
            "    \"\"\"异步部署拓扑。\"\"\"\n"
            "    return data\n",
            encoding="utf-8",
        )
        (source / "vemu_config" / "config.py").write_text(
            "class CommonConfig:\n"
            "    master_port = 5000\n"
            "    redis_password = 'must-not-leak'\n"
            "    mail_pass = 'also-must-not-leak'\n",
            encoding="utf-8",
        )

        (source / "legacy.py").write_text(
            'pattern = "\\d+"\n',
            encoding="utf-8",
        )

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            counts = generate_indexes(source, output)
        assert not [item for item in captured if item.category is SyntaxWarning]

        routes = _read_jsonl(output / "routes.jsonl")
        symbols = _read_jsonl(output / "symbols.jsonl")
        tasks = _read_jsonl(output / "celery_tasks.jsonl")
        configs = _read_jsonl(output / "config_items.jsonl")
        config_text = (output / "config_items.jsonl").read_text(encoding="utf-8")

    assert counts["routes"] == 1
    assert routes[0]["route"] == "/master/topo/"
    assert routes[0]["side"] == "master"
    assert routes[0]["implementation"] == "webserver/api/topo/master_topo.py"
    assert any(row["symbol"] == "TopoDeployAPI" for row in symbols)
    assert tasks[0]["symbol"] == "master_deploy_topo"
    password = next(row for row in configs if row["name"] == "redis_password")
    mail_pass = next(row for row in configs if row["name"] == "mail_pass")
    assert password["sensitive"] is True
    assert password["default"] == "<redacted>"
    assert mail_pass["sensitive"] is True
    assert mail_pass["default"] == "<redacted>"
    assert "must-not-leak" not in config_text
    assert "also-must-not-leak" not in config_text


def test_generator_prefers_runtime_package_over_duplicate_tree():
    from scripts.generate_klonet_indexes import generate_indexes

    with local_temp_dir() as temp_dir:
        source = temp_dir / "repo"
        for prefix in (Path(), Path("vemu_uestc")):
            api = source / prefix / "webserver" / "api" / "topo"
            api.mkdir(parents=True)
            (api / "master_topo.py").write_text(
                "class TopoDeployAPI:\n    def post(self):\n        return {}\n",
                encoding="utf-8",
            )
            (source / prefix / "webserver" / "app_factory.py").write_text(
                "def create_master_app():\n"
                "    from .api.topo import master_topo\n"
                "    register_api(master_topo.TopoDeployAPI, 'topo', '/master/topo/')\n",
                encoding="utf-8",
            )

        output = temp_dir / "indexes"
        counts = generate_indexes(source, output)
        routes = _read_jsonl(output / "routes.jsonl")

    assert counts["routes"] == 1
    assert routes[0]["implementation"] == "vemu_uestc/webserver/api/topo/master_topo.py"

def test_main_index_uses_curated_experience_and_machine_layers_only():
    from klonet_agent.knowledge.indexer import KnowledgeIndexer

    with local_temp_dir() as temp_dir:
        root = temp_dir / "repo"
        knowledge = root / "knowledge"
        (knowledge / "klonet").mkdir(parents=True)
        (knowledge / "klonet_experience" / "cases").mkdir(parents=True)
        (knowledge / "klonet_index").mkdir(parents=True)
        (knowledge / "staging").mkdir(parents=True)
        (knowledge / "extracted_docs").mkdir(parents=True)

        (root / "README.md").write_text("# Test\n", encoding="utf-8")
        (knowledge / "klonet" / "guide.md").write_text("正式拓扑部署知识", encoding="utf-8")
        (knowledge / "klonet_experience" / "cases" / "case.md").write_text(
            "拓扑进度卡住案例", encoding="utf-8"
        )
        (knowledge / "klonet_index" / "routes.jsonl").write_text(
            json.dumps({"route": "/master/topo/", "domain": "topology"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (knowledge / "staging" / "draft.md").write_text("不应索引草稿", encoding="utf-8")
        (knowledge / "extracted_docs" / "raw.raw.md").write_text("不应索引原始抽取", encoding="utf-8")

        index_file = temp_dir / "index.jsonl"
        KnowledgeIndexer(root=root, index_file=index_file).build()
        rows = _read_jsonl(index_file)
        text = index_file.read_text(encoding="utf-8")

    assert "正式拓扑部署知识" in text
    assert "拓扑进度卡住案例" in text
    assert "/master/topo/" in text
    assert "不应索引草稿" not in text
    assert "不应索引原始抽取" not in text
    sources = {row["path"]: row["source"] for row in rows}
    assert sources["knowledge/klonet/guide.md"] == "curated"
    assert sources["knowledge/klonet_experience/cases/case.md"] == "experience"
    assert sources["knowledge/klonet_index/routes.jsonl"] == "machine_index"


def test_exact_route_query_prefers_machine_index():
    from klonet_agent.knowledge.retriever import KnowledgeRetriever

    with local_temp_dir() as temp_dir:
        index_file = temp_dir / "index.jsonl"
        rows = [
            {
                "source": "curated",
                "path": "knowledge/klonet/flows/topology_deploy.md",
                "title": "Topology",
                "content": ("/master/topo/ TopoDeployAPI " * 4).strip(),
            },
            {
                "source": "machine_index",
                "path": "knowledge/klonet_index/routes.jsonl",
                "title": "routes#/master/topo/",
                "content": '{"route": "/master/topo/", "view_class": "TopoDeployAPI"}',
            },
        ]
        index_file.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        results = KnowledgeRetriever(index_file=index_file).search(
            "/master/topo/ TopoDeployAPI",
            top_k=1,
        )

    assert results[0].source == "machine_index"
    assert results[0].title == "routes#/master/topo/"
