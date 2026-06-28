"""Klonet、通用技术和混合问题的软路由。"""

from __future__ import annotations

import re

from klonet_agent.knowledge.models import QueryRoute, TaskType


_EXPLICIT_GENERAL = re.compile(
    r"(?:"
    r"(?:不需要|无需|不使用|不要|独立于|排除|不涉及)\s*(?:klonet|vemu)"
    r"|(?:与|和)\s*(?:klonet|vemu)\s*无关"
    r"|非\s*(?:klonet|vemu)"
    r")",
    re.IGNORECASE,
)
_KLONET_TERMS = {
    "klonet", "vemu", "topodeployapi", "service_layer", "process_bar",
    "卫星", "satellite", "天地一体化", "星座",
    "拓扑部署", "拓扑删除", "worker注册", "worker 注册", "验收差异",
    "项目日志", "虚拟机终端", "链路时延", "进度条卡住",
}
_GENERAL_TERMS = {
    "docker compose", "docker-compose", "dind", "rust", "ubuntu",
    "linux vm", "kubernetes", "通用技术", "自定义网络",
}
_DOMAIN_TERMS = {
    "topology": ("拓扑", "topo", "process_bar"),
    "vm": ("虚拟机", "kvm", "terminal", "ssh"),
    "traffic": ("流量", "traffic", "pkt"),
    "link": ("链路", "时延", "delay", "link"),
    "monitor": ("监控", "prometheus", "grafana"),
    "auth": ("用户", "登录", "密码", "权限"),
    "runtime": ("部署", "启动", "关闭", "环境"),
    "satellite": ("卫星", "satellite", "天地一体化", "星座", "星间链路", "星地链路"),
}


class QueryRouter:
    """输出带置信度的软路由，只有确定规则才关闭 RAG。"""

    def route(self, query: str) -> QueryRoute:
        normalized = " ".join((query or "").lower().split())
        task_type = _task_type(normalized)
        domains = tuple(
            domain
            for domain, terms in _DOMAIN_TERMS.items()
            if any(term in normalized for term in terms)
        )

        if _EXPLICIT_GENERAL.search(normalized):
            return QueryRoute(
                scope="general",
                confidence=1.0,
                task_type="general",
                domains=domains,
                reasons=("explicit_klonet_negation",),
                hard_disable_rag=True,
            )

        has_klonet = any(term in normalized for term in _KLONET_TERMS)
        has_general = any(term in normalized for term in _GENERAL_TERMS)
        has_exact = bool(
            re.search(r"/[a-z0-9_<>.-]+(?:/[a-z0-9_<>.-]+)+/?", normalized)
            or re.search(r"\b[A-Za-z_][A-Za-z0-9_]*(?:API|Manager|Worker)\b", query or "")
        )

        if has_klonet and has_general:
            return QueryRoute(
                scope="mixed",
                confidence=0.9,
                task_type=task_type,
                domains=domains,
                reasons=("klonet_and_general_terms",),
            )
        if has_exact:
            return QueryRoute(
                scope="klonet",
                confidence=0.98,
                task_type="code_lookup",
                domains=domains,
                reasons=("exact_code_identifier",),
            )
        if has_klonet:
            return QueryRoute(
                scope="klonet",
                confidence=0.88,
                task_type=task_type,
                domains=domains,
                reasons=("klonet_terms",),
            )
        if has_general:
            return QueryRoute(
                scope="general",
                confidence=0.82,
                task_type="general",
                domains=domains,
                reasons=("general_technology_terms",),
            )
        return QueryRoute(
            scope="klonet",
            confidence=0.45,
            task_type=task_type,
            domains=domains,
            reasons=("dedicated_agent_default",),
        )


def _task_type(query: str) -> TaskType:
    """根据用户目标选择检索层级策略。"""

    if any(term in query for term in ("报错", "失败", "异常", "卡住", "排查", "无法")):
        return "troubleshooting"
    if any(term in query for term in ("在哪里", "哪个文件", "源码", "实现", "类", "函数", "api", "路由")):
        return "code_lookup"
    if any(term in query for term in ("新增", "修改", "开发", "怎么写", "compose")):
        return "development"
    if any(term in query for term in ("进度", "验收", "日志", "完成了什么")):
        return "project_progress"
    return "concept"


DEFAULT_QUERY_ROUTER = QueryRouter()
