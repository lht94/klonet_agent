"""项目 Markdown 状态机。

ProjectJournal 面向两类读者：
1. 同学：复盘自己怎么开发、哪里卡住、测试结果是什么。
2. 老师：快速判断进度、功能差异和验收风险。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from klonet_agent.config import JOURNAL_DIR
from klonet_agent.journal.templates import PROJECT_JOURNAL_TEMPLATE
from klonet_agent.knowledge.vector_index import cosine_similarity
from klonet_agent.session import AgentSession


_UTC8 = timezone(timedelta(hours=8))
EmbeddingProvider = Callable[[str], Sequence[float]]


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
        elif goal:
            self.merge_goal(goal)
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

    def append_event_once(self, section: str, content: str) -> str:
        """向指定章节追加事件，已存在同内容时跳过。"""

        self.ensure()
        normalized = " ".join(content.strip().split())
        if not normalized:
            return f"项目日志无需更新：{self.path}"
        text = self.path.read_text(encoding="utf-8")
        compact_text = " ".join(text.split())
        if normalized in compact_text:
            return f"项目日志已有该事件：{self.path}"
        return self.append_event(section, content)

    def merge_goal(
        self,
        goal: str,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> str:
        """合并项目目标：占位时替换，已有目标时去重追加。"""

        self.ensure()
        cleaned = goal.strip()
        if not cleaned:
            return f"项目目标无需更新：{self.path}"
        text = self.path.read_text(encoding="utf-8")
        old_block = "## 项目目标\n待补充。"
        if old_block in text:
            text = text.replace(old_block, f"## 项目目标\n{cleaned}", 1)
            self.path.write_text(text, encoding="utf-8")
            return f"项目目标已更新：{self.path}"
        compact_text = " ".join(text.split())
        compact_goal = " ".join(cleaned.split())
        if compact_goal in compact_text:
            return f"项目目标已存在：{self.path}"
        if _has_similar_goal(text, cleaned, embedding_provider=embedding_provider):
            return f"项目目标已有相似内容：{self.path}"
        goal_line = cleaned if cleaned.startswith("- ") else f"- {cleaned}"
        text = _append_under_heading(text, "项目目标", f"\n{goal_line}\n")
        self.path.write_text(text, encoding="utf-8")
        return f"项目目标已追加：{self.path}"

    def update_goal_if_placeholder(self, goal: str) -> str:
        """兼容旧调用：现在会合并目标而不是只填占位。"""

        return self.merge_goal(goal)

    def update_next_step_if_placeholder(self, next_step: str) -> str:
        """仅当下一步仍是占位内容时补写下一步。"""

        self.ensure()
        cleaned = next_step.strip()
        if not cleaned:
            return f"下一步无需更新：{self.path}"
        text = self.path.read_text(encoding="utf-8")
        old_block = "## 下一步\n待补充。"
        if old_block not in text:
            return f"下一步已存在：{self.path}"
        text = text.replace(old_block, f"## 下一步\n{cleaned}", 1)
        self.path.write_text(text, encoding="utf-8")
        return f"下一步已更新：{self.path}"

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


def _has_similar_goal(
    text: str,
    goal: str,
    threshold: float = 0.72,
    embedding_provider: EmbeddingProvider | None = None,
) -> bool:
    """判断项目目标章节中是否已有语义上高度相似的目标。"""

    existing_goals = _extract_goal_items(text)
    if embedding_provider is not None and _has_similar_goal_by_embedding(
        existing_goals,
        goal,
        embedding_provider,
    ):
        return True
    goal_terms = _goal_terms(goal)
    if not goal_terms:
        return False
    for existing in existing_goals:
        existing_terms = _goal_terms(existing)
        if not existing_terms:
            continue
        overlap = len(goal_terms & existing_terms)
        smaller = min(len(goal_terms), len(existing_terms))
        union = len(goal_terms | existing_terms)
        containment = overlap / smaller if smaller else 0.0
        jaccard = overlap / union if union else 0.0
        if containment >= threshold or jaccard >= 0.58:
            return True
    return False


def _has_similar_goal_by_embedding(
    existing_goals: list[str],
    goal: str,
    embedding_provider: EmbeddingProvider,
    threshold: float = 0.88,
) -> bool:
    """用 embedding 余弦相似度判断目标是否语义重复。"""

    try:
        goal_embedding = tuple(float(value) for value in embedding_provider(goal))
    except Exception:
        return False
    if not goal_embedding:
        return False
    for existing in existing_goals:
        try:
            existing_embedding = tuple(
                float(value) for value in embedding_provider(existing)
            )
        except Exception:
            continue
        if cosine_similarity(goal_embedding, existing_embedding) >= threshold:
            return True
    return False


def _extract_goal_items(text: str) -> list[str]:
    marker = "## 项目目标"
    if marker not in text:
        return []
    start = text.index(marker) + len(marker)
    next_heading = text.find("\n## ", start)
    block = text[start:next_heading] if next_heading != -1 else text[start:]
    goals = []
    for line in block.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned == "待补充。":
            continue
        if cleaned.startswith("- "):
            cleaned = cleaned[2:].strip()
        goals.append(cleaned)
    return goals


def _goal_terms(value: str) -> set[str]:
    text = value.lower()
    for char in "，。；：、,.!！？?（）()[]【】<>《》\"'`":
        text = text.replace(char, " ")
    raw_parts = [part.strip() for part in text.split() if part.strip()]
    terms = set(raw_parts)
    for keyword in [
        "klonet",
        "worker",
        "master",
        "redis",
        "mysql",
        "ovs",
        "kvm",
        "拓扑",
        "容器",
        "数据",
        "持久化",
        "断电",
        "恢复",
        "重启",
        "架构",
        "升级",
        "调度",
        "切分",
        "链路",
        "状态",
        "同步",
        "进度",
        "聚合",
        "机制",
        "研究",
        "分析",
        "设计",
        "实现",
    ]:
        if keyword in text:
            terms.add(keyword)
    return {term for term in terms if term not in {"的", "和", "与", "及", "the", "and"}}
