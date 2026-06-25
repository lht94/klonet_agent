"""根据当前任务类型生成 Mentor 的本轮回答约束。"""

from __future__ import annotations

from klonet_agent.knowledge.intent import QueryIntent


_DEPLOYMENT_TERMS = (
    "部署",
    "安装",
    "启动",
    "停止",
    "配置环境",
    "环境配置",
    "上线",
)
_COMPLEX_CONCEPT_TERMS = (
    "解释",
    "原理",
    "架构",
    "流程",
    "机制",
    "为什么",
    "如何工作",
)


def build_answer_policy(
    task_type: str,
    user_input: str,
    *,
    intent: QueryIntent | None = None,
) -> str:
    """返回只对当前用户问题生效的回答结构和长度策略。"""

    structure, length, extra_rule = _select_policy(task_type, user_input, intent)
    rules = [
        "【本轮回答策略】",
        f"- 回答结构：{structure}",
        f"- 建议长度：{length}",
        "- 第一段直接给出结论，只解释理解结论所必需的原因。",
        "- 不重复用户问题，不汇报内部检索过程。",
        "- 不机械追加学习建议、源码路径或下一步。",
        "- 没有可靠证据时说明不确定，不生成 Klonet 架构推测。",
    ]
    if extra_rule:
        rules.append(f"- {extra_rule}")
    return "\n".join(rules)


def _select_policy(
    task_type: str,
    user_input: str,
    intent: QueryIntent | None,
) -> tuple[str, str, str]:
    """选择结构、长度和任务特有规则，未知类型安全降级。"""

    normalized_type = (task_type or "").strip().lower()
    query = (user_input or "").strip()

    if normalized_type == "credential_boundary":
        return (
            "安全边界、可替代做法",
            "100 至 300 字",
            "不得复述、猜测或保存明文账号、密码、token、真实 IP",
        )
    if intent is not None and intent.operation == "platform_start":
        return (
            "启动前提、标准启动命令、验证方式",
            "300 至 700 字",
            "不得包含环境安装步骤；不得推测 start.sh 或其他无证据脚本",
        )
    if intent is not None and intent.operation == "platform_stop":
        return "停止前提、标准停止命令、验证方式", "200 至 500 字", ""
    if intent is not None and intent.operation == "platform_restart":
        return "重启前提、受影响服务、重启命令、验证方式", "300 至 700 字", ""
    if intent is not None and intent.operation in {
        "environment_setup",
        "dependency_install",
    }:
        return (
            "推荐方案、当前前提、执行步骤、验证方式",
            "按必要步骤展开",
            "不得混入平台日常启动步骤",
        )
    if normalized_type in {"concept", "development"} and any(
        term in query for term in _DEPLOYMENT_TERMS
    ):
        return (
            "推荐方案、当前前提、执行步骤、验证方式",
            "按必要步骤展开",
            "步骤必须与已经确认的前提一致",
        )
    if normalized_type == "troubleshooting":
        return "最可能原因、排查顺序、判断依据", "500 至 1000 字", ""
    if normalized_type == "code_lookup":
        return (
            "入口、调用链、关键状态、注意点",
            "按必要步骤展开",
            "只提供理解调用链所必需的源码路径",
        )
    if normalized_type == "development":
        return "结论、实现要点、必要步骤、验证方式", "300 至 800 字", ""
    if normalized_type == "project_progress":
        return (
            "当前状态、已完成、阻塞项、下一步",
            "300 至 600 字",
            "下一步仅在回答项目进度所必需时提供",
        )
    if normalized_type == "concept":
        if _is_simple_fact(query):
            return "结论、必要说明", "100 至 300 字", ""
        return "结论、核心原理、必要的相关模块", "300 至 600 字", ""
    if normalized_type == "general":
        return (
            "直接结论、必要解释",
            "按问题复杂度控制，能短则短",
            "不主动加入 Klonet 内容",
        )
    return "直接结论、必要解释", "按问题复杂度控制，能短则短", ""


def _is_simple_fact(query: str) -> bool:
    """用保守规则识别短事实题，复杂概念词优先判为展开解释。"""

    if any(term in query for term in _COMPLEX_CONCEPT_TERMS):
        return False
    return len(query) <= 30
