"""工具调用与产物日志。

这里记录工具调用、命令输出、文件改动、失败原因、测试结果和最终产物。
这些日志既用于安全审计，也用于后续沉淀到知识库。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_UTC8 = timezone(timedelta(hours=8))


class TraceLogger:
    """JSONL 版 trace 记录器。"""

    def __init__(self, trace_file: Path):
        self.trace_file = trace_file

    def record_tool_call(
        self,
        user_id: str,
        project_id: str,
        mode: str,
        tool_name: str,
        status: str,
        duration_ms: int,
        args: dict | None = None,
        result: str = "",
    ):
        """记录一次工具调用。"""

        row = {
            "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
            "event": "tool_call",
            "user_id": user_id,
            "project_id": project_id,
            "mode": mode,
            "tool_name": tool_name,
            "status": status,
            "duration_ms": duration_ms,
            "args": args or {},
            "result_preview": result[:1000],
        }
        self._append(row)

    def record_llm_call(
        self,
        user_id: str,
        project_id: str,
        mode: str,
        total_tokens: int,
        duration_ms: int,
    ):
        """记录一次模型调用。"""

        self._append(
            {
                "ts": datetime.now(_UTC8).isoformat(timespec="seconds"),
                "event": "llm_call",
                "user_id": user_id,
                "project_id": project_id,
                "mode": mode,
                "total_tokens": total_tokens,
                "duration_ms": duration_ms,
            }
        )

    def _append(self, row: dict):
        """追加一行 JSONL。"""

        self.trace_file.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_json_safe(row), ensure_ascii=False) + "\n")


def _json_safe(value: Any):
    """把 trace 中的对象转换成 JSON 可序列化格式。"""

    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return str(value)
