"""记忆相关数据结构。

先把数据形状固定下来，后续从 Markdown 文件迁移到数据库时会更轻松。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TaskCheckpoint:
    """结构化任务检查点。

    压缩不再只是"记忆复盘"的自然语言回复，而是符合固定 schema 的任务状态：
    它必须能回答"当前目标、已完成什么、失败过什么、下一步是什么"。
    checkpoint 是原始事件的派生视图，不删除、不改写原始事件。
    """

    checkpoint_id: str
    version: int
    user_id: str
    project_id: str
    mode: str
    goal: str
    status: str
    constraints: tuple[str, ...]
    decisions: tuple[str, ...]
    completed_steps: tuple[str, ...]
    pending_steps: tuple[str, ...]
    failed_attempts: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    touched_files: tuple[str, ...]
    verification: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    next_action: str
    source_event_start: str
    source_event_end: str
    created_at: str = ""

    # 必填核心字段：缺失即校验失败。
    REQUIRED_TEXT_FIELDS = ("goal", "next_action", "source_event_end")

    def validate(self) -> list[str]:
        """返回校验错误列表；空列表表示合法。"""

        errors: list[str] = []
        for name in self.REQUIRED_TEXT_FIELDS:
            if not str(getattr(self, name) or "").strip():
                errors.append(f"缺少必填字段 {name}")
        if not (self.completed_steps or self.pending_steps or self.failed_attempts):
            errors.append("至少需要记录已完成、待办或失败步骤之一")
        if self.status not in {"in_progress", "blocked", "completed"}:
            errors.append("status 必须是 in_progress/blocked/completed 之一")
        return errors

    def to_dict(self) -> dict:
        """转换为 JSON 可序列化 dict。"""

        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, tuple):
                data[key] = list(value)
        return data

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""

        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskCheckpoint":
        """从 dict 构建；未知字段忽略，缺失字段给空值。"""

        known = set(cls.__dataclass_fields__)
        cleaned = {
            key: value
            for key, value in data.items()
            if key in known
        }
        for key in (
            "constraints",
            "decisions",
            "completed_steps",
            "pending_steps",
            "failed_attempts",
            "evidence_refs",
            "touched_files",
            "verification",
            "unresolved_questions",
        ):
            cleaned[key] = tuple(cleaned.get(key) or ())
        for key in (
            "checkpoint_id",
            "goal",
            "status",
            "next_action",
            "source_event_start",
            "source_event_end",
            "created_at",
        ):
            cleaned[key] = str(cleaned.get(key) or "")
        cleaned["version"] = int(cleaned.get("version") or 1)
        return cls(**cleaned)

    @classmethod
    def from_json(cls, raw: str) -> "TaskCheckpoint":
        """从 JSON 字符串构建；解析失败抛 ValueError。"""

        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("checkpoint 必须是 JSON 对象")
        return cls.from_dict(data)

    def render_system_message(self) -> dict:
        """渲染成稳定、短小的 system 消息，供编译器注入。"""

        def _lines(items: tuple[str, ...], prefix: str) -> list[str]:
            return [f"- {prefix}{item}" for item in items[:8]]

        parts = [
            "【任务检查点】以下是更早对话的压缩状态，只包含可证明的内容；"
            "与当前工具结果冲突时，以当前工具结果为准。",
            f"- 目标：{self.goal}",
            f"- 状态：{self.status}",
        ]
        parts.extend(_lines(self.constraints, "约束："))
        parts.extend(_lines(self.completed_steps, "已完成："))
        parts.extend(_lines(self.pending_steps, "待办："))
        parts.extend(_lines(self.failed_attempts, "已失败："))
        parts.extend(_lines(self.evidence_refs, "证据："))
        parts.append(f"- 下一步：{self.next_action}")
        content = "\n".join(parts)
        return {
            "role": "system",
            "checkpoint_id": self.checkpoint_id,
            "content": content,
        }
