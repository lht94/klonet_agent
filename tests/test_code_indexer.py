"""Structure-aware Klonet source index coverage."""

from __future__ import annotations

import json
import os

from tests.helpers import local_temp_dir


def _read_rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_code_indexer_chunks_python_by_class_method_and_function():
    from klonet_agent.knowledge.code_indexer import CodeIndexer

    with local_temp_dir() as temp_dir:
        source_root = temp_dir / "vemu_uestc"
        source_file = source_root / "webserver" / "api" / "topo.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text(
            '"""Topology API."""\n'
            "from flask import request\n\n"
            "class TopoAPI:\n"
            "    route = '/master/topo/'\n\n"
            "    def post(self):\n"
            "        return request.json\n\n"
            "def deploy_topology(data):\n"
            "    return data\n",
            encoding="utf-8",
        )
        index_file = temp_dir / "code_index.jsonl"

        count = CodeIndexer(
            source_root=source_root,
            index_file=index_file,
        ).build()
        rows = _read_rows(index_file)

    assert count == 4
    assert [row["symbol"] for row in rows] == [
        "module",
        "TopoAPI",
        "TopoAPI.post",
        "deploy_topology",
    ]
    assert rows[2]["line_start"] == 7
    assert rows[2]["path"] == "webserver/api/topo.py"
    assert rows[2]["layer"] == "source_code"


def test_code_indexer_excludes_git_vendor_binaries_and_minified_assets():
    from klonet_agent.knowledge.code_indexer import CodeIndexer

    with local_temp_dir() as temp_dir:
        source_root = temp_dir / "vemu_uestc"
        (source_root / ".git").mkdir(parents=True)
        (source_root / "static_resources" / "vendor").mkdir(parents=True)
        (source_root / "webserver" / "static").mkdir(parents=True)
        (source_root / "webserver" / "static" / "xterm").mkdir(parents=True)
        (source_root / "test").mkdir(parents=True)
        (source_root / "doc").mkdir(parents=True)
        (source_root / "mains").mkdir(parents=True)
        (source_root / ".git" / "config").write_text("secret", encoding="utf-8")
        (source_root / "static_resources" / "vendor" / "lib.c").write_text(
            "int vendor(void) { return 1; }",
            encoding="utf-8",
        )
        (source_root / "webserver" / "static" / "app.min.js").write_text(
            "function bundled(){return 1}",
            encoding="utf-8",
        )
        (source_root / "webserver" / "static" / "jquery-1.12.4.js").write_text(
            "function jquery(){return 1}",
            encoding="utf-8",
        )
        (source_root / "webserver" / "static" / "xterm" / "terminal.js").write_text(
            "function terminal(){return 1}",
            encoding="utf-8",
        )
        (source_root / "test" / "old_behavior.py").write_text(
            "def legacy_test():\n    return False\n",
            encoding="utf-8",
        )
        (source_root / "doc" / "deployment.md").write_text(
            "# Historical deployment",
            encoding="utf-8",
        )
        (source_root / "mains" / "master_main.py").write_text(
            "def main():\n    return 'master'\n",
            encoding="utf-8",
        )
        index_file = temp_dir / "code_index.jsonl"

        CodeIndexer(source_root=source_root, index_file=index_file).build()
        text = index_file.read_text(encoding="utf-8")

    assert "master_main.py" in text
    assert ".git" not in text
    assert "static_resources" not in text
    assert "app.min.js" not in text
    assert "jquery-1.12.4.js" not in text
    assert "terminal.js" not in text
    assert "old_behavior.py" not in text
    assert "deployment.md" not in text


def test_code_indexer_detects_source_change_after_build():
    from klonet_agent.knowledge.code_indexer import CodeIndexer

    with local_temp_dir() as temp_dir:
        source_root = temp_dir / "vemu_uestc"
        source_file = source_root / "mains" / "master_main.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("PORT = 5000\n", encoding="utf-8")
        indexer = CodeIndexer(
            source_root=source_root,
            index_file=temp_dir / "code_index.jsonl",
        )

        indexer.build()
        assert indexer.is_stale() is False
        newer_ns = indexer.index_file.stat().st_mtime_ns + 1_000_000_000
        os.utime(source_file, ns=(newer_ns, newer_ns))

        assert indexer.is_stale() is True


def test_code_indexer_redacts_literal_credentials_before_indexing():
    from klonet_agent.knowledge.code_indexer import CodeIndexer

    with local_temp_dir() as temp_dir:
        source_root = temp_dir / "vemu_uestc"
        source_file = source_root / "vemu_config" / "config.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text(
            "REDIS_PASSWORD = 'do-not-embed-this'\n"
            'config = {"api_key": "also-private"}\n'
            "password = request.json['password']\n"
            "URL = 'redis://worker:literal-pass@127.0.0.1/0'\n",
            encoding="utf-8",
        )
        index_file = temp_dir / "code_index.jsonl"

        CodeIndexer(source_root=source_root, index_file=index_file).build()
        text = index_file.read_text(encoding="utf-8")

    assert "do-not-embed-this" not in text
    assert "also-private" not in text
    assert "literal-pass" not in text
    assert text.count("[REDACTED]") == 3
    assert "request.json" in text
