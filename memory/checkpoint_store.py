"""结构化检查点的版本化存储。

CheckpointStore 负责原子写入、读取最新版和保存历史版本。
checkpoint 是派生视图：损坏时可以回退到上一版本，或从原始事件重新生成。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from klonet_agent.memory.models import TaskCheckpoint


class CheckpointStore:
    """按 user/project 隔离的 checkpoint 存储器。

    目录布局：
        memory_dir/checkpoints/checkpoint-v{n}.json
    n 从 1 开始单调递增，写入使用临时文件 + 原子替换。
    """

    def __init__(self, memory_dir: Path):
        self.checkpoint_dir = Path(memory_dir) / "checkpoints"

    def _ensure_dir(self) -> Path:
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        return self.checkpoint_dir

    def _versioned_path(self, version: int) -> Path:
        return self.checkpoint_dir / f"checkpoint-v{int(version)}.json"

    def list_versions(self) -> list[int]:
        """返回已保存的版本号，升序。"""

        if not self.checkpoint_dir.exists():
            return []
        versions = []
        for path in self.checkpoint_dir.glob("checkpoint-v*.json"):
            stem = path.stem
            prefix = "checkpoint-v"
            if not stem.startswith(prefix):
                continue
            try:
                versions.append(int(stem[len(prefix) :]))
            except ValueError:
                continue
        return sorted(versions)

    def next_version(self) -> int:
        """下一个可用的版本号。"""

        versions = self.list_versions()
        return (versions[-1] + 1) if versions else 1

    def save(self, checkpoint: TaskCheckpoint) -> TaskCheckpoint:
        """原子写入一个新版本；返回带最终版本号的 checkpoint。"""

        version = self.next_version()
        data = checkpoint.to_dict()
        data["version"] = version
        stored = TaskCheckpoint.from_dict(data)

        directory = self._ensure_dir()
        target = self._versioned_path(version)
        tmp = directory / f".checkpoint-v{version}.tmp"
        tmp.write_text(stored.to_json(), encoding="utf-8")
        os.replace(tmp, target)
        return stored

    def load_version(self, version: int) -> TaskCheckpoint | None:
        """读取指定版本；缺失或损坏返回 None。"""

        path = self._versioned_path(version)
        if not path.exists():
            return None
        return self._load_path(path)

    def load_latest(self) -> TaskCheckpoint | None:
        """读取最新版本；损坏时自动回退到上一版本。"""

        for version in reversed(self.list_versions()):
            checkpoint = self.load_version(version)
            if checkpoint is not None:
                return checkpoint
        return None

    def _load_path(self, path: Path) -> TaskCheckpoint | None:
        try:
            checkpoint = TaskCheckpoint.from_json(
                path.read_text(encoding="utf-8")
            )
        except (ValueError, json.JSONDecodeError, OSError):
            return None
        if checkpoint.validate():
            # schema 校验失败视同损坏，交给上层回退。
            return None
        return checkpoint
