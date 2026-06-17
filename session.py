"""一次用户会话的状态与生命周期。"""

from __future__ import annotations

from pathlib import Path

from klonet_agent.config import (
    DEFAULT_MODE,
    DEFAULT_PROJECT_ID,
    DEFAULT_USER_ID,
    JOURNAL_DIR,
    WORKSPACE_DIR,
)

# 任务状态集合：未做、正在做、已完成。
VALID_STATUS = {"pending", "in_progress", "completed"}

# 状态符号转换表，把任务的三种状态映射成命令行里更直观的文本符号。
STATUS_ICON = {"pending": "[]", "in_progress": "[~]", "completed": "[x]"}


class AgentSession:
    """一次用户任务的状态容器。

    这里是多用户隔离的第一层：用户、项目、workspace、journal 和 todo 都挂在会话上，
    避免不同同学之间共享全局任务状态。
    """

    def __init__(
        self,
        user_id: str = DEFAULT_USER_ID,
        project_id: str = DEFAULT_PROJECT_ID,
        mode: str = DEFAULT_MODE,
        workspace_path: Path | None = None,
        journal_path: Path | None = None,
    ):
        self.user_id = user_id
        self.project_id = project_id
        self.mode = mode
        self.history: list[dict] = []
        self.token_total = 0
        self.loaded_skills: list[str] = []
        self.todos: list[dict] = []
        self.workspace_path = workspace_path or WORKSPACE_DIR / user_id / project_id
        self.journal_path = journal_path or JOURNAL_DIR / user_id / f"{project_id}.md"


def render_todos(todos: list[dict]) -> str:
    """把 todo 列表渲染成命令行可读文本。"""

    if not todos:
        return "()"
    lines = []
    for todo in todos:
        # 获取当前任务状态对应的图标。
        icon = STATUS_ICON.get(todo.get("status", "pending"), "[?]")
        # 打印一行：状态图标 + id + 任务内容。
        lines.append(f"{icon} {todo.get('id')}. {todo.get('content', '')}")
    # 用换行符把每一行拼接起来。
    return "\n".join(lines)


    def update_todos(self, todos: list[dict]) -> str:
        """更新当前会话的任务进度。"""

        return update_todos(self.todos, todos)


def update_todos(target: list[dict], todos: list[dict]) -> str:
    """更新任务进度，并对模型输出做二次校验。

    模型输出的 todos 通常能直接使用，这里额外做格式清洗、状态校验和 in_progress 数量校验。
    """

    cleaned = []
    # enumerate 可以给列表自动带上索引，start=1 表示从 1 开始编号。
    for index, todo in enumerate(todos, start=1):
        # 获取任务内容，空内容直接丢弃。
        content = (todo.get("content") or "").strip()
        if not content:
            continue
        # 获取任务状态，默认为 pending。
        status = todo.get("status", "pending")
        if status not in VALID_STATUS:
            status = "pending"
        # 存储规范化后的任务：填充 id、去掉空 content、校验 status。
        cleaned.append({"id": todo.get("id", index), "content": content, "status": status})

    # 同一时间只能有一个任务处于 in_progress，避免模型同时“做多件事”。
    in_progress = [todo for todo in cleaned if todo["status"] == "in_progress"]
    if len(in_progress) > 1:
        return "Error: 同一时间只能有一个 in_progress 任务，请重新规划。"

    # 注意这里用 clear + extend 原地更新，保持外部持有的列表对象引用不变。
    target.clear()
    target.extend(cleaned)
    print("\n小白，计划更新啦！")
    print(render_todos(target))
    print()

    # 统计任务状态，返回给大模型作为工具执行结果。
    pending = [todo for todo in target if todo["status"] == "pending"]
    completed = [todo for todo in target if todo["status"] == "completed"]
    summary = (
        f"todos updated: total={len(target)}, completed={len(completed)}, "
        f"in_progress={len(in_progress)}, pending={len(pending)}"
    )
    return summary + "\n\n当前列表：\n" + render_todos(target)
