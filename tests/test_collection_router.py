import json
from pathlib import Path


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


def test_collection_router_routes_topology_deploy_to_flow_collection():
    from klonet_agent.knowledge.collection_router import route_collections
    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "troubleshooting",
            "operation": "topology_deploy",
            "target": "topology",
            "confidence": 0.93,
        }
    )

    matches = route_collections(intent)

    assert "klonet_topology_deploy" in [item.collection_id for item in matches]
    assert any(
        "knowledge/klonet/flows/topology_deploy.md" in item.paths
        for item in matches
    )


def test_collection_router_routes_platform_usage_to_user_guides():
    from klonet_agent.knowledge.collection_router import route_collections
    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "operation_guide",
            "operation": "unknown",
            "target": "klonet_platform platform_usage normal_user browser",
            "confidence": 0.9,
        }
    )

    matches = route_collections(intent)

    ids = [item.collection_id for item in matches]
    assert "klonet_platform_usage" in ids
    assert any(
        "knowledge/klonet/usage/platform_usage.md" == path
        for item in matches
        for path in item.paths
    )


def test_all_curated_klonet_markdown_docs_are_covered_by_collections():
    """每个面向回答的 Klonet Markdown 文档都应被至少一个 manifest 覆盖。"""

    from klonet_agent.config import PROJECT_ROOT
    from klonet_agent.knowledge.collection_router import (
        collection_paths,
        load_collections,
    )

    docs = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "knowledge" / "klonet").rglob("*.md")
        if path.name.lower() != "readme.md"
    }
    covered = set(collection_paths(load_collections()))

    assert docs <= covered


def test_collection_router_routes_foundation_concepts_to_overview_collection():
    from klonet_agent.knowledge.collection_router import route_collections
    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "concept",
            "operation": "unknown",
            "target": "architecture terminology klonet_platform",
            "confidence": 0.9,
        }
    )

    ids = [item.collection_id for item in route_collections(intent)]

    assert "klonet_foundation" in ids


def test_collection_router_routes_development_to_dev_collection():
    from klonet_agent.knowledge.collection_router import route_collections
    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "development",
            "operation": "unknown",
            "target": "backend api git workflow",
            "confidence": 0.9,
        }
    )

    ids = [item.collection_id for item in route_collections(intent)]

    assert "klonet_development" in ids


def test_collection_router_routes_vm_and_satellite_flows():
    from klonet_agent.knowledge.collection_router import route_collections
    from klonet_agent.knowledge.intent import QueryIntent

    vm_intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "troubleshooting",
            "operation": "unknown",
            "target": "kvm vm terminal networking",
            "confidence": 0.9,
        }
    )
    satellite_intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "concept",
            "operation": "unknown",
            "target": "satellite satellite_platform 星座",
            "confidence": 0.9,
        }
    )

    vm_ids = [item.collection_id for item in route_collections(vm_intent)]
    satellite_ids = [item.collection_id for item in route_collections(satellite_intent)]

    assert "klonet_vm_networking" in vm_ids
    assert "klonet_satellite_platform" in satellite_ids


def test_collection_router_routes_common_troubleshooting():
    from klonet_agent.knowledge.collection_router import route_collections
    from klonet_agent.knowledge.intent import QueryIntent

    intent = QueryIntent.from_mapping(
        {
            "scope": "klonet",
            "task_type": "troubleshooting",
            "operation": "unknown",
            "target": "worker redis nginx web_terminal",
            "symptom": "connection refused address already in use",
            "confidence": 0.9,
        }
    )

    ids = [item.collection_id for item in route_collections(intent)]

    assert "klonet_common_troubleshooting" in ids
