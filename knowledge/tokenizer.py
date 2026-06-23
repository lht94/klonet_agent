"""中英文、源码标识符和 API 路由混合分词。"""

from __future__ import annotations

import logging
import re
import warnings

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="pkg_resources is deprecated as an API.*",
        category=UserWarning,
    )
    import jieba

jieba.setLogLevel(logging.ERROR)


DOMAIN_TERMS = (
    "Klonet",
    "VEMU",
    "拓扑部署",
    "拓扑删除",
    "进度条",
    "Worker",
    "Worker心跳",
    "Worker注册",
    "数据服务器",
    "虚拟机终端",
    "链路时延",
    "验收差异",
    "TopoDeployAPI",
    "Service_layer",
    "process_bar",
    "machine_index",
)

_STOP_WORDS = {
    "的", "了", "和", "与", "在", "是", "要", "用", "如何", "怎么",
    "应该", "一下", "这个", "那个", "可以", "需要", "进行", "问题",
    "完全", "存在", "哪里", "在哪", "什么",
}
_ROUTE_RE = re.compile(r"/[A-Za-z0-9_<>.-]+(?:/[A-Za-z0-9_<>.-]+)+/?")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


class MixedTokenizer:
    """为 Klonet 文档提供稳定、可解释的混合分词。"""

    def __init__(self, domain_terms: tuple[str, ...] = DOMAIN_TERMS):
        self._tokenizer = jieba.Tokenizer()
        for term in domain_terms:
            self._tokenizer.add_word(term, freq=1_000_000)

    def tokenize(self, text: str) -> list[str]:
        """返回去重后的检索 token，保留原始出现顺序。"""

        normalized = (text or "").lower()
        tokens: list[str] = []

        # 路由和源码标识符必须整体保留，不能被中文分词拆散。
        tokens.extend(match.group(0).lower() for match in _ROUTE_RE.finditer(normalized))
        tokens.extend(match.group(0).lower() for match in _IDENTIFIER_RE.finditer(normalized))

        for chinese in _CHINESE_RE.findall(normalized):
            words = [
                word.strip().lower()
                for word in self._tokenizer.cut_for_search(chinese)
                if word.strip()
            ]
            tokens.extend(
                word for word in words
                if word not in _STOP_WORDS and (len(word) > 1 or word.isascii())
            )
            # 只有整段没有被 jieba 切开时才补充双字，避免普通问句产生大量噪声。
            useful_words = [word for word in words if word not in _STOP_WORDS]
            if len(chinese) >= 4 and len(useful_words) <= 1:
                tokens.extend(
                    chinese[index:index + 2]
                    for index in range(len(chinese) - 1)
                )

        return list(dict.fromkeys(token for token in tokens if token))


DEFAULT_TOKENIZER = MixedTokenizer()
