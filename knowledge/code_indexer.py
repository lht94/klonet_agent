"""Structure-aware indexing for the external Klonet source tree."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from klonet_agent.config import CODE_INDEX_FILE, KLONET_SOURCE_ROOT


CODE_SUFFIXES = {
    ".c",
    ".cfg",
    ".conf",
    ".css",
    ".h",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".py",
    ".sh",
    ".sql",
    ".ts",
    ".yaml",
    ".yml",
}
CODE_SKIP_DIRS = {
    ".git",
    ".history",
    ".idea",
    ".libs",
    ".pytest_cache",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "doc",
    "logs",
    "node_modules",
    "static_resources",
    "test",
    "test-results",
    "tests",
    "tmp",
    "vendor",
    "xterm",
}
CODE_SKIP_NAME_PATTERNS = (
    "jquery*.js",
    "*.min.css",
    "*.min.js",
    "*.map",
)
MAX_SOURCE_FILE_BYTES = 512_000
CODE_INDEX_SCHEMA_VERSION = 2
SOURCE_MANIFEST_NAME = "manifest.json"

_JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?"
    r"(?:class|function)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
)
_C_SYMBOL_RE = re.compile(
    r"^\s*(?:[A-Za-z_][\w\s*]+\s+)+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?\s*$"
)
_SENSITIVE_NAME = (
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|mail[_-]?pass|redis[_-]?password)"
)
_QUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_SENSITIVE_NAME}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_SHELL_SECRET_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?"
    r"(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_KEY|ACCESS_KEY|PRIVATE_KEY|"
    r"MAIL_PASS|REDIS_PASSWORD)\s*=\s*)"
    r"(?P<value>(?![$'{\"])[^\s#]+)"
)
_CREDENTIAL_URL_RE = re.compile(
    r"(?i)(?P<scheme>[a-z][a-z0-9+.-]*://[^:/@\s]+:)"
    r"(?P<password>[^@\s/]+)(?P<suffix>@)"
)


class CodeIndexer:
    """Build a separate, generated index from real Klonet source files."""

    def __init__(
        self,
        source_root: Path = KLONET_SOURCE_ROOT,
        index_file: Path = CODE_INDEX_FILE,
    ):
        self.source_root = Path(source_root)
        self.index_file = Path(index_file)

    def build(self) -> int:
        revision = self.source_revision()
        rows: list[dict[str, Any]] = []
        for path in self.iter_source_files():
            rows.extend(self._chunk_file(path, revision=revision))
        _write_jsonl_atomic(self.index_file, rows)
        return len(rows)

    def is_stale(self) -> bool:
        if not self.index_file.exists() or self.index_file.stat().st_size == 0:
            return True
        try:
            with self.index_file.open("r", encoding="utf-8") as file:
                first = json.loads(file.readline())
        except (OSError, json.JSONDecodeError):
            return True
        if first.get("index_schema_version") != CODE_INDEX_SCHEMA_VERSION:
            return True
        if first.get("code_revision") != self.source_revision():
            return True
        try:
            index_mtime_ns = self.index_file.stat().st_mtime_ns
        except OSError:
            return True
        return any(
            path.stat().st_mtime_ns > index_mtime_ns
            for path in self.iter_source_files()
        )

    def source_revision(self) -> str:
        if not self.source_root.exists():
            return "missing"
        manifest_path = self.source_root / SOURCE_MANIFEST_NAME
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}
            revision = str(manifest.get("upstream_commit") or "").strip()
            if revision:
                return revision
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.source_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        revision = completed.stdout.strip()
        return revision if completed.returncode == 0 and revision else "unknown"

    def iter_source_files(self) -> Iterable[Path]:
        if not self.source_root.is_dir():
            return
        root = self.source_root.resolve()
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(name for name in dirs if name not in CODE_SKIP_DIRS)
            for name in sorted(files):
                path = Path(current) / name
                try:
                    relative = path.relative_to(root)
                    if path.is_symlink() or not is_code_source_path(
                        relative,
                        size=path.stat().st_size,
                    ):
                        continue
                    path.resolve().relative_to(root)
                except (OSError, ValueError):
                    continue
                yield path

    def _chunk_file(self, path: Path, *, revision: str) -> list[dict[str, Any]]:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        if "\x00" in text or not text.strip():
            return []
        text = redact_sensitive_text(text)

        relative = path.resolve().relative_to(self.source_root.resolve()).as_posix()
        if path.suffix.lower() == ".py":
            sections = _python_sections(text)
        elif path.suffix.lower() in {".js", ".ts"}:
            sections = _symbol_sections(text, _JS_SYMBOL_RE)
        elif path.suffix.lower() in {".c", ".h"}:
            sections = _symbol_sections(text, _C_SYMBOL_RE)
        else:
            sections = _line_windows(text)

        rows = []
        symbol_occurrences: dict[str, int] = {}
        for symbol, start_line, end_line, content in sections:
            if not content.strip():
                continue
            symbol_occurrences[symbol] = symbol_occurrences.get(symbol, 0) + 1
            occurrence = symbol_occurrences[symbol]
            title = f"{relative}:{start_line}-{end_line}"
            if symbol:
                title = f"{title} {symbol}"
            digest = hashlib.sha256(
                f"{relative}:{symbol}:{occurrence}".encode("utf-8")
            ).hexdigest()[:20]
            rows.append(
                {
                    "chunk_id": f"code-{digest}",
                    "layer": "source_code",
                    "source": "source_code",
                    "path": relative,
                    "title": title,
                    "content": content.strip(),
                    "domain": _infer_code_domain(relative),
                    "priority": "P1",
                    "status": "current",
                    "quality": "generated",
                    "sensitivity": "public",
                    "last_verified": revision,
                    "intent_tags": ["code_lookup", "development"],
                    "line_start": start_line,
                    "line_end": end_line,
                    "symbol": symbol,
                    "code_revision": revision,
                    "index_schema_version": CODE_INDEX_SCHEMA_VERSION,
                }
            )
        return rows


def _python_sections(text: str) -> list[tuple[str, int, int, str]]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _line_windows(text)

    nodes: list[tuple[str, ast.AST]] = []
    first_definition_line = len(lines) + 1
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes.append((node.name, node))
            first_definition_line = min(first_definition_line, _node_start(node))
        elif isinstance(node, ast.ClassDef):
            first_definition_line = min(first_definition_line, _node_start(node))
            methods = [
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            if methods:
                class_header_end = min(_node_start(method) for method in methods) - 1
                if class_header_end >= _node_start(node):
                    nodes.append((node.name, _LineNode(_node_start(node), class_header_end)))
                nodes.extend((f"{node.name}.{method.name}", method) for method in methods)
            else:
                nodes.append((node.name, node))

    sections: list[tuple[str, int, int, str]] = []
    preamble_end = first_definition_line - 1
    if preamble_end >= 1 and any(line.strip() for line in lines[:preamble_end]):
        sections.extend(
            _bounded_section("module", 1, preamble_end, lines)
        )
    for symbol, node in nodes:
        start = _node_start(node)
        end = int(getattr(node, "end_lineno", start))
        sections.extend(_bounded_section(symbol, start, end, lines))
    if not sections:
        return _line_windows(text)
    return sections


class _LineNode:
    def __init__(self, lineno: int, end_lineno: int):
        self.lineno = lineno
        self.end_lineno = end_lineno


def _node_start(node: ast.AST | _LineNode) -> int:
    decorators = getattr(node, "decorator_list", ())
    decorator_lines = [int(item.lineno) for item in decorators]
    return min([int(getattr(node, "lineno", 1)), *decorator_lines])


def _bounded_section(
    symbol: str,
    start: int,
    end: int,
    lines: list[str],
) -> list[tuple[str, int, int, str]]:
    if end - start < 120:
        return [(symbol, start, end, "\n".join(lines[start - 1:end]))]
    result = []
    cursor = start
    part = 1
    while cursor <= end:
        part_end = min(end, cursor + 119)
        result.append(
            (
                f"{symbol} (part {part})",
                cursor,
                part_end,
                "\n".join(lines[cursor - 1:part_end]),
            )
        )
        if part_end == end:
            break
        cursor = max(cursor + 1, part_end - 19)
        part += 1
    return result


def _symbol_sections(
    text: str,
    pattern: re.Pattern[str],
) -> list[tuple[str, int, int, str]]:
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for line_no, line in enumerate(lines, start=1):
        match = pattern.match(line)
        if not match:
            continue
        symbol = next((group for group in match.groups() if group), "symbol")
        starts.append((line_no, symbol))
    if not starts:
        return _line_windows(text)

    sections = []
    if starts[0][0] > 1:
        sections.extend(_bounded_section("module", 1, starts[0][0] - 1, lines))
    for index, (start, symbol) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        sections.extend(_bounded_section(symbol, start, end, lines))
    return sections


def _line_windows(text: str) -> list[tuple[str, int, int, str]]:
    lines = text.splitlines()
    if not lines:
        return []
    result = []
    start = 1
    part = 1
    while start <= len(lines):
        end = min(len(lines), start + 119)
        result.append(
            (
                f"part {part}",
                start,
                end,
                "\n".join(lines[start - 1:end]),
            )
        )
        if end == len(lines):
            break
        start = max(start + 1, end - 19)
        part += 1
    return result


def _infer_code_domain(path: str) -> str:
    lowered = path.lower()
    rules = {
        "topology": ("topo", "topology"),
        "vm": ("kvm", "virtual", "terminal", "ssh", "vm_"),
        "traffic": ("traffic", "pkt", "sflow"),
        "link": ("link", "delay", "vxlan"),
        "monitor": ("monitor", "prometheus", "grafana", "health"),
        "auth": ("user", "auth", "login"),
        "runtime": ("config", "main", "gun", "celery", "deploy"),
        "satellite": ("satellite", "onos"),
    }
    for domain, terms in rules.items():
        if any(term in lowered for term in terms):
            return domain
    return "code"


def redact_sensitive_text(text: str) -> str:
    """Remove literal credentials before content reaches an embedding provider."""

    redacted_lines = []
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_ending = raw_line[len(line):]
        line = _QUOTED_SECRET_RE.sub(
            lambda match: (
                f"{match.group('prefix')}{match.group('quote')}"
                f"[REDACTED]{match.group('quote')}"
            ),
            line,
        )
        line = _SHELL_SECRET_RE.sub(
            lambda match: f"{match.group('prefix')}[REDACTED]",
            line,
        )
        line = _CREDENTIAL_URL_RE.sub(
            lambda match: (
                f"{match.group('scheme')}[REDACTED]{match.group('suffix')}"
            ),
            line,
        )
        redacted_lines.append(line + line_ending)
    return "".join(redacted_lines)


def is_code_source_path(path: Path | str, *, size: int) -> bool:
    """Return whether a repository path belongs in the tracked source snapshot."""

    relative = Path(path)
    if relative.name == SOURCE_MANIFEST_NAME:
        return False
    if any(part in CODE_SKIP_DIRS for part in relative.parts):
        return False
    if relative.suffix.lower() not in CODE_SUFFIXES:
        return False
    if size > MAX_SOURCE_FILE_BYTES:
        return False
    return not any(
        fnmatch.fnmatch(relative.name, pattern)
        for pattern in CODE_SKIP_NAME_PATTERNS
    )


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_name = file.name
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
