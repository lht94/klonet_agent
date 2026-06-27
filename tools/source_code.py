"""Klonet 源码只读检索工具。

这些工具和 workspace 文件工具不同：它们固定面向规范源码树
``klonet_knowledge/02_vemu_uestc_code``，供 Mentor 在源码解释、接口定位、
配置说明和故障排查时读取真实源码证据。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from klonet_agent.config import KLONET_SOURCE_ROOT


SOURCE_ROOT = KLONET_SOURCE_ROOT
DEFAULT_MAX_RESULTS = 50
DEFAULT_MAX_CHARS = 12000
DEFAULT_MAX_FILE_BYTES = 1_000_000
SKIP_DIRS = {
    ".git",
    ".history",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "logs",
    "tmp",
    "test-results",
    "output",
}


def search_code(
    query: str,
    path: str = "",
    file_glob: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    case_sensitive: bool = False,
) -> str:
    """在 Klonet 源码树内搜索文本，返回相对路径、行号和命中行。

    默认按字面量搜索，避免模型传入正则后造成误匹配或性能波动。
    """

    normalized_query = (query or "").strip()
    if not normalized_query:
        return "未提供搜索关键词。"

    base = _resolve_source_path(path or ".")
    max_results = _clamp(max_results, 1, 200)
    rg_result = _search_with_rg(
        normalized_query,
        base=base,
        file_glob=file_glob,
        max_results=max_results,
        case_sensitive=case_sensitive,
    )
    if rg_result is not None:
        return rg_result
    return _search_with_python(
        normalized_query,
        base=base,
        file_glob=file_glob,
        max_results=max_results,
        case_sensitive=case_sensitive,
    )


def read_source_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """读取 Klonet 源码树内的单个文本文件，可选行范围。"""

    file_path = _resolve_source_path(path)
    if not file_path.is_file():
        return f"未找到源码文件：{_display_path(file_path)}"
    if file_path.stat().st_size > DEFAULT_MAX_FILE_BYTES:
        return f"源码文件过大，已拒绝读取：{_relative_path(file_path)}"

    text = file_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    end = int(end_line) if end_line else len(lines)
    end = min(max(start, end), len(lines))
    max_chars = _clamp(max_chars, 1000, 50000)

    selected = lines[start - 1 : end]
    header = f"源码文件：{_relative_path(file_path)}（第 {start}-{end} 行）"
    body_lines = [
        f"{_relative_path(file_path)}:{line_no}: {line}"
        for line_no, line in enumerate(selected, start=start)
    ]
    result = "\n".join([header, *body_lines])
    return _truncate(result, max_chars)


def list_source_files(
    path: str = "",
    pattern: str | None = None,
    max_results: int = 200,
) -> str:
    """列出 Klonet 源码树中的相对文件路径。"""

    base = _resolve_source_path(path or ".")
    if not base.exists():
        return f"未找到源码目录：{_display_path(base)}"
    if not base.is_dir():
        return f"不是源码目录：{_relative_path(base)}"

    max_results = _clamp(max_results, 1, 1000)
    matches: list[str] = []
    for file_path in _iter_text_files(base):
        rel = _relative_path(file_path)
        if pattern and not fnmatch.fnmatch(rel, pattern):
            continue
        matches.append(rel)
        if len(matches) >= max_results:
            break

    if not matches:
        return "未找到匹配的源码文件。"
    suffix = "" if len(matches) < max_results else "\n...（结果过多，已截断）"
    return "\n".join(matches) + suffix


def _search_with_rg(
    query: str,
    *,
    base: Path,
    file_glob: str | None,
    max_results: int,
    case_sensitive: bool,
) -> str | None:
    """优先使用 ripgrep；不可用时返回 None 让调用方降级。"""

    rg = shutil.which("rg")
    if rg is None:
        return None

    command = [
        rg,
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--fixed-strings",
        "--max-count",
        str(max_results),
    ]
    if not case_sensitive:
        command.append("--ignore-case")
    for skip in sorted(SKIP_DIRS):
        command.extend(["--glob", f"!{skip}/**"])
    if file_glob:
        command.extend(["--glob", file_glob])
    command.extend([query, str(base)])

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode not in (0, 1):
        return None
    if not completed.stdout.strip():
        return "未在源码中找到匹配内容。"

    rows = []
    for line in completed.stdout.splitlines():
        parsed = _parse_rg_line(line)
        if parsed is None:
            continue
        file_path, line_no, snippet = parsed
        try:
            resolved = Path(file_path).resolve()
            rel = _relative_path(resolved)
        except PermissionError:
            continue
        rows.append(f"{rel}:{line_no}: {snippet.strip()}")
        if len(rows) >= max_results:
            break
    if not rows:
        return "未在源码中找到匹配内容。"
    suffix = "" if len(rows) < max_results else "\n...（结果过多，已截断）"
    return "\n".join(rows) + suffix


def _search_with_python(
    query: str,
    *,
    base: Path,
    file_glob: str | None,
    max_results: int,
    case_sensitive: bool,
) -> str:
    needle = query if case_sensitive else query.lower()
    rows: list[str] = []
    for file_path in _iter_text_files(base):
        rel = _relative_path(file_path)
        if file_glob and not fnmatch.fnmatch(rel, file_glob):
            continue
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    haystack = line if case_sensitive else line.lower()
                    if needle in haystack:
                        rows.append(f"{rel}:{line_no}: {line.strip()}")
                        if len(rows) >= max_results:
                            return "\n".join(rows) + "\n...（结果过多，已截断）"
        except OSError:
            continue
    if not rows:
        return "未在源码中找到匹配内容。"
    return "\n".join(rows)


def _iter_text_files(base: Path):
    for root, dirs, files in os.walk(base):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            file_path = Path(root) / name
            if file_path.stat().st_size > DEFAULT_MAX_FILE_BYTES:
                continue
            yield file_path


def _resolve_source_path(path: str) -> Path:
    root = SOURCE_ROOT.resolve()
    candidate = (root / (path or ".")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"源码工具只能访问 Klonet 源码目录：{root}") from exc
    return candidate


def _relative_path(path: Path) -> str:
    root = SOURCE_ROOT.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise PermissionError(f"源码工具只能访问 Klonet 源码目录：{root}") from exc


def _display_path(path: Path) -> str:
    try:
        return _relative_path(path)
    except PermissionError:
        return str(path)


def _parse_rg_line(line: str) -> tuple[str, str, str] | None:
    match = re.match(r"^(.*?):(\d+):(.*)$", line)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(parsed, maximum))


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = "\n...（源码内容过长，已截断）"
    return text[: max_chars - len(suffix)].rstrip() + suffix

