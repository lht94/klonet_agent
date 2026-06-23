"""知识索引构建流程。

第一版先使用 JSONL 本地索引，不引入向量数据库。这样实现简单，也方便后续做对比实验：
无 RAG、关键词 RAG、向量 RAG 可以逐步替换。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from klonet_agent.config import JOURNAL_DIR, KNOWLEDGE_INDEX_FILE, PROJECT_ROOT


TEXT_SUFFIXES = {".md", ".txt", ".py", ".json", ".jsonl", ".yaml", ".yml", ".toml"}
SKIP_PARTS = {"__pycache__", ".git", ".DS_Store"}
RUNTIME_MEMORY_FILES = {"MEMORY.md", "USER.md", "history.jsonl", "tokens.jsonl"}
SKIP_KNOWLEDGE_DIRS = {"extracted_docs", "extracted_images", "staging"}


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
        seen: set[Path] = set()
        knowledge_root = self.root / "knowledge"
        for source_root in roots:
            if not source_root.exists():
                continue
            candidates = [source_root] if source_root.is_file() else source_root.rglob("*")
            for path in candidates:
                if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                    continue
                if any(part in SKIP_PARTS for part in path.parts):
                    continue
                if _is_runtime_memory_file(path, self.root):
                    continue
                if path.resolve() == self.index_file.resolve():
                    continue
                if path.is_relative_to(knowledge_root):
                    rel_knowledge = path.relative_to(knowledge_root)
                    if rel_knowledge.parts and rel_knowledge.parts[0] in SKIP_KNOWLEDGE_DIRS:
                        continue
                    if path.suffix == ".jsonl" and (
                        not rel_knowledge.parts or rel_knowledge.parts[0] != "klonet_index"
                    ):
                        continue
                elif path.suffix == ".jsonl":
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield path

    def _chunk_file(self, path: Path) -> list[KnowledgeChunk]:
        """按固定长度切分文件。"""

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(self.root) if path.is_relative_to(self.root) else path
        # 索引里统一使用 /，避免 Windows 反斜杠影响检索结果展示和测试。
        rel_text = rel.as_posix() if isinstance(rel, Path) else str(rel)
        source = _knowledge_source(rel_text)
        if path.suffix == ".jsonl":
            return _chunk_jsonl(text, rel_text, source)
        chunks = []
        for index, content in enumerate(_split_text(text), start=1):
            chunks.append(
                KnowledgeChunk(
                    source=source,
                    path=rel_text,
                    title=f"{rel_text}#{index}",
                    content=content,
                )
            )
        return chunks


def _knowledge_source(path: str) -> str:
    """按知识资产层标记检索来源。"""

    if path.startswith("knowledge/klonet/"):
        return "curated"
    if path.startswith("knowledge/klonet_experience/"):
        return "experience"
    if path.startswith("knowledge/klonet_index/"):
        return "machine_index"
    return "local"


def _chunk_jsonl(text: str, path: str, source: str) -> list[KnowledgeChunk]:
    """机器索引按 JSONL 记录切分，避免跨记录窗口污染。"""

    chunks = []
    for index, line in enumerate(text.splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        identifier = (
            row.get("route")
            or row.get("symbol")
            or row.get("domain")
            or row.get("name")
            or row.get("path")
            or str(index)
        )
        chunks.append(
            KnowledgeChunk(
                source=source,
                path=path,
                title=f"{path}#{identifier}",
                content=json.dumps(row, ensure_ascii=False, sort_keys=True),
            )
        )
    return chunks

def _is_runtime_memory_file(path: Path, root: Path) -> bool:
    """判断是否为运行时记忆文件。

    `memory/store.py` 这类源码可以进知识库，但 MEMORY.md、USER.md 和按日期生成
    的情景记忆属于用户运行状态，不应该沉淀进 Klonet 公共知识索引。
    """

    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    if len(parts) < 2 or parts[0] != "memory":
        return False
    if path.name in RUNTIME_MEMORY_FILES:
        return True
    return path.suffix == ".md" and path.stem[:4].isdigit()


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
