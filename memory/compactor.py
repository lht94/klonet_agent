"""对话记忆压缩。

把一段对话历史压缩成结构化 TaskCheckpoint。压缩是"上下文进入危险区
之前的控制动作"：只生成派生 checkpoint，不删除或改写原始事件。

压缩响应必须通过 schema 校验；校验失败时最多做一次结构修复，仍失败
则保留旧 checkpoint 并由调用方决定后续动作。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from klonet_agent.memory.models import TaskCheckpoint

_UTC8 = timezone(timedelta(hours=8))


_COMPACT_INSTRUCTION = """你是任务检查点生成器。请阅读以下对话历史，输出一个严格的 JSON 对象（不要输出任何其他文字或代码块标记），schema 如下：
{
  "goal": "当前任务目标（一句话）",
  "status": "in_progress | blocked | completed 三选一",
  "constraints": ["不可违反的约束，无则空数组"],
  "decisions": ["已确定的技术/方案决策"],
  "completed_steps": ["已完成的步骤"],
  "pending_steps": ["尚未完成的步骤"],
  "failed_attempts": ["方法 + 失败原因"],
  "evidence_refs": ["结论对应的证据来源（工具名/文件/命令）"],
  "touched_files": ["涉及过的文件路径"],
  "verification": ["已做过的验证及结果"],
  "unresolved_questions": ["未解决的问题"],
  "next_action": "下一步要执行的一个具体、可执行的动作"
}
生成规范：
1. 只总结对话历史中可证明的内容；assistant 的猜测不得写成已验证事实。
2. failed_attempts 必须同时记录方法和失败原因。
3. next_action 必须是一个具体、可执行的动作。
4. 对系统运行态（进程/服务/容器等）只引用证据，不宣称当前仍然有效。
"""


class CompactionError(Exception):
    """压缩失败：模型响应无法通过 schema 校验，且没有可用旧 checkpoint。"""


class MemoryCompactor:
    """从对话历史生成结构化任务检查点。

    complete_fn 接收消息列表，返回 assistant 文本回复。
    """

    def __init__(self, complete_fn: Callable):
        self.complete_fn = complete_fn

    def compact(
        self,
        messages: list[dict],
        previous_checkpoint: TaskCheckpoint | None = None,
        *,
        user_id: str,
        project_id: str,
        mode: str,
        source_event_start: str = "",
        source_event_end: str = "",
    ) -> TaskCheckpoint:
        """把 messages 压缩为一个通过校验的 checkpoint。

        校验失败时做一次结构修复；仍失败时：
        - 有 previous_checkpoint 则返回它（版本号不变）；
        - 否则抛出 CompactionError。
        """

        history_text = _render_messages(messages)
        prompt = _COMPACT_INSTRUCTION + "\n【对话历史】\n" + history_text
        if previous_checkpoint is not None:
            prompt += (
                "\n【上一版检查点】\n"
                + json.dumps(previous_checkpoint.to_dict(), ensure_ascii=False)
                + "\n在其基础上增量更新，不要丢失仍然有效的条目。"
            )

        first = self._request_json(prompt)
        checkpoint, errors = self._build_checkpoint(
            first,
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            source_event_start=source_event_start,
            source_event_end=source_event_end,
        )
        if checkpoint is not None:
            return checkpoint

        # 最多一次结构修复。
        repair_prompt = (
            "你上一次输出的 JSON 未通过校验：\n"
            + "\n".join(f"- {error}" for error in errors)
            + "\n请重新输出完整、合法、符合 schema 的 JSON 对象，不要输出其他文字。"
        )
        second = self._request_json(repair_prompt, previous_reply=first)
        checkpoint, _ = self._build_checkpoint(
            second,
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            source_event_start=source_event_start,
            source_event_end=source_event_end,
        )
        if checkpoint is not None:
            return checkpoint

        if previous_checkpoint is not None:
            return previous_checkpoint
        raise CompactionError(
            "压缩响应两次未通过 schema 校验：\n" + "\n".join(errors)
        )

    def _request_json(self, prompt: str, previous_reply: str | None = None) -> str:
        messages: list[dict] = [
            {"role": "system", "content": "你是严谨的任务检查点生成器。"}
        ]
        if previous_reply:
            messages.append({"role": "assistant", "content": previous_reply[:2000]})
        messages.append({"role": "user", "content": prompt})
        return str(self.complete_fn(messages) or "")

    def _build_checkpoint(
        self,
        reply: str,
        *,
        user_id: str,
        project_id: str,
        mode: str,
        source_event_start: str,
        source_event_end: str,
    ) -> tuple[TaskCheckpoint | None, list[str]]:
        try:
            data = _extract_json(reply)
        except ValueError as exc:
            return None, [str(exc)]

        if not isinstance(data, dict):
            return None, ["响应不是 JSON 对象"]

        now = datetime.now(_UTC8).isoformat(timespec="seconds")
        data.setdefault("checkpoint_id", f"cp-{now.replace(':', '')}")
        data.setdefault("version", 1)
        data.setdefault("created_at", now)
        data["user_id"] = user_id
        data["project_id"] = project_id
        data["mode"] = mode
        if not str(data.get("source_event_start") or ""):
            data["source_event_start"] = source_event_start
        if not str(data.get("source_event_end") or ""):
            data["source_event_end"] = source_event_end

        try:
            checkpoint = TaskCheckpoint.from_dict(data)
        except (TypeError, ValueError) as exc:
            return None, [f"字段类型错误: {exc}"]

        errors = checkpoint.validate()
        if errors:
            return None, errors
        return checkpoint, []


def _extract_json(reply: str):
    """从模型回复中提取 JSON 对象；容忍代码块包裹。"""

    text = (reply or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个 { 到最后一个 }。
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("响应中不包含合法 JSON")


def _render_messages(messages: list[dict]) -> str:
    lines = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if not content and message.get("tool_calls"):
            calls = ", ".join(
                str(call.get("function", {}).get("name") or "")
                for call in message["tool_calls"]
                if isinstance(call, dict)
            )
            content = f"[发起工具调用: {calls}]"
        text = str(content or "")
        if len(text) > 1500:
            text = text[:1500] + "…（已截断）"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)
