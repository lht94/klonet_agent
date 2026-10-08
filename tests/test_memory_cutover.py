"""阶段 7：记忆权威切换 CLI 的测试。

关键性质：**评测不达标就不能切换**。这条门槛是整个 cutover 的安全阀，
所以它自己必须被测到，而不是只写在文档里。
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from klonet_agent.config import PROJECT_ROOT


def _load_cli():
    path = PROJECT_ROOT / "scripts" / "memory_cutover.py"
    spec = importlib.util.spec_from_file_location("memory_cutover_cli", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


@pytest.fixture
def env_file(tmp_path):
    target = tmp_path / "klonet-agent.env"
    target.write_text(
        "# klonet-agent 环境\nLLM_MODEL=deepseek-v4-flash\n", encoding="utf-8"
    )
    return target


def test_set_authority_appends_when_missing(env_file) -> None:
    previous = cli.set_authority(env_file, "cutover")
    assert previous == "(未设置)"
    text = env_file.read_text(encoding="utf-8")
    assert "KLONET_AGENT_MEMORY_AUTHORITY=cutover" in text
    assert "LLM_MODEL=deepseek-v4-flash" in text, "其它配置不能被动过"


def test_set_authority_replaces_in_place(env_file) -> None:
    cli.set_authority(env_file, "shadow")
    previous = cli.set_authority(env_file, "cutover")
    assert previous == "shadow"
    text = env_file.read_text(encoding="utf-8")
    assert text.count("KLONET_AGENT_MEMORY_AUTHORITY") == 1
    assert "KLONET_AGENT_MEMORY_AUTHORITY=cutover" in text


def test_apply_is_refused_when_the_eval_does_not_pass(env_file, monkeypatch) -> None:
    monkeypatch.setattr(cli, "evaluate_memory_eval", lambda: (False, "未达标"))
    code = cli.main(["--apply", str(env_file)])
    assert code == 1
    assert "MEMORY_AUTHORITY" not in env_file.read_text(encoding="utf-8")


def test_apply_writes_cutover_after_a_passing_eval(env_file, monkeypatch) -> None:
    monkeypatch.setattr(cli, "evaluate_memory_eval", lambda: (True, "达标"))
    code = cli.main(["--apply", str(env_file)])
    assert code == 0
    assert "KLONET_AGENT_MEMORY_AUTHORITY=cutover" in env_file.read_text(encoding="utf-8")


def test_check_does_not_touch_the_env_file(env_file, monkeypatch) -> None:
    monkeypatch.setattr(cli, "evaluate_memory_eval", lambda: (True, "达标"))
    assert cli.main(["--check", ]) == 0
    assert "MEMORY_AUTHORITY" not in env_file.read_text(encoding="utf-8")


def test_rollback_needs_no_eval(env_file, monkeypatch) -> None:
    def _boom():  # pragma: no cover - 被调用就说明回滚居然还要求评测
        raise AssertionError("回滚不该依赖评测")

    monkeypatch.setattr(cli, "evaluate_memory_eval", _boom)
    cli.set_authority(env_file, "cutover")
    assert cli.main(["--rollback", str(env_file)]) == 0
    assert "KLONET_AGENT_MEMORY_AUTHORITY=legacy" in env_file.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 配置层的单一切换点
# --------------------------------------------------------------------------- #


@pytest.fixture
def config():
    """重新加载 ``config`` 模块，并在退出时把默认值放回去。

    ``config`` 的值是模块级常量，reload 是唯一能验证"环境变量怎么读"的办法；
    但 reload 会污染同进程后续用例，所以收尾必须**先清环境变量再 reload**——
    只在 monkeypatch 里撤销是不够的，那时 reload 已经发生过了。
    """

    import importlib
    import os

    import klonet_agent.config as module

    key = "KLONET_AGENT_MEMORY_AUTHORITY"
    original = os.environ.pop(key, None)
    try:
        yield module
    finally:
        os.environ.pop(key, None)
        if original is not None:
            os.environ[key] = original
        importlib.reload(module)


def _reload_with(monkeypatch, value: str):
    import importlib

    import klonet_agent.config as module

    monkeypatch.setenv("KLONET_AGENT_MEMORY_AUTHORITY", value)
    return importlib.reload(module)


def test_cutover_turns_on_the_whole_database_path(monkeypatch, config) -> None:
    """一个变量打开三件事：读取管线、写入管线、按需注入。"""

    reloaded = _reload_with(monkeypatch, "cutover")
    assert reloaded.memory_cutover_enabled() is True
    assert reloaded.markdown_memory_is_authoritative() is False
    assert reloaded.MEMORY_PACK_ENABLED is True
    assert reloaded.MEMORY_WRITE_PIPELINE_ENABLED is True


def test_earlier_phases_keep_markdown_authoritative(monkeypatch, config) -> None:
    for phase in ("legacy", "shadow", "compare"):
        reloaded = _reload_with(monkeypatch, phase)
        assert reloaded.memory_cutover_enabled() is False
        assert reloaded.markdown_memory_is_authoritative() is True
        assert reloaded.MEMORY_PACK_ENABLED is False


def test_a_misspelled_authority_falls_back_to_legacy(monkeypatch, config) -> None:
    """拼错一个字母绝不能把权威悄悄切走——这是最糟的失败方向。"""

    reloaded = _reload_with(monkeypatch, "cutovr")
    assert reloaded.MEMORY_AUTHORITY == "legacy"
    assert reloaded.memory_cutover_enabled() is False
    assert reloaded.markdown_memory_is_authoritative() is True
