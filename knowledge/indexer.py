"""知识资产扫描、metadata 归一化和 JSONL 索引构建。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from klonet_agent.config import KNOWLEDGE_INDEX_FILE, PROJECT_ROOT


TEXT_SUFFIXES = {".md", ".txt", ".py", ".json", ".jsonl", ".yaml", ".yml", ".toml"}
SKIP_PARTS = {"__pycache__", ".git", ".DS_Store"}
RUNTIME_MEMORY_FILES = {"MEMORY.md", "USER.md", "history.jsonl", "tokens.jsonl"}
SKIP_KNOWLEDGE_DIRS = {"extracted_docs", "extracted_images", "staging"}
INDEX_SCHEMA_VERSION = 3

_LAYER_DEFAULTS = {
    "curated": {
        "priority": "P1",
        "status": "current",
        "quality": "reviewed",
        "sensitivity": "public",
    },
    "experience": {
        "priority": "P1",
        "status": "current",
        "quality": "reviewed",
        "sensitivity": "public",
    },
    "machine_index": {
        "priority": "P1",
        "status": "current",
        "quality": "generated",
        "sensitivity": "public",
    },
    "local": {
        "priority": "P2",
        "status": "current",
        "quality": "unknown",
        "sensitivity": "public",
    },
}


@dataclass
class KnowledgeChunk:
    """一段带来源和质量信息的可检索知识。"""

    chunk_id: str
    layer: str
    source: str
    path: str
    title: str
    content: str
    domain: str = "general"
    priority: str = "P2"
    status: str = "current"
    quality: str = "unknown"
    sensitivity: str = "public"
    last_verified: str = ""
    intent_tags: tuple[str, ...] = ()
    index_schema_version: int = INDEX_SCHEMA_VERSION


class KnowledgeIndexer:
    """扫描正式知识资产并构建本地 JSONL 索引。"""

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        index_file: Path = KNOWLEDGE_INDEX_FILE,
    ):
        self.root = root
        self.index_file = index_file

    def build(self) -> int:
        """重建索引，敏感 chunk 不进入公共索引。"""

        chunks: list[KnowledgeChunk] = []
        for path in self._iter_source_files():
            chunks.extend(
                chunk
                for chunk in self._chunk_file(path)
                if chunk.sensitivity == "public"
            )
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        with self.index_file.open("w", encoding="utf-8") as file:
            for chunk in chunks:
                file.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
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
            self.root / "journals",
        ]
        seen: set[Path] = set()
        knowledge_root = self.root / "knowledge"
        for source_root in roots:
            if not source_root.exists():
                continue
            candidates = [source_root] if source_root.is_file() else source_root.rglob("*")
            for path in candidates:
                if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if any(part in SKIP_PARTS for part in path.parts):
                    continue
                if _is_runtime_memory_file(path, self.root):
                    continue
                if path.resolve() == self.index_file.resolve():
                    continue
                if path.is_relative_to(knowledge_root):
                    relative = path.relative_to(knowledge_root)
                    if relative.parts and relative.parts[0] in SKIP_KNOWLEDGE_DIRS:
                        continue
                    if path.suffix == ".jsonl" and (
                        not relative.parts or relative.parts[0] != "klonet_index"
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
        """根据文件类型生成带 metadata 的 chunk。"""

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")

        relative = path.relative_to(self.root) if path.is_relative_to(self.root) else path
        relative_text = relative.as_posix()
        layer = _knowledge_layer(relative_text)
        defaults = dict(_LAYER_DEFAULTS[layer])

        if path.suffix.lower() == ".jsonl":
            return _chunk_jsonl(text, relative_text, layer, defaults)

        metadata: dict[str, Any] = {}
        if path.suffix.lower() == ".md":
            metadata, text = _parse_frontmatter(text)
        normalized = _normalize_metadata(
            metadata,
            defaults,
            domain=_infer_domain(relative_text),
        )

        sections = (
            _split_markdown_sections(text)
            if path.suffix.lower() == ".md"
            else [(relative_text, part) for part in _split_windows(text)]
        )
        chunks = []
        for index, (section_title, content) in enumerate(sections, start=1):
            parts = _split_windows(content)
            for part_index, part in enumerate(parts, start=1):
                title = section_title or relative_text
                if len(parts) > 1:
                    title = f"{title} ({part_index})"
                chunks.append(
                    _make_chunk(
                        path=relative_text,
                        layer=layer,
                        title=title,
                        content=part,
                        index=f"{index}-{part_index}",
                        metadata=normalized,
                    )
                )
        return chunks


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """读取 Markdown YAML frontmatter。"""

    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    metadata = yaml.safe_load(raw) or {}
    return metadata if isinstance(metadata, dict) else {}, text[end + 5:]


def _normalize_metadata(
    metadata: dict[str, Any],
    defaults: dict[str, str],
    *,
    domain: str,
) -> dict[str, Any]:
    """补齐统一 metadata，并把日期等对象转换成字符串。"""

    raw_domains = metadata.get("domain") or metadata.get("domains")
    if isinstance(raw_domains, list):
        normalized_domain = str(raw_domains[0]) if raw_domains else domain
    else:
        normalized_domain = str(raw_domains or domain).split(",", maxsplit=1)[0].strip()

    raw_status = str(metadata.get("status") or defaults["status"]).lower()
    if raw_status in {"deprecated", "archived"}:
        status = "deprecated"
    elif raw_status == "draft":
        status = "draft"
    else:
        status = "current"

    quality = metadata.get("quality")
    if not quality and raw_status == "current_verified":
        quality = "verified"
    elif not quality and raw_status == "diagnostic_playbook":
        quality = "reviewed"

    return {
        "domain": normalized_domain,
        "priority": str(metadata.get("priority") or defaults["priority"]).upper(),
        "status": status,
        "quality": str(quality or defaults["quality"]).lower(),
        "sensitivity": str(
            metadata.get("sensitivity") or defaults["sensitivity"]
        ).lower(),
        "last_verified": str(metadata.get("last_verified") or ""),
        "intent_tags": _normalize_string_list(metadata.get("intent_tags")),
    }


def _normalize_string_list(value: Any) -> tuple[str, ...]:
    """把 YAML 数组或逗号分隔字符串归一化为去重 tuple。"""

    raw_items = value if isinstance(value, list) else str(value or "").split(",")
    result = []
    for item in raw_items:
        normalized = str(item or "").strip().lower()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切分，并把父级标题保留在子章节名称中。"""

    cleaned = text.strip()
    if not cleaned:
        return []
    heading_source = _mask_markdown_fences(cleaned)
    matches = list(
        re.finditer(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$", heading_source)
    )
    if not matches:
        return [("", cleaned)]

    sections = []
    preamble = cleaned[:matches[0].start()].strip()
    if preamble:
        sections.append(("前言", preamble))
    heading_stack: list[str] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()
        heading_stack = heading_stack[: level - 1]
        heading_stack.append(heading)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        body = cleaned[match.end():end].strip()
        if not body:
            continue
        title = " / ".join(heading_stack)
        content = f"{match.group(0).strip()}\n\n{body}"
        sections.append((title, content))
    return sections


def _mask_markdown_fences(text: str) -> str:
    """屏蔽 fenced code，保持字符位置不变以便继续切原始正文。"""

    pattern = re.compile(
        r"(?ms)^(?P<fence>`{3,}|~{3,})[^\r\n]*\r?\n.*?^(?P=fence)[ \t]*$"
    )
    masked = list(text)
    for match in pattern.finditer(text):
        for index in range(match.start(), match.end()):
            if masked[index] not in "\r\n":
                masked[index] = " "
    return "".join(masked)


def _split_windows(text: str, chunk_size: int = 1200, overlap: int = 120) -> list[str]:
    """超长章节再按固定窗口切分，并保留少量重叠。"""

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


def _chunk_jsonl(
    text: str,
    path: str,
    layer: str,
    defaults: dict[str, str],
) -> list[KnowledgeChunk]:
    """机器索引按记录切分并继承原记录 metadata。"""

    chunks = []
    for index, line in enumerate(text.splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue

        identifier = (
            row.get("route")
            or row.get("symbol")
            or row.get("name")
            or row.get("path")
            or row.get("domain")
            or str(index)
        )
        metadata = dict(row)
        if row.get("sensitive") is True and row.get("default") != "<redacted>":
            metadata["sensitivity"] = "restricted"
        normalized = _normalize_metadata(
            metadata,
            defaults,
            domain=str(row.get("domain") or _infer_domain(str(row.get("path") or path))),
        )
        chunks.append(
            _make_chunk(
                path=path,
                layer=layer,
                title=f"{path}#{identifier}",
                content=json.dumps(row, ensure_ascii=False, sort_keys=True),
                index=str(index),
                metadata=normalized,
            )
        )
    return chunks


def _make_chunk(
    *,
    path: str,
    layer: str,
    title: str,
    content: str,
    index: str,
    metadata: dict[str, Any],
) -> KnowledgeChunk:
    """创建稳定 chunk id，便于 eval 和后续增量索引。"""

    digest = hashlib.sha256(f"{path}:{index}:{title}".encode("utf-8")).hexdigest()[:16]
    return KnowledgeChunk(
        chunk_id=digest,
        layer=layer,
        source=layer,
        path=path,
        title=title,
        content=content,
        **metadata,
    )


def _knowledge_layer(path: str) -> str:
    if path.startswith("knowledge/klonet/"):
        return "curated"
    if path.startswith("knowledge/klonet_experience/"):
        return "experience"
    if path.startswith("knowledge/klonet_index/"):
        return "machine_index"
    return "local"


def _infer_domain(path: str) -> str:
    lowered = path.lower()
    rules = {
        "topology": ("topo", "topology"),
        "vm": ("kvm", "virtual", "terminal", "ssh"),
        "traffic": ("traffic", "pkt"),
        "link": ("link", "delay", "vxlan"),
        "monitor": ("monitor", "prometheus", "grafana"),
        "auth": ("user", "auth", "login"),
        "runtime": ("environment", "startup", "deploy", "config"),
    }
    for domain, terms in rules.items():
        if any(term in lowered for term in terms):
            return domain
    return "general"


def _is_runtime_memory_file(path: Path, root: Path) -> bool:
    """运行时记忆不进入 Klonet 公共知识索引。"""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    parts = relative.parts
    if len(parts) < 2 or parts[0] != "memory":
        return False
    if path.name in RUNTIME_MEMORY_FILES:
        return True
    if "sessions" in parts or "users" in parts:
        return True
    return path.suffix == ".md" and path.stem[:4].isdigit()


# 兼容已有测试和调用。
_split_text = _split_windows
_knowledge_source = _knowledge_layer
