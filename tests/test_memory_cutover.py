"""阶段 7：记忆权威切换 CLI 的测试。

关键性质：**评测不达标就不能切换**。这条门槛是整个 cutover 的安全阀，
所以它自己必须被测到，而不是只写在文档里。

04 计划阶段 0 修订后新增的硬门禁：

1. **DSN 必须存在**——``KLONET_AGENT_TEST_PG_DSN`` 没设就退出码 3。
2. **Worker 健康**——``evaluate_worker_health()`` 返回 False 就退出码 1。
3. **评测达标**——与历史契约一致（退出码 1）。

三道门禁按顺序检查，DSN 缺失不会先被"Worker 健康"接住——它是更靠前的硬
约束，回避了"评测函数默认 return 0（baseline_generated）"过去伪装通过的问题。
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from klonet_agent.config import PROJECT_ROOT

_TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"


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


@pytest.fixture
def with_dsn(monkeypatch):
    """DSN 设置好了；不依赖真库——``evaluate_memory_eval`` 通常被 monkeypatch 替换。"""

    monkeypatch.setenv(_TEST_DSN_ENV, "postgresql://placeholder:5432/postgres")


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


# --------------------------------------------------------------------------- #
# DSN 门禁（04 阶段 0 修订）
# --------------------------------------------------------------------------- #


def test_apply_is_refused_when_no_dsn(env_file, monkeypatch) -> None:
    """无 DSN 直接退出码 3，不进评测函数。"""

    monkeypatch.delenv(_TEST_DSN_ENV, raising=False)
    # 若进了 evaluate_memory_eval，boom 会抛；不抛证明 DSN 门禁先拦住。
    monkeypatch.setattr(
        cli,
        "evaluate_memory_eval",
        lambda: pytest.fail("DSN 缺失时应先被门禁拦下"),
    )
    code = cli.main(["--apply", str(env_file)])
    assert code == cli.EXIT_PREREQUISITES, f"期望 {EXIT_PREREQUISITES!r}（无 DSN），实际 {code}"
    assert "MEMORY_AUTHORITY" not in env_file.read_text(encoding="utf-8")


def test_check_is_refused_when_no_dsn(monkeypatch) -> None:
    """``--check`` 也走 DSN 门禁（同样不允许 baseline_generated 伪装通过）。"""

    monkeypatch.delenv(_TEST_DSN_ENV, raising=False)
    monkeypatch.setattr(
        cli,
        "evaluate_memory_eval",
        lambda: pytest.fail("DSN 缺失时应先被门禁拦下"),
    )
    assert cli.main(["--check"]) == cli.EXIT_PREREQUISITES


def test_test_dsn_present_helper(monkeypatch) -> None:
    monkeypatch.delenv(_TEST_DSN_ENV, raising=False)
    assert cli._test_dsn_present() is False
    monkeypatch.setenv(_TEST_DSN_ENV, "postgresql://x")
    assert cli._test_dsn_present() is True
    monkeypatch.setenv(_TEST_DSN_ENV, "   ")
    assert cli._test_dsn_present() is False


# --------------------------------------------------------------------------- #
# Worker 健康门禁（04 阶段 0 修订的钩子，阶段 7 落地）
# --------------------------------------------------------------------------- #


def test_worker_module_missing_is_treated_as_healthy(monkeypatch) -> None:
    """``memory.maintenance.health`` 不可导入时，降级为通过（阶段 0–6 期间合法）。

    阶段 0 已落地 ``memory/maintenance/health.py`` 的占位实现，所以这里改成：
    把真实的占位实现跑一遍，验证它返回 ``healthy=True`` 且键名集合与计划 §6.6
    对齐——这两点是阶段 7 真实接入前的契约保证。
    """

    # 阶段 7 起 collect_snapshot 是真实现：无 MEMORY_DSN → 过渡语义 True；
    # （DSN 在但库打不开 → False，由 test_memory_maintenance_health.py 覆盖。）
    monkeypatch.delenv("KLONET_AGENT_MEMORY_DSN", raising=False)
    healthy, detail = cli.evaluate_worker_health()
    assert healthy is True
    # §6.6 指标字段名集合
    from klonet_agent.memory.maintenance.health import collect_snapshot

    snapshot = dict(collect_snapshot())
    assert snapshot["healthy"] is True
    required_keys = {
        "memory_maintenance_last_success_timestamp",
        "memory_maintenance_run_duration_seconds",
        "memory_maintenance_consecutive_failures",
        "memory_embedding_pending_total",
        "memory_embedding_abandoned_total",
        "memory_embedding_coverage_ratio",
        "memory_expired_active_total",
        "memory_deleted_waiting_purge_total",
        "memory_proposals_pending_total",
        "memory_reembedding_progress_ratio",
    }
    assert required_keys.issubset(snapshot.keys()), (
        f"健康快照缺字段：{required_keys - snapshot.keys()}"
    )


def test_import_health_module_breaks_during_maintenance_disabled(monkeypatch) -> None:
    """``memory.maintenance.health.collect_snapshot`` 抛异常时，
    ``evaluate_worker_health`` 必须降级为 True 而不是冒泡——切到 cutover
    的路径不能因为"读健康"失败而炸。

    用 monkeypatch 把 ``collect_snapshot`` 替换成抛 ImportError 的版本，
    模拟"模块加载失败但模块本身存在"的失败模式。
    """

    from klonet_agent.memory.maintenance import health as health_module

    def _raise(*args, **kwargs):
        raise ImportError("模拟：worker 快照采集失败")

    monkeypatch.setattr(health_module, "collect_snapshot", _raise)
    healthy, detail = cli.evaluate_worker_health()
    assert healthy is True
    assert "尚未就绪" in detail or "视为过渡态" in detail


def test_unhealthy_worker_blocks_apply(env_file, monkeypatch, with_dsn) -> None:
    """阶段 7 之后，若 ``collect_snapshot`` 返回 ``healthy=False``，``--apply`` 必须拒绝。"""

    monkeypatch.setattr(
        cli,
        "evaluate_worker_health",
        lambda: (False, "Worker 不健康：last_success 太久"),
    )
    monkeypatch.setattr(
        cli,
        "evaluate_memory_eval",
        lambda: pytest.fail("健康门禁应先拦住"),
    )
    code = cli.main(["--apply", str(env_file)])
    assert code == cli.EXIT_EVAL_FAILED
    assert "MEMORY_AUTHORITY" not in env_file.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 评测门禁（历史契约）
# --------------------------------------------------------------------------- #


def test_apply_is_refused_when_the_eval_does_not_pass(env_file, monkeypatch, with_dsn) -> None:
    monkeypatch.setattr(cli, "evaluate_worker_health", lambda: (True, "ok"))
    monkeypatch.setattr(cli, "evaluate_memory_eval", lambda: (False, "未达标"))
    code = cli.main(["--apply", str(env_file)])
    assert code == cli.EXIT_EVAL_FAILED
    assert "MEMORY_AUTHORITY" not in env_file.read_text(encoding="utf-8")


def test_apply_writes_cutover_after_a_passing_eval(env_file, monkeypatch, with_dsn) -> None:
    monkeypatch.setattr(cli, "evaluate_worker_health", lambda: (True, "ok"))
    monkeypatch.setattr(cli, "evaluate_memory_eval", lambda: (True, "达标"))
    code = cli.main(["--apply", str(env_file)])
    assert code == cli.EXIT_OK
    assert "KLONET_AGENT_MEMORY_AUTHORITY=cutover" in env_file.read_text(encoding="utf-8")


def test_check_does_not_touch_the_env_file(monkeypatch, with_dsn, tmp_path) -> None:
    monkeypatch.setattr(cli, "evaluate_worker_health", lambda: (True, "ok"))
    monkeypatch.setattr(cli, "evaluate_memory_eval", lambda: (True, "达标"))
    env_file = tmp_path / "klonet-agent.env"
    env_file.write_text("LLM_MODEL=deepseek-v4-flash\n", encoding="utf-8")
    assert cli.main(["--check"]) == cli.EXIT_OK
    assert "MEMORY_AUTHORITY" not in env_file.read_text(encoding="utf-8")


def test_rollback_needs_no_eval(env_file, monkeypatch) -> None:
    def _boom():  # pragma: no cover - 被调用就说明回滚居然还要求评测
        raise AssertionError("回滚不该依赖评测")

    monkeypatch.setattr(cli, "evaluate_worker_health", _boom)
    monkeypatch.setattr(cli, "evaluate_memory_eval", _boom)
    monkeypatch.delenv(_TEST_DSN_ENV, raising=False)
    cli.set_authority(env_file, "cutover")
    assert cli.main(["--rollback", str(env_file)]) == cli.EXIT_OK
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


# --------------------------------------------------------------------------- #
# 默认值（2026-10-10 起：数据库优先，库不可用降级 Markdown）
# --------------------------------------------------------------------------- #


def test_default_authority_is_cutover(config) -> None:
    """不设任何环境变量时，新部署直接走数据库优先。"""

    import importlib

    reloaded = importlib.reload(config)
    assert reloaded.MEMORY_AUTHORITY == "cutover"
    assert reloaded.memory_cutover_enabled() is True
    assert reloaded.MEMORY_PACK_ENABLED is True
    assert reloaded.MEMORY_WRITE_PIPELINE_ENABLED is True
    assert reloaded.markdown_memory_is_authoritative() is False


# --------------------------------------------------------------------------- #
# orchestrator 的可用性降级（数据库优先，没有数据库再降级 Markdown）
# --------------------------------------------------------------------------- #


class _FakeSession:
    user_id = "u"
    project_id = "p"
    mode = "mentor"


class _FakeTrace:
    def __init__(self):
        self.events = []

    def record_privileged_event(self, **kwargs):
        self.events.append(kwargs)


def _bare_orchestrator(
    monkeypatch, *, authoritative: bool, db_available: bool, pack_enabled: bool = True
):
    """构造一个只填了记忆判定所需字段的最小 orchestrator。

    不走真实构造器：这条测试只锁 `_markdown_memory_is_injected` 的判定表，
    与数据库、profile、工具表都无关。
    """

    import klonet_agent.orchestrator as orch_mod

    monkeypatch.setattr(
        orch_mod, "markdown_memory_is_authoritative", lambda: authoritative
    )
    monkeypatch.setattr(orch_mod, "MEMORY_PACK_ENABLED", pack_enabled)
    obj = orch_mod.AgentOrchestrator.__new__(orch_mod.AgentOrchestrator)
    obj._memory_pipeline = None
    # 预置"管线不可用"的原因，避免测试真的去连库。
    obj._memory_pipeline_error = "未配置记忆库 DSN" if not db_available else None
    obj._memory_read_repository = object() if db_available else None
    obj._memory_read_error = None if db_available else None
    obj._markdown_fallback_notified = False
    obj.session = _FakeSession()
    obj.trace_logger = _FakeTrace()
    return obj


def test_cutover_falls_back_to_markdown_when_db_unavailable(monkeypatch) -> None:
    obj = _bare_orchestrator(monkeypatch, authoritative=False, db_available=False)
    assert obj._markdown_memory_is_injected() is True, "库不可用必须降级回 Markdown"


def test_cutover_keeps_markdown_out_when_db_available(monkeypatch) -> None:
    obj = _bare_orchestrator(monkeypatch, authoritative=False, db_available=True)
    assert obj._markdown_memory_is_injected() is False, "库可用就不该注入 Markdown 全文"


def test_cutover_fallback_warns_and_traces_only_once(monkeypatch, capsys) -> None:
    obj = _bare_orchestrator(monkeypatch, authoritative=False, db_available=False)
    first = obj._markdown_memory_is_injected()
    second = obj._markdown_memory_is_injected()
    assert first is True and second is True
    out = capsys.readouterr().out
    assert out.count("降级为 Markdown") == 1, "告警只该出现一次"
    assert len(obj.trace_logger.events) == 1
    assert obj.trace_logger.events[0]["event"] == "memory_pack_markdown_fallback"


def test_legacy_state_keeps_the_old_semantics(monkeypatch) -> None:
    """legacy 且没开按需召回 → 仍然常驻注入 Markdown（旧行为不变）。"""

    import klonet_agent.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "markdown_memory_is_authoritative", lambda: True)
    obj = _bare_orchestrator(
        monkeypatch, authoritative=True, db_available=False, pack_enabled=False
    )
    assert obj._markdown_memory_is_injected() is True
    assert obj._markdown_fallback_notified is False, "legacy 态不走降级告警"
