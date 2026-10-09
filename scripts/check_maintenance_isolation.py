#!/usr/bin/env python
"""静态检查：主链路不得 import ``memory.maintenance``（04 计划 §2.1 第 1 条）。

**为什么需要这个检查**：维护 Worker 是"独立进程、故障隔离"的组件。如果
``orchestrator`` / ``agent`` / ``session`` / ``agents/`` / ``app/`` 里任何一处
出现了 ``from klonet_agent.memory.maintenance import ...``，就等于把 Worker 拉回
了对话进程——于是一个 Worker 版本里的 import 副作用（连接池、信号处理器、
后台线程）会静默污染每次对话请求。

这类"架构约束"光写在文档里没用：一次 PR 就能悄悄破坏它，而所有单测仍然是绿的。
所以它必须是**可执行的门禁**。

用法::

    python scripts/check_maintenance_isolation.py
    # 退出码：0 干净；1 发现违规

也可以被 ``tests/test_maintenance_isolation.py`` 直接 import 复用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 主链路（对话进程会 import 的模块）。Worker 绝不能被这些模块间接拉进来。
MAIN_CHAIN_PATHS: tuple[str, ...] = (
    "agent.py",
    "orchestrator.py",
    "session.py",
    "answer_policy.py",
    "prompts.py",
    "app",
    "agents",
    "subagents",
    "context",
)

FORBIDDEN_TOKEN = "memory.maintenance"

# 只匹配真正的 import 语句 / importlib 字符串引用，避免把注释/文档字符串里
# 提到 "memory.maintenance" 的说明文字误判成违规。
_IMPORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*import\s+klonet_agent\.memory\.maintenance\b", re.MULTILINE),
    re.compile(
        r"^\s*from\s+klonet_agent\.memory\.maintenance(?:\.[\w.]+)?\s+import\b",
        re.MULTILINE,
    ),
    re.compile(r"^\s*from\s+\.+memory\.maintenance(?:\.[\w.]+)?\s+import\b", re.MULTILINE),
    re.compile(r"""import_module\(\s*['"]klonet_agent\.memory\.maintenance"""),
    re.compile(r"""__import__\(\s*['"]klonet_agent\.memory\.maintenance"""),
)


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for entry in MAIN_CHAIN_PATHS:
        path = PROJECT_ROOT / entry
        if path.is_file() and path.suffix == ".py":
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts))
    return files


def find_violations() -> list[tuple[Path, int, str]]:
    """返回 ``[(文件, 行号, 命中行)]``；空列表表示干净。"""

    violations: list[tuple[Path, int, str]] = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in _IMPORT_PATTERNS:
            for match in pattern.finditer(text):
                line_number = text.count("\n", 0, match.start()) + 1
                line = text.splitlines()[line_number - 1].strip()
                violations.append((path, line_number, line))
    return violations


def main() -> int:
    violations = find_violations()
    if not violations:
        print(
            f"隔离检查通过：{len(_iter_python_files())} 个主链路文件均未 import "
            f"{FORBIDDEN_TOKEN}"
        )
        return 0
    print(f"发现 {len(violations)} 处违规 import（主链路不得引用 {FORBIDDEN_TOKEN}）：")
    for path, line_number, line in violations:
        print(f"  {path.relative_to(PROJECT_ROOT)}:{line_number}: {line}")
    print(
        "\n维护 Worker 必须与 Agent 对话进程分离（04 计划 §2.1/§3.1）。"
        "若需要在主链路里查询维护健康，请通过已接入的 memory_cutover.py 钩子，"
        "而不是直接 import 本包。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
