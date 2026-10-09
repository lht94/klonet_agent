"""维护 Worker 主循环测试（04 计划 §8 / 阶段 2 完成标准）。

不需要真库：``MaintenanceService`` 只依赖鸭子类型的 repository，全部循环
语义用一个 fake repository + fake Job 就能测。真库路径由
``tests/test_memory_maintenance_repository.py`` 覆盖。

覆盖点：

* ``MaintenanceConfig`` 非法值被拒绝（不静默修正）；
* job registry 拒绝未知名字 / 别名 / 重名；
* ``run_forever`` 在已请求停止时不领取任何 Job；
* Job 运行期间触发停止 → 收尾完成后退出，不再领新 Job（"等当前 Job 完成"）；
* 空闲时走可被唤醒的退避 sleep；
* 单个 Job 抛异常 → 记 fail、循环继续、不冒泡；
* DB 里出现未注册 Job → 记 ``unknown_job`` 失败；
* ``run_once --job`` 过滤、``force`` 透传、``dry_run`` 透传。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

import klonet_agent.config as config_module
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.service import (
    MaintenanceService,
    UnknownJobError,
    resolve_job_name,
)

# 注意：**不** ``from klonet_agent.config import MaintenanceConfig``——同进程里
# ``tests/test_memory_cutover.py`` 会 ``importlib.reload(config)``，reload 会
# 重新执行模块体、把 ``MaintenanceConfig`` / ``MaintenanceConfigError`` 换成
# **新的类对象**。测试若在模块导入时绑定旧对象，`pytest.raises(旧类)` 就匹配不到
# reload 后抛出的新类实例。所以统一在调用点按属性取（``_Config()`` / ``_ConfigError``）。


def _Config(**kwargs):
    return config_module.MaintenanceConfig(**kwargs)


def _ConfigError():
    return config_module.MaintenanceConfigError


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class FakeJob:
    def __init__(self, name: str, *, result=None, raises=None, on_run=None):
        self.name = name
        self._result = result if result is not None else JobResult(scanned=1, changed=1)
        self._raises = raises
        self._on_run = on_run
        self.calls: list[tuple[JobContext, str | None]] = []

    def run(self, context: JobContext, cursor: str | None) -> JobResult:
        self.calls.append((context, cursor))
        if self._on_run is not None:
            self._on_run(context)
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeClaim:
    def __init__(self, job_name: str, *, run_id="run-1", cursor=None, config=None, batch_limit=7):
        now = datetime.now(timezone.utc)
        self.job_name = job_name
        self.run_id = run_id
        self.worker_id = "fake"
        self.started_at = now
        self.deadline = now + timedelta(seconds=120)
        self.cursor = cursor
        self.config = config or {}
        self.batch_limit = batch_limit


class FakeRepository:
    def __init__(self, claims):
        # claims: list[FakeClaim] 每次 claim 弹一个；空则返回 None。
        self._claims = list(claims)
        self.claim_calls: list[dict] = []
        self.completed: list[dict] = []
        self.failed: list[dict] = []

    def claim_due_job(self, *, worker_id, lease_seconds, job_name=None, force=False):
        self.claim_calls.append(
            {
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
                "job_name": job_name,
                "force": force,
            }
        )
        if not self._claims:
            return None
        return self._claims.pop(0)

    def heartbeat(self, *, job_name, worker_id, lease_seconds):  # pragma: no cover
        return True

    def complete(self, *, job_name, run_id, result, next_run_seconds):
        self.completed.append(
            {
                "job_name": job_name,
                "run_id": run_id,
                "result": result,
                "next_run_seconds": next_run_seconds,
            }
        )

    def fail(self, *, job_name, run_id, error_code, error_summary, next_run_seconds):
        self.failed.append(
            {
                "job_name": job_name,
                "run_id": run_id,
                "error_code": error_code,
                "error_summary": error_summary,
                "next_run_seconds": next_run_seconds,
            }
        )


def _config(**overrides):
    base = dict(
        enabled=True,
        poll_seconds=1.0,
        shutdown_grace_seconds=10.0,
        lease_seconds=120.0,
    )
    base.update(overrides)
    return _Config(**base)


# --------------------------------------------------------------------------- #
# MaintenanceConfig 拒绝非法值
# --------------------------------------------------------------------------- #


def test_config_rejects_zero_poll() -> None:
    with pytest.raises(_ConfigError(), match="poll_seconds"):
        _Config(poll_seconds=0)


def test_config_rejects_zero_lease() -> None:
    with pytest.raises(_ConfigError(), match="lease_seconds"):
        _Config(lease_seconds=0)


def test_config_rejects_lease_shorter_than_poll() -> None:
    with pytest.raises(_ConfigError(), match="lease_seconds"):
        _Config(poll_seconds=10.0, lease_seconds=5.0, shutdown_grace_seconds=1.0)


def test_config_rejects_grace_longer_than_lease() -> None:
    with pytest.raises(_ConfigError(), match="shutdown_grace_seconds"):
        _Config(poll_seconds=1.0, lease_seconds=10.0, shutdown_grace_seconds=60.0)


def test_config_rejects_non_positive_batch() -> None:
    with pytest.raises(_ConfigError(), match="batch_size"):
        _Config(embedding_batch_size=0)


def test_config_rejects_non_positive_interval() -> None:
    with pytest.raises(_ConfigError(), match="interval_seconds"):
        _Config(expiration_interval_seconds=0)


def test_config_defaults_are_sane() -> None:
    c = _Config()
    assert c.enabled is False
    assert c.poll_seconds == 5.0
    assert c.lease_seconds == 120.0
    assert c.job_interval_seconds("embedding_outbox") == 10
    assert c.job_batch_size("purge") == 1000


# --------------------------------------------------------------------------- #
# load_maintenance_config（环境变量解析）
# --------------------------------------------------------------------------- #


def test_load_config_defaults() -> None:
    c = config_module.load_maintenance_config({})
    assert c.enabled is False
    assert c.poll_seconds == 5.0
    assert c.lease_seconds == 120.0
    assert c.dry_run is False


def test_load_config_reads_overrides() -> None:
    c = config_module.load_maintenance_config(
        {
            "KLONET_AGENT_MAINTENANCE_ENABLED": "true",
            "KLONET_AGENT_MAINTENANCE_POLL_SECONDS": "2.5",
            "KLONET_AGENT_MAINTENANCE_LEASE_SECONDS": "60",
            "KLONET_AGENT_MAINTENANCE_DRY_RUN": "yes",
            "KLONET_AGENT_MAINTENANCE_EMBEDDING_BATCH_SIZE": "7",
        }
    )
    assert c.enabled is True
    assert c.poll_seconds == 2.5
    assert c.lease_seconds == 60.0
    assert c.dry_run is True
    assert c.embedding_batch_size == 7


def test_load_config_rejects_non_numeric() -> None:
    with pytest.raises(_ConfigError(), match="POLL_SECONDS"):
        config_module.load_maintenance_config({"KLONET_AGENT_MAINTENANCE_POLL_SECONDS": "abc"})


def test_load_config_rejects_bad_bool() -> None:
    with pytest.raises(_ConfigError(), match="ENABLED"):
        config_module.load_maintenance_config({"KLONET_AGENT_MAINTENANCE_ENABLED": "maybe"})


def test_load_config_blank_value_uses_default() -> None:
    c = config_module.load_maintenance_config({"KLONET_AGENT_MAINTENANCE_POLL_SECONDS": "  "})
    assert c.poll_seconds == 5.0


# --------------------------------------------------------------------------- #
# job registry
# --------------------------------------------------------------------------- #


def test_register_rejects_unknown_job_name() -> None:
    service = MaintenanceService(FakeRepository([]), _config())
    with pytest.raises(UnknownJobError, match="未知 job name"):
        service.register(FakeJob("no_such_job"))


def test_register_rejects_alias_name() -> None:
    service = MaintenanceService(FakeRepository([]), _config())
    with pytest.raises(UnknownJobError):
        service.register(FakeJob("embedding"))  # 别名不允许作为注册名


def test_register_rejects_duplicate() -> None:
    service = MaintenanceService(FakeRepository([]), _config())
    service.register(FakeJob("expiration"))
    with pytest.raises(UnknownJobError, match="重复"):
        service.register(FakeJob("expiration"))


def test_register_rejects_missing_run() -> None:
    class _NoRun:
        name = "purge"

    service = MaintenanceService(FakeRepository([]), _config())
    with pytest.raises(UnknownJobError, match="run"):
        service.register(_NoRun())  # type: ignore[arg-type]


def test_resolve_job_name_aliases() -> None:
    assert resolve_job_name("embedding") == "embedding_outbox"
    assert resolve_job_name("health") == "health_report"
    assert resolve_job_name("purge") == "purge"
    with pytest.raises(UnknownJobError):
        resolve_job_name("")


# --------------------------------------------------------------------------- #
# 停止语义
# --------------------------------------------------------------------------- #


def test_run_forever_returns_immediately_when_stop_already_requested() -> None:
    repo = FakeRepository([FakeClaim("expiration")])
    service = MaintenanceService(repo, _config(), jobs=[FakeJob("expiration")])
    service.request_stop()
    ticks = service.run_forever()
    assert ticks == 0
    assert repo.claim_calls == []  # 一个都没领


def test_job_running_requests_stop_then_loop_exits_after_finishing_it() -> None:
    """Job 运行期间收到停止 → 当前 Job 跑完并 complete，然后退出，不再领新 Job。

    这是 04 计划 §8.2 "当前事务在 grace period 内完成" 的可执行形式。
    """

    repo = FakeRepository([FakeClaim("expiration"), FakeClaim("expiration", run_id="run-2")])
    service = MaintenanceService(repo, _config(), jobs=[FakeJob("expiration")])

    def _on_run(context: JobContext) -> None:
        service.request_stop()

    job = FakeJob("expiration", on_run=_on_run)
    service = MaintenanceService(repo, _config(), jobs=[job])
    ticks = service.run_forever()
    assert ticks == 1, "停止后不应再进入第二个 tick"
    assert len(job.calls) == 1
    assert len(repo.completed) == 1, "当前 Job 必须被正常收尾"
    assert repo.completed[0]["run_id"] == "run-1"
    # 第二个 claim 不该被消费
    assert len(repo.claim_calls) == 1
    status = service.status()
    assert status.stopping is True
    assert status.succeeded == 1


def test_idle_uses_interruptible_sleep() -> None:
    """无 due Job 时走等待；等待被调用次数 == 空转次数。"""

    slept: list[float] = []
    repo = FakeRepository([])  # 永远没有 due
    service = MaintenanceService(
        repo, _config(poll_seconds=2.0), jobs=[FakeJob("expiration")], wait=slept.append
    )
    ticks = service.run_forever(max_ticks=3)
    assert ticks == 3
    assert slept == [2.0, 2.0, 2.0]
    assert service.status().idle_sleeps == 3
    assert repo.claim_calls, "每 tick 都应尝试 claim"


def test_install_signal_handlers_requests_stop() -> None:
    import signal

    service = MaintenanceService(FakeRepository([]), _config(), jobs=[FakeJob("expiration")])
    original = signal.getsignal(signal.SIGTERM)
    try:
        service.install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert service.status().stopping is True
    finally:
        signal.signal(signal.SIGTERM, original)


# --------------------------------------------------------------------------- #
# 执行语义
# --------------------------------------------------------------------------- #


def test_job_exception_is_recorded_and_does_not_bubble() -> None:
    repo = FakeRepository([FakeClaim("expiration"), FakeClaim("expiration", run_id="run-2")])
    boom = RuntimeError("job blew up")
    service = MaintenanceService(
        repo, _config(), jobs=[FakeJob("expiration", raises=boom)]
    )
    service.run_once()
    # run_once 默认 max_runs=1（无 job_name）→ 只跑一个。再 run 一次跑第二个。
    service.run_once()
    assert len(repo.failed) == 2
    assert repo.failed[0]["error_code"] == "RuntimeError"
    assert "job blew up" in repo.failed[0]["error_summary"]
    assert repo.completed == []
    assert service.status().failed == 2


def test_unknown_job_in_db_is_failed_not_skipped() -> None:
    repo = FakeRepository([FakeClaim("ghost_job")])
    service = MaintenanceService(repo, _config(), jobs=[FakeJob("expiration")])
    service.run_once()
    assert len(repo.failed) == 1
    assert repo.failed[0]["error_code"] == "unknown_job"
    assert "ghost_job" in repo.failed[0]["error_summary"]


def test_non_jobresult_return_is_failed() -> None:
    class _BadJob:
        name = "expiration"

        def run(self, context, cursor):
            return "not-a-result"

    repo = FakeRepository([FakeClaim("expiration")])
    service = MaintenanceService(repo, _config(), jobs=[_BadJob()])
    service.run_once()
    assert repo.failed and repo.failed[0]["error_code"] == "bad_job_result"


def test_complete_uses_job_interval_from_config() -> None:
    repo = FakeRepository([FakeClaim("purge")])
    service = MaintenanceService(repo, _config(), jobs=[FakeJob("purge")])
    service.run_once()
    assert repo.completed[0]["next_run_seconds"] == 86400


def test_dry_run_is_propagated_from_config_and_claim() -> None:
    seen: list[JobContext] = []

    def _capture(context: JobContext) -> None:
        seen.append(context)

    repo = FakeRepository([FakeClaim("expiration", config={"dry_run": True})])
    service = MaintenanceService(
        repo, _config(dry_run=False), jobs=[FakeJob("expiration", on_run=_capture)]
    )
    service.run_once()
    assert seen and seen[0].dry_run is True

    seen.clear()
    repo2 = FakeRepository([FakeClaim("expiration")])
    service2 = MaintenanceService(
        repo2, _config(dry_run=True), jobs=[FakeJob("expiration", on_run=_capture)]
    )
    service2.run_once()
    assert seen and seen[0].dry_run is True


def test_batch_limit_falls_back_to_config_when_claim_missing() -> None:
    seen: list[JobContext] = []

    def _capture(context: JobContext) -> None:
        seen.append(context)

    repo = FakeRepository([FakeClaim("purge", batch_limit=0)])
    service = MaintenanceService(
        repo, _config(), jobs=[FakeJob("purge", on_run=_capture)]
    )
    service.run_once()
    assert seen and seen[0].batch_limit == _config().job_batch_size("purge")


def test_run_once_filters_by_job_and_force() -> None:
    repo = FakeRepository([FakeClaim("expiration")])
    service = MaintenanceService(repo, _config(), jobs=[FakeJob("expiration")])
    service.run_once(job_name="expiration", force=True)
    assert repo.claim_calls[-1]["job_name"] == "expiration"
    assert repo.claim_calls[-1]["force"] is True


def test_run_once_rejects_unregistered_job() -> None:
    service = MaintenanceService(FakeRepository([]), _config(), jobs=[FakeJob("expiration")])
    with pytest.raises(UnknownJobError, match="未注册"):
        service.run_once(job_name="purge")


def test_run_once_returns_empty_when_no_due() -> None:
    service = MaintenanceService(FakeRepository([]), _config(), jobs=[FakeJob("expiration")])
    assert service.run_once() == []


def test_claim_exception_is_swallowed_and_retried() -> None:
    class _BrokenRepo(FakeRepository):
        def claim_due_job(self, *, worker_id, lease_seconds, job_name=None, force=False):
            raise RuntimeError("connection reset")

    repo = _BrokenRepo([])
    service = MaintenanceService(
        repo, _config(), jobs=[FakeJob("expiration")], wait=lambda _s: None
    )
    ticks = service.run_forever(max_ticks=2)
    assert ticks == 2
    assert "RuntimeError" in (service.status().last_error or "")
