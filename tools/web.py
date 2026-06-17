"""网页访问工具。

这里放 web_fetch、搜索、下载文档等联网能力。
服务器部署时建议默认受限，只允许访问白名单域名或经过审批的资料源。
"""

import re
import urllib.request
from html.parser import HTMLParser


# HTML 转纯文本的辅助类。
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        # script/style 里的内容一般不是人类可读正文，需要跳过。
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        # 遇到常见块级标签时补一个换行，让提取出的文本更易读。
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        # 把连续三行以上空行压缩成最多两行，避免返回内容太散。
        return re.sub(r"\n{3,}", "\n\n", "".join(self._parts)).strip()


def web_fetch(url: str, extract_mode: str = "text", max_chars: int = 8000) -> str:
    """获取指定 URL 的网页内容。"""

    # 用 url 创建请求，并设置 User-Agent，避免部分网站拒绝默认 Python 请求。
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return f"Error fetching {url}:{exc}"

    # text 模式提取纯文本；raw 模式保留原始 HTML。
    if extract_mode == "text":
        parser = _TextExtractor()
        parser.feed(raw)
        text = parser.get_text()
    else:
        text = raw
    return text[:max_chars]
