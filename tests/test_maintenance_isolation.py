"""主链路与维护 Worker 的隔离检查（04 计划 §2.1 第 1 条 / 阶段 2）。

三道门禁：

1. **静态扫描**——``scripts/check_maintenance_isolation.py`` 对主链路所有
   Python 文件做 regex 扫描，不允许出现 ``memory.maintenance`` 的 import。
2. **运行期 import 图**——在**子进程**里 import 主链路模块，断言
   ``sys.modules`` 里没有 ``klonet_agent.memory.maintenance.*``。
   用子进程是为了不被同进程其它测试的 import 污染。
3. **检查器本身有效**——把一段合成的违规代码喂给扫描函数，必须能被抓到；
   否则一个永远返回"干净"的检查等于没有检查。

跑法（不需要 DSN / 真库）::

    python -m pytest tests/test_maintenance_isolation.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from klonet_agent.config import PROJECT_ROOT

_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "check_maintenance_isolation.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_maintenance_isolation", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


# --------------------------------------------------------------------------- #
# 1) 静态扫描必须干净
# --------------------------------------------------------------------------- #


def test_no_main_chain_imports_maintenance() -> None:
    violations = checker.find_violations()
    assert violations == [], (
        "主链路出现了 memory.maintenance 的 import：\n"
        + "\n".join(f"  {p}:{ln}: {line}" for p, ln, line in violations)
    )


def test_main_chain_files_are_discovered() -> None:
    """确保扫描目标集非空——一个扫描不到任何文件的检查会假绿。"""

    files = list(checker._iter_python_files())
    assert len(files) >= 10, f"只发现 {len(files)} 个主链路文件，检查目标集可疑"
    relative = {str(p.relative_to(PROJECT_ROOT)) for p in files}
    assert "agent.py" in relative
    assert "orchestrator.py" in relative
    assert any(name.startswith("app") for name in relative)


# --------------------------------------------------------------------------- #
# 2) 子进程 import 图
# --------------------------------------------------------------------------- #


_MAIN_CHAIN_IMPORT_SCRIPT = """
import sys
import klonet_agent.orchestrator  # noqa: F401
import klonet_agent.session  # noqa: F401
import klonet_agent.answer_policy  # noqa: F401
import klonet_agent.prompts  # noqa: F401
leaked = sorted(
    m for m in sys.modules if m.startswith("klonet_agent.memory.maintenance")
)
print("LEAKED:" + ",".join(leaked))
"""


def test_importing_main_chain_does_not_pull_in_maintenance() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _MAIN_CHAIN_IMPORT_SCRIPT],
        cwd=str(PROJECT_ROOT.parent),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"import 主链路失败：\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    leaked_line = next(
        (line for line in result.stdout.splitlines() if line.startswith("LEAKED:")),
        "LEAKED:",
    )
    leaked = [m for m in leaked_line[len("LEAKED:"):].split(",") if m]
    assert leaked == [], f"import 主链路时被拉进了维护模块：{leaked}"


# --------------------------------------------------------------------------- #
# 3) 检查器本身能被触发（不能是永远为绿的摆设）
# --------------------------------------------------------------------------- #


def test_checker_detects_synthetic_violation(tmp_path, monkeypatch) -> None:
    fake_root = tmp_path / "repo"
    (fake_root / "app").mkdir(parents=True)
    offender = fake_root / "app" / "bad.py"
    offender.write_text(
        "from klonet_agent.memory.maintenance.service import MaintenanceService\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(checker, "PROJECT_ROOT", fake_root)
    monkeypatch.setattr(checker, "MAIN_CHAIN_PATHS", ("app",))
    violations = checker.find_violations()
    assert len(violations) == 1, violations
    path, line_number, line = violations[0]
    assert path == offender
    assert line_number == 1
    assert "MaintenanceService" in line


def test_checker_ignores_mentions_in_comments_and_strings(tmp_path, monkeypatch) -> None:
    """注释 / 文档字符串里提到 memory.maintenance 不算违规。"""

    fake_root = tmp_path / "repo"
    (fake_root / "app").mkdir(parents=True)
    (fake_root / "app" / "note.py").write_text(
        "# 这里不 import memory.maintenance，只提到这个名字\n"
        'DOC = "memory.maintenance 是独立进程"\n'
        "x = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(checker, "PROJECT_ROOT", fake_root)
    monkeypatch.setattr(checker, "MAIN_CHAIN_PATHS", ("app",))
    assert checker.find_violations() == []


def test_checker_main_returns_nonzero_on_violation(tmp_path, monkeypatch, capsys) -> None:
    fake_root = tmp_path / "repo"
    (fake_root / "app").mkdir(parents=True)
    (fake_root / "app" / "bad.py").write_text(
        "import klonet_agent.memory.maintenance.cli\n", encoding="utf-8"
    )
    monkeypatch.setattr(checker, "PROJECT_ROOT", fake_root)
    monkeypatch.setattr(checker, "MAIN_CHAIN_PATHS", ("app",))
    code = checker.main()
    assert code == 1
    out = capsys.readouterr().out
    assert "违规" in out
