"""Tracked Klonet source snapshot generation."""

from __future__ import annotations

import json
import subprocess

from tests.helpers import local_temp_dir


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_sync_snapshot_uses_clean_commit_filters_noise_and_redacts(monkeypatch):
    from klonet_agent.scripts import sync_klonet_source

    with local_temp_dir() as temp_dir:
        source = temp_dir / "upstream"
        destination = temp_dir / "agent" / "knowledge" / "klonet_source"
        source.mkdir()
        _git(source, "init")
        _git(source, "config", "user.email", "test@example.com")
        _git(source, "config", "user.name", "Test")

        tracked = source / "webserver" / "api.py"
        tracked.parent.mkdir(parents=True)
        tracked.write_text(
            "API_KEY = 'committed-secret'\n"
            "def route():\n"
            "    return '/master/topo/'\n",
            encoding="utf-8",
        )
        vendor = source / "static_resources" / "vendor.c"
        vendor.parent.mkdir(parents=True)
        vendor.write_text("int vendor(void) { return 1; }\n", encoding="utf-8")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "fixture")
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        tracked.write_text("dirty deployment-only content\n", encoding="utf-8")
        (source / "local.py").write_text("LOCAL = True\n", encoding="utf-8")
        monkeypatch.setattr(
            sync_klonet_source,
            "PROJECT_ROOT",
            temp_dir / "agent",
        )

        manifest = sync_klonet_source.sync_snapshot(source, destination)
        snapshot_text = (destination / "webserver" / "api.py").read_text(
            encoding="utf-8"
        )
        stored_manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )

    assert manifest["upstream_commit"] == commit
    assert manifest["redacted_file_count"] == 1
    assert stored_manifest == manifest
    assert "committed-secret" not in snapshot_text
    assert "[REDACTED]" in snapshot_text
    assert "dirty deployment-only content" not in snapshot_text
    assert not (destination / "local.py").exists()
    assert not (destination / "static_resources").exists()
