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

# 任务状态集合：未做、正在做、等待、阻塞、已完成。
VALID_STATUS = {"pending", "in_progress", "completed", "waiting_user", "blocked"}

# 状态符号转换表，把任务状态映射成命令行里更直观的文本符号。
STATUS_ICON = {
    "pending": "[]",
    "in_progress": "[~]",
    "completed": "[x]",
    "waiting_user": "[?]",
    "blocked": "[!]",
}


class AgentSession:
    """一次用户任务的状态容器。

    这里是多用户隔离的第一层：用户、项目、workspace、journal 和 todo 都挂在会话上，
    避免不同同学之间共享全局任务状态。
    """

    def __init__(
        self,
        user_id: str = DEFAULT_USER_ID,             # 当前用户标识
        project_id: str = DEFAULT_PROJECT_ID,       # 当前项目标识
        mode: str = DEFAULT_MODE,                   # 当前 Agent 模式
        workspace_path: Path | None = None,         # 当前会话 workspace
        journal_path: Path | None = None,           # 当前项目日志路径
    ):
        self.user_id = user_id                                      # 用户隔离维度
        self.project_id = project_id                                # 项目隔离维度
        self.mode = mode                                            # mentor / coding
        self.history: list[dict] = []                               # 当前会话内存历史
        self.token_total = 0                                        # 当前会话累计 token
        self.loaded_skills: list[str] = []                          # 已加载技能名
        self.todos: list[dict] = []                                 # 当前会话任务列表
        # 运行治理观察者（03 计划阶段 2）：todos 落内存前先通知权威状态机。
        # 观察者抛异常 = 状态改变被拒绝（fail closed），内存列表不会更新。
        # 默认 None：治理层是可选部署件，关闭时主链路逐字不变。
        self.on_todos_updated = None
        self.workspace_path = workspace_path or WORKSPACE_DIR / user_id / project_id
        self.journal_path = journal_path or JOURNAL_DIR / user_id / f"{project_id}.md"

    def update_todos(self, todos: list[dict]) -> str:
        """更新当前会话的任务进度。"""

        return update_todos(self.todos, todos, observer=self.on_todos_updated)


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


def update_todos(target: list[dict], todos: list[dict], observer=None) -> str:
    """更新任务进度，并对模型输出做二次校验。

    模型输出的 todos 通常能直接使用，这里额外做格式清洗、状态校验和 in_progress 数量校验。

    ``observer`` 是治理层的状态改变入口（计划 §3.3-2：状态改变必须同时留下
    事件）：在内存列表被修改**之前**调用，观察者抛异常时整个更新被拒绝，
    内存列表保持原状——禁止"只改内存对象"。
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

    # 治理观察者在内存变更前执行：异常向上传播，更新整体拒绝。
    if observer is not None:
        observer(cleaned)

    # 注意这里用 clear + extend 原地更新，保持外部持有的列表对象引用不变。
    target.clear()
    target.extend(cleaned)
    print("\nKlonet Agent：计划已更新。")
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
