"""持久化记忆存储。

从旧版 history.py 迁移到这里，负责 MEMORY.md、USER.md、history.jsonl 等文件的读写。
它关注“agent 记住了什么”，不负责具体项目的 Markdown 开发日志。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from klonet_agent.config import (
    MEMORY_DIR,
    SHARED_OPS_MEMORY_RECENT_DAYS,
    SHARED_OPS_MEMORY_SEARCH_LIMIT,
)


_UTC8 = timezone(timedelta(hours=8))


class MemoryStore:
    """三层记忆的文件存储器。

    当前记忆系统分为：
    1. 工作记忆：history.jsonl 中尚未压缩的对话原文。
    2. 情景记忆：每天一个 YYYY-MM-DD.md，记录阶段性事件摘要。
    3. 长期记忆：MEMORY.md 和 USER.md，常驻系统提示词。
    """

    def __init__(
        self,
        memory_dir: Path,
        user_file: Path,
        shared_dir: Path | None = None,
    ):
        self.memory_dir = memory_dir
        self.memory_file = memory_dir / "MEMORY.md"
        self.history_file = memory_dir / "history.jsonl"
        self.user_file = user_file
        self.shared_dir = shared_dir
        self._ensure()

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
        shared_dir = memory_root / "shared" / "ops"
        return cls(project_dir, user_file, shared_dir=shared_dir)

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
        if self.shared_dir is not None:
            self.shared_dir.mkdir(parents=True, exist_ok=True)

    def append_history(self, msg_dict: dict[str, Any]):
        """追加一条工作记忆。"""

        row = {
            "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
        }
        row.update(_json_safe(msg_dict))
        with self.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def today_episode_path(self) -> Path:
        """返回今天的情景记忆文件路径。"""

        date = datetime.now(_UTC8).strftime("%Y-%m-%d")
        return self.memory_dir / f"{date}.md"

    def read_today_episode(self) -> str:
        """读取今天的情景记忆。"""

        path = self.today_episode_path()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def append_episode(self, content: str):
        """向今天的情景记忆追加内容。"""

        _append_markdown_once(self.today_episode_path(), content)

    def shared_episode_path(self) -> Path:
        """返回多用户共享 Ops 情景记忆路径。"""

        base = self.shared_dir or (self.memory_dir / "shared" / "ops")
        date = datetime.now(_UTC8).strftime("%Y-%m-%d")
        return base / f"{date}.md"

    def shared_ops_baseline_path(self) -> Path:
        """Return the semi-permanent shared Ops environment baseline path."""

        base = self.shared_dir or (self.memory_dir / "shared" / "ops")
        return base / "BASELINE.md"

    def write_shared_ops_baseline(self, content: str):
        """Overwrite the shared Ops baseline snapshot."""

        path = self.shared_ops_baseline_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(_UTC8).isoformat(timespec="seconds")
        body = "\n".join(
            [
                "# Ops 半永久环境基线",
                "",
                f"- updated_at: {stamp}",
                "",
                content.strip(),
                "",
            ]
        )
        path.write_text(body, encoding="utf-8")

    def read_shared_ops_baseline(self) -> str:
        """Read the semi-permanent shared Ops environment baseline."""

        path = self.shared_ops_baseline_path()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def append_shared_episode(self, content: str):
        """向多用户共享 Ops 情景记忆追加已验证工具证据。"""

        _append_markdown_once(self.shared_episode_path(), content)

    def read_shared_memory(self) -> str:
        """读取最近的多用户共享 Ops 情景记忆。"""

        base = self.shared_dir or (self.memory_dir / "shared" / "ops")
        if not base.exists():
            return ""
        chunks = []
        for path in sorted(base.glob("*.md"))[-3:]:
            chunks.append(path.read_text(encoding="utf-8"))
        return "\n\n".join(chunks)

    def append_shared_ops_record(
        self,
        *,
        question: str,
        intent: str,
        target: str,
        tools: list[str],
        evidence: list[str],
        conclusion: str,
        confidence: str,
        caveat: str,
    ):
        """Append one structured Ops diagnosis record to shared daily memory."""

        now = datetime.now(_UTC8).isoformat(timespec="seconds")
        tool_text = ", ".join(tools) if tools else "none"
        evidence_lines = evidence or ["no reusable evidence captured"]
        block = "\n".join(
            [
                "## Ops 诊断记录",
                f"- time: {now}",
                f"- question: {_one_line(question, 500)}",
                f"- intent: {_one_line(intent, 160)}",
                f"- target: {_one_line(target, 160)}",
                f"- tools: {tool_text}",
                "- evidence:",
                *[f"  - {_one_line(item, 260)}" for item in evidence_lines[:8]],
                f"- conclusion: {_one_line(conclusion, 700)}",
                f"- confidence: {_one_line(confidence, 80)}",
                f"- caveat: {_one_line(caveat, 260)}",
            ]
        )
        self.append_shared_episode(block)

    def read_shared_memory(
        self,
        *,
        today: str | None = None,
        recent_days: int = SHARED_OPS_MEMORY_RECENT_DAYS,
    ) -> str:
        """Read recent shared Ops memory that is safe to inject automatically."""

        base = self.shared_dir or (self.memory_dir / "shared" / "ops")
        if not base.exists():
            return ""
        today_date = _parse_date(today) if today else datetime.now(_UTC8).date()
        if today_date is None:
            today_date = datetime.now(_UTC8).date()
        day_count = max(1, recent_days)
        start_date = today_date - timedelta(days=day_count - 1)
        chunks = []
        for path in sorted(base.glob("*.md")):
            file_date = _parse_date(path.stem)
            if file_date is None or file_date < start_date or file_date > today_date:
                continue
            chunks.append(path.read_text(encoding="utf-8"))
        return "\n\n".join(chunks)

    def search_shared_memory(
        self,
        query: str,
        max_results: int = SHARED_OPS_MEMORY_SEARCH_LIMIT,
    ) -> str:
        """Search all shared Ops memories, including archived days."""

        base = self.shared_dir or (self.memory_dir / "shared" / "ops")
        if not base.exists():
            return "未检索到匹配的共享 Ops 记忆。"
        terms = _search_terms(query)
        scored = []
        for path in sorted(base.glob("*.md"), reverse=True):
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            score = sum(1 for term in terms if term in lowered)
            if score <= 0:
                continue
            scored.append((score, path.name, text))
        if not scored:
            return "未检索到匹配的共享 Ops 记忆。"
        chunks = [
            "以下是历史共享 Ops 记忆检索结果，只能作为线索；用于当前诊断前必须再用本轮工具确认："
        ]
        for _, name, text in sorted(scored, key=lambda item: (-item[0], item[1]))[
            : max(1, max_results)
        ]:
            chunks.append(f"\n## {name}\n{_truncate_text(text.strip(), 1200)}")
        return "\n".join(chunks)

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

    def append_compact_marker(self):
        """在 history.jsonl 中打上压缩归档标记。"""

        row = {
            "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
            "type": "compact_event",
        }
        with self.history_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def load_unarchived_history(self, max_messages: int = 20) -> list[dict[str, Any]]:
        """返回最新的工作记忆。"""

        if not self.history_file.exists():
            return []
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
                msg = {key: value for key, value in row.items() if key != "ts"}
                unarchived_reversed.append(msg)
                if max_messages > 0 and len(unarchived_reversed) >= max_messages:
                    break

        messages = sanitize_openai_tool_history(list(reversed(unarchived_reversed)))
        while messages and messages[0].get("role") == "tool":
            messages.pop(0)
        return messages

    def memory_prompt(self, mode: str = "ops") -> str:
        """生成记忆系统提示词，让大模型知道如何主动维护记忆。"""

        current_memory = self.read_memory()
        current_user = self.read_user()
        shared_ops_baseline = self.read_shared_ops_baseline()
        shared_ops_memory = self.read_shared_memory()
        shared_ops_policy = (
            f"仅自动注入最近 {SHARED_OPS_MEMORY_RECENT_DAYS} 天的共享 Ops 诊断记录；"
            "更早记录需要通过 search_shared_ops_memory 按需检索。"
            "共享记忆是历史线索，不是当前事实，使用前必须结合本轮工具结果确认。"
        )
        if (mode or "").strip().lower() != "ops":
            shared_ops_baseline = ""
            shared_ops_memory = ""
            shared_ops_policy = "当前模式不注入共享 Ops 环境记忆；如需读取服务器运行态，请切换到 Ops 模式。"
        return f"""
            【当前长期记忆 (MEMORY.md)】
            {current_memory}

            【多用户共享 Ops 情景记忆】
            {shared_ops_policy}
            {shared_ops_memory}

            【多用户共享 Ops 半永久环境基线】
            这部分来自 inspect_ops_context 的 baseline 快照，可作为 Ubuntu/内核/架构/CPU/内存/磁盘/虚拟化/工具版本等低频变化事实的起点；涉及端口、进程、服务、screen、容器等运行态问题时仍必须刷新 runtime。
            {shared_ops_baseline}

            【用户画像与偏好 (USER.md)】
            {current_user}

            【记忆维护规则】
            只有出现可复用的重要进展时才使用记忆工具，普通问答不要写记忆：
            1. 完成具体任务或解决 Bug 后，可以调用 append_episode 记录。
            2. 项目长期目标或关键事实确实变化时，才调用 write_memory。
            3. 用户明确表达稳定偏好时，才调用 write_user。
            4. 不要把临时问题、工具报错或其他项目的内容写入当前会话记忆。
            5. 多用户共享 Ops 情景记忆只作为工具证据索引；如果它与本轮工具结果冲突，以本轮工具结果为准。
            """


def _append_markdown_once(path: Path, content: str):
    """Append a markdown block if it is not already present."""

    existing = (
        path.read_text(encoding="utf-8")
        if path.exists()
        else f"# {path.stem} 情景记忆\n"
    )
    block = content.strip()
    if not block or block in existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip() + "\n\n" + block + "\n", encoding="utf-8")


def _parse_date(value: str | None):
    """Parse YYYY-MM-DD dates from shared memory file names."""

    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _search_terms(query: str) -> list[str]:
    """Build simple terms for lightweight markdown memory retrieval."""

    lowered = (query or "").strip().lower()
    terms = []
    for part in lowered.replace("/", " ").replace("_", " ").split():
        if part and part not in terms:
            terms.append(part)
    if lowered and lowered not in terms:
        terms.append(lowered)
    return terms or [lowered]


def _truncate_text(value: str, max_chars: int) -> str:
    """Return a bounded text snippet."""

    if len(value) <= max_chars:
        return value
    return value[: max_chars - 20].rstrip() + "\n...（已截断）"


def _one_line(value: str, max_chars: int) -> str:
    """Collapse markdown/newlines for structured memory fields."""

    text = " ".join((value or "").split())
    return _truncate_text(text, max_chars)


def _safe_path_component(value: str) -> str:
    """把用户输入转换成安全、稳定的目录名。"""

    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in (value or "default").strip()
    )
    return cleaned or "default"


def _iter_jsonl_lines_reverse(path: Path, chunk_size: int = 8192):
    """从 JSONL 文件尾部开始逐行读取。"""

    with path.open("rb") as file:
        file.seek(0, 2)
        file_size = file.tell()
        if file_size == 0:
            return

        buffer = b""
        pos = file_size
        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            file.seek(pos)
            chunk = file.read(read_size)
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
        return [_json_safe(item) for item in obj]
    if isinstance(obj, dict):
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


def sanitize_openai_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop invalid assistant/tool fragments before sending history to OpenAI.

    OpenAI requires an assistant message with tool_calls to be followed
    immediately by one tool message for each tool_call_id. Long-running Ops
    steps can be interrupted after the assistant tool_calls were persisted but
    before the tool result was appended. Loading that fragment later would make
    the next request fail with a 400, so we discard incomplete fragments.
    """

    sanitized: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            expected_ids = [
                str(tool_call.get("id") or "")
                for tool_call in tool_calls
                if isinstance(tool_call, dict)
            ]
            if not expected_ids:
                index += 1
                continue
            following = messages[index + 1 : index + 1 + len(expected_ids)]
            following_ids = [
                str(item.get("tool_call_id") or "")
                for item in following
                if item.get("role") == "tool"
            ]
            if following_ids == expected_ids:
                sanitized.append(message)
                sanitized.extend(following)
                index += 1 + len(expected_ids)
                continue
            index += 1
            continue
        if role == "tool":
            index += 1
            continue
        sanitized.append(message)
        index += 1
    return sanitized


MEMORY_STORE = MemoryStore.for_session(MEMORY_DIR, "default", "default")
