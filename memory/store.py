"""持久化记忆存储。

从旧版 history.py 迁移到这里，负责 MEMORY.md、USER.md、history.jsonl 等文件的读写。
它关注“agent 记住了什么”，不负责具体项目的 Markdown 开发日志。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from klonet_agent.config import MEMORY_DIR


# 获取北京时间。datetime 默认是本机时间，这里显式指定 UTC+8，保证记忆日志时间稳定。
_UTC8 = timezone(timedelta(hours=8))


class MemoryStore:
    """三层记忆的文件存储器。

    当前记忆系统分为：
    1. 工作记忆：history.jsonl 中尚未压缩的对话原文。
    2. 情景记忆：每天一个 YYYY-MM-DD.md，记录阶段性事件摘要。
    3. 长期记忆：MEMORY.md 和 USER.md，常驻系统提示词。
    """

    def __init__(self, memory_dir: Path, user_file: Path):
        self.memory_dir = memory_dir
        self.memory_file = memory_dir / "MEMORY.md"  # 长期记忆文件
        self.history_file = memory_dir / "history.jsonl"  # 工作记忆文件
        self.user_file = user_file  # 用户画像文件
        self._ensure()  # 如果没有记忆文件，则初始化

    @classmethod
    def for_session(
        cls,
        memory_root: Path,
        user_id: str,
        project_id: str,
    ) -> "MemoryStore":
        """按用户和项目创建隔离的记忆存储器。"""

        user_name = _safe_path_component(user_id)
        project_name = _safe_path_component(project_id)
        project_dir = memory_root / "sessions" / user_name / project_name
        user_file = memory_root / "users" / user_name / "USER.md"
        return cls(project_dir, user_file)

    """-------------下面是记忆文件读取的实现----------------"""

    def _ensure(self):
        """初始化记忆目录和必要文件。"""

        self.memory_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            self.memory_file.write_text(
                "# 长期记忆\n\n此文件常驻上下文，记录核心目标、当前任务与关键事实。\n",
                encoding="utf-8",
            )
        if not self.history_file.exists():
            self.history_file.write_text("", encoding="utf-8")

    def append_history(self, msg_dict: dict[str, Any]):
        """追加一条工作记忆。

        比早期版本多了当前时间戳，方便后续审计和复盘。
        """

        row = {
            "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
        }
        # 使用 update 将消息字典里的所有字段
        # role、content、tool_call_id、tool_calls、reasoning_content 等平铺写入 row。
        row.update(_json_safe(msg_dict))
        # "a" 表示 append 追加写入。jsonl 每行是一条 JSON，适合持续增长的日志。
        with self.history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def today_episode_path(self) -> Path:
        """返回今天的情景记忆文件路径。

        情景记忆按天创建，文件名形如 2026-06-16.md。
        """

        date = datetime.now(_UTC8).strftime("%Y-%m-%d")
        return self.memory_dir / f"{date}.md"

    def read_today_episode(self) -> str:
        """读取今天的情景记忆。"""

        path = self.today_episode_path()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def append_episode(self, content: str):
        """向今天的情景记忆追加内容。"""

        path = self.today_episode_path()
        existing = (
            path.read_text(encoding="utf-8")
            if path.exists()
            else f"# {path.stem} 情景记忆\n"
        )
        # strip() 去掉首尾空白，保证 Markdown 版式稳定。
        new_text = existing.rstrip() + "\n\n" + content.strip() + "\n"
        path.write_text(new_text, encoding="utf-8")

    def read_memory(self) -> str:
        """读取长期记忆 MEMORY.md。"""

        return self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""

    def write_memory(self, content: str):
        """覆盖写入长期记忆 MEMORY.md。"""

        self.memory_file.write_text(content.strip() + "\n", encoding="utf-8")

    def read_user(self) -> str:
        """读取用户画像 USER.md。"""

        return self.user_file.read_text(encoding="utf-8") if self.user_file.exists() else ""

    def write_user(self, content: str):
        """覆盖写入用户画像 USER.md。"""

        self.user_file.write_text(content.strip() + "\n", encoding="utf-8")

    """-------------下面是记忆压缩机制的实现----------------

    具体流程：
    检测溢出 -> 工作记忆写入标记 -> 工作记忆只读取标记后的内容 -> 标记前的内容写入情景记忆。
    """

    def append_compact_marker(self):
        """在 history.jsonl 中打上压缩归档标记。"""

        row = {
            "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
            "type": "compact_event",
        }
        with self.history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def load_unarchived_history(self, max_messages: int = 20) -> list[dict[str, Any]]:
        """返回最新的工作记忆。

        只加载最后一个 compact_event 之后的对话，避免每次启动都把完整历史塞进上下文。
        """

        if not self.history_file.exists():
            return []
        # 倒序扫描文件：找到最后一个 compact_event 后立刻停止，避免全量正序加载。
        unarchived_reversed = []
        for line in _iter_jsonl_lines_reverse(self.history_file):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            if row.get("type") == "compact_event":
                break
            if "role" in row:
                # 绝不硬编码 role 和 content！直接提取除了时间戳 ts 以外的所有完整字段。
                msg = {key: value for key, value in row.items() if key != "ts"}
                unarchived_reversed.append(msg)
                if max_messages > 0 and len(unarchived_reversed) >= max_messages:
                    break

        # 倒序收集后再翻转回时间正序。
        messages = list(reversed(unarchived_reversed))
        # 被截断的历史不能从孤立 tool 消息开始，否则模型 API 会拒绝上下文。
        while messages and messages[0].get("role") == "tool":
            messages.pop(0)
        return messages

    def memory_prompt(self) -> str:
        """生成记忆系统提示词，让大模型知道如何主动维护记忆。"""

        current_memory = self.read_memory()
        current_user = self.read_user()
        return f"""
            【当前长期记忆 (MEMORY.md)】
            {current_memory}

            【用户画像与偏好 (USER.md)】
            {current_user}

            【记忆维护规则】
            只有出现可复用的重要进展时才使用记忆工具，普通问答不要写记忆：
            1. 完成具体任务或解决 Bug 后，可以调用 append_episode 记录。
            2. 项目长期目标或关键事实确实变化时，才调用 write_memory。
            3. 用户明确表达稳定偏好时，才调用 write_user。
            4. 不要把临时问题、工具报错或其他项目的内容写入当前会话记忆。
            """


def _safe_path_component(value: str) -> str:
    """把用户输入转换成安全、稳定的目录名。"""

    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in (value or "default").strip()
    )
    return cleaned or "default"


def _iter_jsonl_lines_reverse(path: Path, chunk_size: int = 8192):
    """从 JSONL 文件尾部开始逐行读取。

    这样不需要把整个 history.jsonl 一次性加载进内存。
    """

    with path.open("rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        if file_size == 0:
            return

        buffer = b""
        pos = file_size
        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            buffer = chunk + buffer

            parts = buffer.split(b"\n")
            buffer = parts[0]
            for raw in reversed(parts[1:]):
                line = raw.rstrip(b"\r")
                if line:
                    yield line.decode("utf-8", errors="ignore")

        tail = buffer.rstrip(b"\r")
        if tail:
            yield tail.decode("utf-8", errors="ignore")


def _json_safe(obj: Any):
    """把任意对象尽量转换成 JSON 可序列化格式，避免写入 history.jsonl 时报错。"""

    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except (TypeError, ValueError):
        pass
    if isinstance(obj, list):
        # 递归处理列表里的每一个元素。
        return [_json_safe(item) for item in obj]
    if isinstance(obj, dict):
        # 递归处理字典里的每一个值。
        return {key: _json_safe(value) for key, value in obj.items()}
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return {
            key: _json_safe(value)
            for key, value in obj.__dict__.items()
            if not key.startswith("_")
        }
    return str(obj)


# 保留默认实例兼容旧调用；正式会话由 Orchestrator 按用户和项目创建实例。
MEMORY_STORE = MemoryStore.for_session(MEMORY_DIR, "default", "default")
