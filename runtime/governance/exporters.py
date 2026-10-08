"""治理事件的导出器。

计划 §4.4：JSONL trace 降级为**人类可读的导出与灾备格式**，不再承担
并发一致性，也绝不参与业务读取。导出失败永远不影响权威链路。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def event_to_export_row(event) -> dict[str, Any]:
    """把 ``RuntimeEvent`` 压平成导出行。"""

    payload = event.payload if isinstance(event.payload, dict) else {}
    return {
        "ts": event.occurred_at.isoformat(timespec="seconds"),
        "event": event.event_type,
        "schema_version": event.schema_version,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "task_id": event.task_id,
        "step_id": event.step_id,
        "event_id": event.event_id,
        "parent_event_id": event.parent_event_id,
        "idempotency_key": event.idempotency_key,
        "actor_type": str(event.actor_type),
        "actor_id": event.actor_id,
        "reason_code": payload.get("reason_code"),
        "privacy_class": str(event.privacy_class),
        "payload": payload,
    }


class JsonlEventExporter:
    """把治理事件追加导出为 JSONL。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def export(self, event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = event_to_export_row(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
