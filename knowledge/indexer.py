"""知识索引构建流程。

第一版先使用 JSONL 本地索引，不引入向量数据库。这样实现简单，也方便后续做对比实验：
无 RAG、关键词 RAG、向量 RAG 可以逐步替换。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from klonet_agent.config import JOURNAL_DIR, KNOWLEDGE_INDEX_FILE, PROJECT_ROOT


TEXT_SUFFIXES = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".toml"}
SKIP_PARTS = {"__pycache__", ".git", ".DS_Store"}


@dataclass
class KnowledgeChunk:
    """一段可检索的知识片段。"""

    source: str
    path: str
    title: str
    content: str


class KnowledgeIndexer:
    """扫描项目资料并构建简单 JSONL 索引。"""

    def __init__(self, root: Path = PROJECT_ROOT, index_file: Path = KNOWLEDGE_INDEX_FILE):
        self.root = root
        self.index_file = index_file

    def build(self) -> int:
        """重建索引，返回写入的 chunk 数量。"""

        chunks: list[KnowledgeChunk] = []
        for path in self._iter_source_files():
            chunks.extend(self._chunk_file(path))
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        with self.index_file.open("w", encoding="utf-8") as f:
            for chunk in chunks:
                f.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
        return len(chunks)

    def _iter_source_files(self):
        """遍历可进入知识库的文本文件。"""

        roots = [
            self.root / "README.md",
            self.root / "prompts.py",
            self.root / "knowledge",
            self.root / "journal",
            self.root / "workspace",
            self.root / "tools",
            self.root / "memory",
            self.root / "doc",
            JOURNAL_DIR,
        ]
        style = self.root / "knowledge" / "style_guide.md"
        if style.exists():
            roots.append(style)

        for root in roots:
            if not root.exists():
                continue
            if root.is_file():
                if root.suffix in TEXT_SUFFIXES:
                    yield root
                continue
            for path in root.rglob("*"):
                if any(part in SKIP_PARTS for part in path.parts):
                    continue
                if path.is_file() and path.suffix in TEXT_SUFFIXES:
                    yield path

    def _chunk_file(self, path: Path) -> list[KnowledgeChunk]:
        """按固定长度切分文件。"""

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(self.root) if path.is_relative_to(self.root) else path
        title = str(rel)
        chunks = []
        for index, content in enumerate(_split_text(text), start=1):
            chunks.append(
                KnowledgeChunk(
                    source="local",
                    path=str(rel),
                    title=f"{title}#{index}",
                    content=content,
                )
            )
        return chunks


def _split_text(text: str, chunk_size: int = 1200, overlap: int = 120) -> list[str]:
    """用简单窗口切分文本，保留少量重叠。"""

    cleaned = text.strip()
    if not cleaned:
        return []
    result = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + chunk_size)
        result.append(cleaned[start:end])
        if end == len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return result
