"""Route structured intents to explicit knowledge document collections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from klonet_agent.knowledge.intent import QueryIntent

_DEFAULT_COLLECTION_ROOT = Path(__file__).resolve().parent / "klonet"


@dataclass(frozen=True)
class KnowledgeCollection:
    """A small, explicit document set selected before chunk retrieval."""

    collection_id: str
    title: str
    paths: tuple[str, ...]
    task_types: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    excluded_operations: tuple[str, ...] = ()


def load_collections(root: Path | None = None) -> tuple[KnowledgeCollection, ...]:
    """Load knowledge collection manifests from the knowledge tree."""

    collection_root = root or _DEFAULT_COLLECTION_ROOT
    if not collection_root.exists():
        return ()

    collections = []
    for manifest_path in sorted(collection_root.rglob("manifest.json")):
        collection = _collection_from_manifest(manifest_path)
        if collection is not None:
            collections.append(collection)
    return tuple(collections)


def route_collections(
    intent: QueryIntent | None,
    *,
    collections: tuple[KnowledgeCollection, ...] | None = None,
) -> tuple[KnowledgeCollection, ...]:
    """Return document collections that match the structured intent."""

    if intent is None or intent.scope == "general" or intent.confidence < 0.6:
        return ()

    candidates = collections if collections is not None else load_collections()
    matches = []
    excluded = set(intent.excluded_intents)
    for collection in candidates:
        if intent.operation in collection.excluded_operations:
            continue
        if excluded.intersection(collection.operations):
            continue
        if intent.operation in collection.operations:
            matches.append(collection)
            continue
        if intent.task_type in collection.task_types and _target_matches(intent, collection):
            matches.append(collection)

    return tuple(dict.fromkeys(matches))


def collection_ids(collections: tuple[KnowledgeCollection, ...]) -> tuple[str, ...]:
    return tuple(collection.collection_id for collection in collections)


def collection_paths(collections: tuple[KnowledgeCollection, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    for collection in collections:
        for path in collection.paths:
            if path not in paths:
                paths.append(path)
    return tuple(paths)


def _target_matches(intent: QueryIntent, collection: KnowledgeCollection) -> bool:
    target = intent.target.lower()
    symptom = intent.symptom.lower()
    haystack = f"{target} {symptom}"
    return bool(target) and any(term in haystack for term in collection.targets)


def _collection_from_manifest(manifest_path: Path) -> KnowledgeCollection | None:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    collection_id = str(raw.get("collection_id") or "").strip()
    paths = _string_tuple(raw.get("paths"))
    if not collection_id or not paths:
        return None

    return KnowledgeCollection(
        collection_id=collection_id,
        title=str(raw.get("title") or collection_id).strip(),
        paths=paths,
        task_types=_string_tuple(raw.get("task_types")),
        operations=_string_tuple(raw.get("operations")),
        targets=_string_tuple(raw.get("targets")),
        excluded_operations=_string_tuple(raw.get("excluded_operations")),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)

