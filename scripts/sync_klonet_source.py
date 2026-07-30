"""Create a tracked, sanitized Klonet source snapshot from a clean Git commit."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from klonet_agent.config import (
    KLONET_SOURCE_ROOT,
    KLONET_UPSTREAM_SOURCE_ROOT,
    PROJECT_ROOT,
)
from klonet_agent.knowledge.code_indexer import (
    SOURCE_MANIFEST_NAME,
    is_code_source_path,
    redact_sensitive_text,
)


SNAPSHOT_SCHEMA_VERSION = 1


def sync_snapshot(
    source_root: Path = KLONET_UPSTREAM_SOURCE_ROOT,
    destination: Path = KLONET_SOURCE_ROOT,
    *,
    revision: str = "HEAD",
) -> dict:
    """Export only allowed text files from a committed upstream Git tree."""

    source_root = Path(source_root).resolve()
    destination = Path(destination).resolve()
    _validate_source(source_root)
    _validate_destination(destination)

    commit = _git(source_root, "rev-parse", revision).strip()
    remote = _git(
        source_root,
        "remote",
        "get-url",
        "origin",
        allow_failure=True,
    ).strip()
    entries = _git_tree_entries(source_root, commit)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            dir=str(destination.parent),
        )
    )
    backup = destination.parent / f".{destination.name}.backup-{os.getpid()}"
    written_paths: list[str] = []
    redacted_files = 0
    try:
        for relative_text, size in entries:
            relative = Path(relative_text)
            if not is_code_source_path(relative, size=size):
                continue
            content = _git_bytes(source_root, commit, relative_text)
            if b"\x00" in content:
                continue
            text = content.decode("utf-8", errors="replace")
            sanitized = redact_sensitive_text(text)
            if sanitized != text:
                redacted_files += 1
            target = temp_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(sanitized, encoding="utf-8")
            written_paths.append(relative.as_posix())

        manifest = {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source_kind": "sanitized_retrieval_snapshot",
            "upstream_remote": remote,
            "upstream_commit": commit,
            "file_count": len(written_paths),
            "redacted_file_count": redacted_files,
        }
        (temp_root / SOURCE_MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if backup.exists():
            raise RuntimeError(f"snapshot backup path already exists: {backup}")
        if destination.exists():
            destination.rename(backup)
        temp_root.rename(destination)
        if backup.exists():
            shutil.rmtree(backup)
        return manifest
    except Exception:
        if not destination.exists() and backup.exists():
            backup.rename(destination)
        raise
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)


def _validate_source(source_root: Path) -> None:
    if not source_root.is_dir():
        raise FileNotFoundError(f"missing upstream source repository: {source_root}")
    if not (source_root / ".git").exists():
        raise ValueError(f"upstream source is not a Git repository: {source_root}")


def _validate_destination(destination: Path) -> None:
    knowledge_root = (PROJECT_ROOT / "knowledge").resolve()
    try:
        destination.relative_to(knowledge_root)
    except ValueError as exc:
        raise ValueError(
            f"snapshot destination must stay inside {knowledge_root}"
        ) from exc
    if destination == knowledge_root:
        raise ValueError("snapshot destination cannot replace the knowledge root")


def _git(
    source_root: Path,
    *args: str,
    allow_failure: bool = False,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source_root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 and not allow_failure:
        detail = completed.stderr.strip() or "git command failed"
        raise RuntimeError(detail)
    return completed.stdout


def _git_tree_entries(source_root: Path, commit: str) -> list[tuple[str, int]]:
    completed = subprocess.run(
        ["git", "-C", str(source_root), "ls-tree", "-r", "-z", "--long", commit],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or "cannot list upstream Git tree"
        )
    entries = []
    for raw_entry in completed.stdout.split(b"\x00"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
        fields = metadata.split()
        if len(fields) != 4 or fields[1] != b"blob":
            continue
        try:
            size = int(fields[3])
        except ValueError:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        entries.append((path, size))
    return entries


def _git_bytes(source_root: Path, commit: str, relative_path: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(source_root), "show", f"{commit}:{relative_path}"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or f"cannot read {relative_path}"
        )
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync a clean, sanitized Klonet source snapshot into knowledge/.",
    )
    parser.add_argument("--source", type=Path, default=KLONET_UPSTREAM_SOURCE_ROOT)
    parser.add_argument("--destination", type=Path, default=KLONET_SOURCE_ROOT)
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    manifest = sync_snapshot(
        source_root=args.source,
        destination=args.destination,
        revision=args.revision,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
