"""Python 3.8 runtime annotation compatibility checks."""

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATHS = (
    "agent.py",
    "answer_policy.py",
    "agents",
    "app",
    "evals",
    "journal",
    "knowledge",
    "llm",
    "memory",
    "ops",
    "orchestrator.py",
    "prompts.py",
    "session.py",
    "subagents",
    "tools",
    "tracing",
    "workspace",
)
MODERN_ANNOTATION_PATTERN = re.compile(
    r"\b(?:list|dict|tuple|set)\s*\[|\w+\s*\|\s*None|None\s*\|\s*\w+"
)


def test_runtime_modules_with_modern_annotations_use_future_annotations():
    """Python 3.8 evaluates annotations unless the future import is present."""

    missing = []
    for path in _runtime_python_files():
        relative = path.relative_to(PROJECT_ROOT)
        text = path.read_text(encoding="utf-8")
        if not MODERN_ANNOTATION_PATTERN.search(text):
            continue
        if "from __future__ import annotations" not in text:
            missing.append(str(relative))

    assert missing == []


def test_runtime_modules_do_not_call_python39_path_is_relative_to():
    """Python 3.8 pathlib.Path does not provide is_relative_to()."""

    offenders = []
    for path in _runtime_python_files():
        text = path.read_text(encoding="utf-8")
        if ".is_relative_to(" in text:
            offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []


def _runtime_python_files():
    for relative in RUNTIME_PATHS:
        path = PROJECT_ROOT / relative
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from path.rglob("*.py")
