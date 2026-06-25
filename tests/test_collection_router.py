import json


def test_collection_router_loads_manifest_files(tmp_path):
    from klonet_agent.knowledge.collection_router import (
        load_collections,
        route_collections,
    )
    from klonet_agent.knowledge.intent import QueryIntent

    manifest_dir = tmp_path / "knowledge" / "klonet" / "ops" / "custom"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps(
            {
                "collection_id": "custom_startup",
                "title": "Custom startup collection",
                "paths": ["knowledge/klonet/ops/custom_startup.md"],
                "task_types": ["deployment_guidance"],
                "operations": ["platform_start"],
                "targets": ["custom_platform"],
                "excluded_operations": ["environment_setup"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "deployment_guidance",
            "operation": "platform_start",
            "target": "custom_platform",
            "confidence": 0.95,
        }
    )

    collections = load_collections(root=tmp_path / "knowledge" / "klonet")
    matches = route_collections(intent, collections=collections)

    assert [item.collection_id for item in collections] == ["custom_startup"]
    assert [item.collection_id for item in matches] == ["custom_startup"]
    assert matches[0].paths == ("knowledge/klonet/ops/custom_startup.md",)
