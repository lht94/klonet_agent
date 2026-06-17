"""项目 Markdown 状态机。

ProjectJournal 面向两类读者：
1. 同学：复盘自己怎么开发、哪里卡住、测试结果是什么。
2. 老师：快速判断进度、功能差异和验收风险。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from klonet_agent.config import JOURNAL_DIR
from klonet_agent.journal.templates import PROJECT_JOURNAL_TEMPLATE
from klonet_agent.session import AgentSession


_UTC8 = timezone(timedelta(hours=8))


class ProjectJournal:
    """负责读写单个项目的 Markdown 状态文件。"""

    def __init__(self, path: Path, user_id: str, project_id: str):
        self.path = path
        self.user_id = user_id
        self.project_id = project_id

    @classmethod
    def from_session(cls, session: AgentSession) -> "ProjectJournal":
        """从会话创建项目日志对象。"""

        return cls(session.journal_path, session.user_id, session.project_id)

    def ensure(self, goal: str | None = None) -> Path:
        """确保日志文件存在。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            text = PROJECT_JOURNAL_TEMPLATE.format(
                user_id=self.user_id,
                project_id=self.project_id,
            )
            if goal:
                text = text.replace("## 项目目标\n待补充。", f"## 项目目标\n{goal.strip()}")
            self.path.write_text(text, encoding="utf-8")
        return self.path

    def read(self) -> str:
        """读取完整项目日志。"""

        self.ensure()
        return self.path.read_text(encoding="utf-8")

    def summary(self, max_chars: int = 3000) -> str:
        """生成适合注入上下文的项目日志摘要。"""

        text = self.read()
        lines = [
            "# 项目日志摘要",
            f"- 用户 ID：{self.user_id}",
            f"- 项目 ID：{self.project_id}",
        ]
        for prefix in ["- 当前状态：", "## 项目目标", "## 下一步"]:
            value = _extract_summary_block(text, prefix)
            if value:
                lines.append(value)

        summary = "\n".join(lines).strip()
        if len(summary) <= max_chars:
            return summary
        return summary[: max_chars - 20].rstrip() + "\n...（已截断）"

    def append_event(self, section: str, content: str) -> str:
        """向指定章节追加一条带时间的事件。"""

        self.ensure()
        text = self.path.read_text(encoding="utf-8")
        title = _normalize_section(section)
        stamp = datetime.now(_UTC8).strftime("%Y-%m-%d %H:%M")
        event = f"\n- {stamp}：{content.strip()}\n"
        if f"## {title}" not in text:
            text = text.rstrip() + f"\n\n## {title}\n"
        text = _append_under_heading(text, title, event)
        self.path.write_text(text, encoding="utf-8")
        return f"项目日志已更新：{self.path}"

    def update_status(self, status: str) -> str:
        """更新基本信息里的当前状态。"""

        self.ensure()
        text = self.path.read_text(encoding="utf-8")
        lines = []
        replaced = False
        for line in text.splitlines():
            if line.startswith("- 当前状态："):
                lines.append(f"- 当前状态：{status.strip()}")
                replaced = True
            else:
                lines.append(line)
        if not replaced:
            lines.insert(3, f"- 当前状态：{status.strip()}")
        self.path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return f"项目状态已更新为：{status}"

    def record_test_result(self, content: str) -> str:
        """记录测试与验证结果。"""

        return self.append_event("测试与验证", content)

    def record_acceptance_gap(self, content: str) -> str:
        """记录功能差异与验收建议。"""

        return self.append_event("功能差异与验收建议", content)


def get_project_journal(
    user_id: str = "default",
    project_id: str = "default",
) -> ProjectJournal:
    """按 user_id/project_id 获取日志对象。"""

    return ProjectJournal(
        JOURNAL_DIR / user_id / f"{project_id}.md",
        user_id,
        project_id,
    )


def _normalize_section(section: str) -> str:
    """把工具输入的章节名收敛到模板里的固定章节。"""

    aliases = {
        "event": "执行记录",
        "events": "执行记录",
        "plan": "开发计划",
        "issue": "遇到的问题",
        "problem": "遇到的问题",
        "test": "测试与验证",
        "acceptance": "功能差异与验收建议",
        "next": "下一步",
    }
    stripped = (section or "执行记录").strip()
    return aliases.get(stripped.lower(), stripped)


def _append_under_heading(text: str, heading: str, event: str) -> str:
    """把事件追加到指定二级标题下。"""

    marker = f"## {heading}"
    start = text.index(marker)
    next_heading = text.find("\n## ", start + len(marker))
    if next_heading == -1:
        return text.rstrip() + event
    before = text[:next_heading].rstrip()
    after = text[next_heading:]
    return before + event + after


def _extract_summary_block(text: str, marker: str) -> str:
    """提取摘要用的关键片段。"""

    if marker.startswith("- "):
        for line in text.splitlines():
            if line.startswith(marker):
                return line
        return ""

    if marker not in text:
        return ""
    start = text.index(marker)
    next_heading = text.find("\n## ", start + len(marker))
    block = text[start:next_heading] if next_heading != -1 else text[start:]
    lines = [line for line in block.splitlines() if line.strip()]
    return "\n".join(lines[:4])
