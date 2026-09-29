"""CheckpointStore 版本化原子写入测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))


def _checkpoint(**overrides):
    from klonet_agent.memory.models import TaskCheckpoint

    defaults = dict(
        checkpoint_id="cp-test-001",
        version=1,
        user_id="u1",
        project_id="p1",
        mode="mentor",
        goal="部署 Klonet 平台",
        status="in_progress",
        constraints=("不能修改 nginx 主配置",),
        decisions=("使用 Docker Compose 部署",),
        completed_steps=("完成环境探测",),
        pending_steps=("启动后端服务",),
        failed_attempts=("直接 apt install 失败：无网络",),
        evidence_refs=("inspect_ops_context",),
        touched_files=("docker-compose.yml",),
        verification=("docker ps 显示全部 healthy",),
        unresolved_questions=(),
        next_action="运行 docker compose up -d",
        source_event_start="msg-000000",
        source_event_end="msg-000042",
        created_at="2026-09-29T18:00:00+08:00",
    )
    defaults.update(overrides)
    return TaskCheckpoint(**defaults)


def _store(tmp_path):
    from klonet_agent.memory.checkpoint_store import CheckpointStore

    return CheckpointStore(tmp_path)


def test_save_and_load_latest(tmp_path):
    store = _store(tmp_path)
    assert store.load_latest() is None

    saved = store.save(_checkpoint())
    loaded = store.load_latest()
    assert loaded is not None
    assert loaded.checkpoint_id == saved.checkpoint_id
    assert loaded.goal == "部署 Klonet 平台"
    assert loaded.version == 1


def test_versions_increment_atomically(tmp_path):
    store = _store(tmp_path)
    store.save(_checkpoint(checkpoint_id="cp-1"))
    store.save(_checkpoint(checkpoint_id="cp-2", goal="更新后的目标"))

    assert store.list_versions() == [1, 2]
    loaded = store.load_latest()
    assert loaded.goal == "更新后的目标"
    assert loaded.version == 2
    # 旧版本仍在。
    assert store.load_version(1).goal == "部署 Klonet 平台"


def test_write_is_atomic_no_partial_file(tmp_path):
    """写入使用临时文件 + 原子替换，目录中不残留部分文件。"""

    store = _store(tmp_path)
    store.save(_checkpoint())
    leftovers = [
        path.name
        for path in (tmp_path / "checkpoints").iterdir()
        if path.name.startswith(".")
    ]
    assert leftovers == []


def test_corrupted_latest_falls_back_to_previous(tmp_path):
    """最新版本损坏时自动回退到上一版本。"""

    store = _store(tmp_path)
    store.save(_checkpoint(checkpoint_id="cp-1"))
    store.save(_checkpoint(checkpoint_id="cp-2", goal="第二版"))

    # 手动破坏最新版本文件。
    latest = tmp_path / "checkpoints" / "checkpoint-v2.json"
    latest.write_text("{不是合法 json", encoding="utf-8")

    loaded = store.load_latest()
    assert loaded is not None
    assert loaded.checkpoint_id == "cp-1"


def test_schema_invalid_checkpoint_is_treated_as_corrupted(tmp_path):
    """缺必填字段的 checkpoint 视同损坏，不返回。"""

    store = _store(tmp_path)
    directory = tmp_path / "checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    broken = _checkpoint(goal="", next_action="")
    (directory / "checkpoint-v1.json").write_text(broken.to_json(), encoding="utf-8")

    assert store.load_latest() is None


def test_stored_file_is_valid_json_with_list_fields(tmp_path):
    store = _store(tmp_path)
    store.save(_checkpoint())
    data = json.loads(
        (tmp_path / "checkpoints" / "checkpoint-v1.json").read_text(encoding="utf-8")
    )
    assert isinstance(data["constraints"], list)
    assert data["goal"] == "部署 Klonet 平台"
