"""维护 Worker CLI 测试（04 计划 §8.1 / 阶段 2）。

覆盖稳定退出码与命令行为；**不连真库**——``_build_service`` 被替换成
测试替身。

退出码契约：
* 0 正常；1 运行故障；2 参数错误；3 配置错误
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from klonet_agent.config import MaintenanceConfig, MaintenanceConfigError
from klonet_agent.memory.maintenance import cli
from klonet_agent.memory.maintenance.base import JobContext, JobResult
from klonet_agent.memory.maintenance.service import MaintenanceService, ServiceStatus


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class _FakeJob:
    def __init__(self, name: str, result: JobResult | None = None):
        self.name = name
        self._result = result or JobResult(scanned=3, changed=2)
        self.calls = 0

    def run(self, context: JobContext, cursor) -> JobResult:
        self.calls += 1
        return self._result


class _FakeClaim:
    def __init__(self, job_name: str):
        now = datetime.now(timezone.utc)
        self.job_name = job_name
        self.run_id = "run-x"
        self.started_at = now
        self.deadline = now
        self.cursor = None
        self.config = {}
        self.batch_limit = 5


class _FakeRepo:
    def __init__(self, claims):
        self._claims = list(claims)
        self.completed = []
        self.failed = []

    def claim_due_job(self, *, worker_id, lease_seconds, job_name=None, force=False):
        return self._claims.pop(0) if self._claims else None

    def complete(self, *, job_name, run_id, result, next_run_seconds):
        self.completed.append((job_name, result))

    def fail(self, *, job_name, run_id, error_code, error_summary, next_run_seconds):
        self.failed.append((job_name, error_code))


class _FakeDb:
    def __init__(self, *, is_open=True):
        self.is_open = is_open
        self.closed = False

    def close(self):
        self.closed = True


def _service(repo, jobs) -> MaintenanceService:
    return MaintenanceService(
        repo,
        MaintenanceConfig(
            enabled=True,
            poll_seconds=1.0,
            shutdown_grace_seconds=5.0,
            lease_seconds=60.0,
        ),
        jobs=jobs,
        worker_id="test-worker",
        wait=lambda _s: None,
    )


# --------------------------------------------------------------------------- #
# 退出码：参数与配置错误
# --------------------------------------------------------------------------- #


def test_no_command_prints_help_and_returns_usage(capsys) -> None:
    assert cli.main([]) == cli.EXIT_USAGE


def test_proposals_without_dsn_is_config_error(monkeypatch, capsys) -> None:
    """阶段 5 起 proposals 已实现：没有 DSN 时是配置错误，不再是 usage。"""

    for key in ("KLONET_AGENT_MEMORY_DSN", "MEMORY_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main(["proposals", "list"]) == cli.EXIT_CONFIG
    assert "数据库不可用" in capsys.readouterr().err


def test_proposals_approve_without_id_is_usage_error(monkeypatch, capsys) -> None:
    """有 DSN 但没给 proposal_id → usage（不需要真连库）。"""

    import klonet_agent.memory.database as database_module

    class _FakeDb:
        is_open = True

        def close(self) -> None:
            pass

    monkeypatch.setattr(database_module, "MemoryDatabase", _FakeDatabaseStub)
    assert cli.main(["proposals", "approve"]) == cli.EXIT_USAGE
    assert "proposal_id" in capsys.readouterr().err


class _FakeDatabaseStub:
    """只够 ``proposals`` 分支构造 store 用的替身（不连库）。"""

    def __init__(self, *args, **kwargs):
        self.is_open = True

    @classmethod
    def from_env(cls, **kwargs):
        return cls()

    def open(self):
        return self

    def close(self):
        pass

    def diagnostic_session(self):
        raise AssertionError("不该真的查询数据库")


# --------------------------------------------------------------------------- #
# proposals（阶段 5）
# --------------------------------------------------------------------------- #


def test_proposals_approve_walks_pending_then_applied(monkeypatch, capsys) -> None:
    """``approve`` 必须显式走 pending -> approved -> applied 两步。

    状态机不允许 pending 直接跳 applied（那是"未批准就改正式记忆"），
    所以 CLI 不能只调 ``apply_proposal``。这条测试钉住那个两步。
    """

    from klonet_agent.memory.maintenance import cli as cli_module
    from klonet_agent.memory.maintenance import proposals as proposals_module
    from klonet_agent.memory.maintenance.proposals import (
        ApplyOutcome,
        Proposal,
        ProposalStatus,
        ProposalType,
    )
    from klonet_agent.memory.domain import Scope

    transitions: list[str] = []

    class _Store:
        def __init__(self, database, tenant):
            self._status = ProposalStatus.PENDING

        def get(self, proposal_id):
            return Proposal(
                proposal_id=proposal_id,
                job_name="consolidation",
                user_id="u",
                project_id="p",
                scope=Scope.PROJECT,
                proposal_type=ProposalType.NOOP,
                source_memory_ids=("a", "b"),
                suggested_action={},
                reason_codes=(),
                evidence_refs=(),
                fingerprint="f",
                policy_version="v1",
                model_version=None,
                status=self._status,
                created_at=None,
                reviewed_at=None,
                applied_version_ids=(),
                source_fingerprints={},
            )

        def transition(self, proposal_id, status, **kwargs):
            transitions.append(getattr(status, "value", str(status)))
            self._status = (
                status if isinstance(status, ProposalStatus) else ProposalStatus(str(status))
            )
            return self.get(proposal_id)

    def _fake_apply(store, repository, proposal_id):
        transitions.append("apply")
        store._status = ProposalStatus.APPLIED
        return ApplyOutcome(proposal=store.get(proposal_id), decision="noop")

    monkeypatch.setattr(proposals_module, "ProposalStore", _Store)
    monkeypatch.setattr(proposals_module, "apply_proposal", _fake_apply)
    monkeypatch.setattr(cli_module, "_build_database", lambda: _FakeDatabaseStub())
    monkeypatch.setattr(
        "klonet_agent.memory.postgres.PostgresMemoryRepository",
        lambda database, tenant: object(),
    )

    assert cli.main(["proposals", "approve", "p-1"]) == cli.EXIT_OK
    assert transitions == ["approved", "apply"]


def test_run_without_enabled_is_config_error(monkeypatch, capsys) -> None:
    monkeypatch.delenv("KLONET_AGENT_MAINTENANCE_ENABLED", raising=False)
    assert cli.main(["run"]) == cli.EXIT_CONFIG
    assert "未启用" in capsys.readouterr().err


def test_invalid_config_env_returns_config_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("KLONET_AGENT_MAINTENANCE_POLL_SECONDS", "not-a-number")
    assert cli.main(["status"]) == cli.EXIT_CONFIG
    assert "配置错误" in capsys.readouterr().err


def test_status_without_dsn_is_config_error(monkeypatch, capsys) -> None:
    for key in ("KLONET_AGENT_MEMORY_DSN", "MEMORY_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    assert cli.main(["status"]) == cli.EXIT_CONFIG
    assert "数据库不可用" in capsys.readouterr().err


def test_once_with_unknown_job_is_usage_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("KLONET_AGENT_MEMORY_DSN", "postgresql://placeholder:5432/x")
    assert cli.main(["once", "--job", "nope"]) == cli.EXIT_USAGE
    assert "未知 job name" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# once
# --------------------------------------------------------------------------- #


def test_once_reports_summary_when_work_done(monkeypatch, capsys) -> None:
    repo = _FakeRepo([_FakeClaim("expiration")])
    db = _FakeDb()
    service = _service(repo, [_FakeJob("expiration")])
    monkeypatch.setattr(cli, "_build_service", lambda config: (service, db))

    code = cli.main(["once", "--job", "expiration"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "完成 1 个 Job" in out
    assert '"changed": 2' in out
    assert db.closed is True


def test_once_reports_no_due(monkeypatch, capsys) -> None:
    repo = _FakeRepo([])
    db = _FakeDb()
    service = _service(repo, [_FakeJob("expiration")])
    monkeypatch.setattr(cli, "_build_service", lambda config: (service, db))

    assert cli.main(["once"]) == cli.EXIT_OK
    assert "无到期的 Job" in capsys.readouterr().out


def test_once_dry_run_sets_config_flag(monkeypatch, capsys) -> None:
    captured = {}

    def _fake_build(config):
        captured["config"] = config
        return _service(_FakeRepo([]), [_FakeJob("expiration")]), _FakeDb()

    monkeypatch.setattr(cli, "_build_service", _fake_build)
    cli.main(["once", "--dry-run"])
    assert captured["config"].dry_run is True


def test_once_force_is_passed_through(monkeypatch) -> None:
    repo = _FakeRepo([_FakeClaim("embedding_outbox")])
    calls = []

    def _spy_claim(*, worker_id, lease_seconds, job_name=None, force=False):
        calls.append({"job_name": job_name, "force": force})
        return repo.claim_due_job(
            worker_id=worker_id, lease_seconds=lease_seconds, job_name=job_name, force=force
        )

    repo.claim_due_job = _spy_claim  # type: ignore[assignment]
    service = _service(repo, [_FakeJob("embedding_outbox")])
    monkeypatch.setattr(cli, "_build_service", lambda config: (service, _FakeDb()))
    cli.main(["once", "--job", "embedding", "--force"])
    assert calls[-1] == {"job_name": "embedding_outbox", "force": True}


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_json_shape(monkeypatch, capsys) -> None:
    service = _service(_FakeRepo([]), [_FakeJob("expiration"), _FakeJob("purge")])
    monkeypatch.setattr(cli, "_build_service", lambda config: (service, _FakeDb()))

    assert cli.main(["status", "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["worker_id"] == "test-worker"
    assert payload["enabled"] is True
    assert "expiration" in payload["registered_jobs"]
    assert payload["db_open"] is True
    # health 快照（阶段 0 的占位模块）应被带上
    assert "health" in payload


def test_status_plain_text(monkeypatch, capsys) -> None:
    service = _service(_FakeRepo([]), [_FakeJob("purge")])
    monkeypatch.setattr(cli, "_build_service", lambda config: (service, _FakeDb()))
    assert cli.main(["status"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "worker_id: test-worker" in out
    assert "db_open:   True" in out


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def test_run_force_starts_and_stops_cleanly(monkeypatch) -> None:
    repo = _FakeRepo([])
    db = _FakeDb()
    service = _service(repo, [_FakeJob("expiration")])

    captured = {}

    def _fake_build(config):
        captured["service"] = service
        return service, db

    monkeypatch.setattr(cli, "_build_service", _fake_build)
    monkeypatch.setattr(
        MaintenanceService, "install_signal_handlers", lambda self: None
    )

    def _run_forever(self, *, max_ticks=None):
        self.request_stop()
        return 0

    monkeypatch.setattr(MaintenanceService, "run_forever", _run_forever)

    assert cli.main(["run", "--force"]) == cli.EXIT_OK
    assert db.closed is True


def test_run_enabled_starts_without_force(monkeypatch) -> None:
    monkeypatch.setenv("KLONET_AGENT_MAINTENANCE_ENABLED", "1")
    db = _FakeDb()
    service = _service(_FakeRepo([]), [_FakeJob("expiration")])
    monkeypatch.setattr(cli, "_build_service", lambda config: (service, db))
    monkeypatch.setattr(MaintenanceService, "install_signal_handlers", lambda self: None)
    monkeypatch.setattr(
        MaintenanceService, "run_forever", lambda self, *, max_ticks=None: 0
    )
    assert cli.main(["run"]) == cli.EXIT_OK
    assert db.closed is True


def test_unexpected_error_returns_runtime_code(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli, "_build_service", lambda config: (_ for _ in ()).throw(ValueError("boom"))
    )
    assert cli.main(["status"]) == cli.EXIT_RUNTIME
    assert "运行故障" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Job 发现（_build_jobs）
# --------------------------------------------------------------------------- #


def test_job_module_map_covers_every_known_job() -> None:
    """每个规范 Job 名都必须在 ``JOB_MODULES`` 里有映射。

    回归保护：``embedding_outbox`` 的模块文件叫 ``embedding.py``。如果把
    "模块名 = 规范名"当成约定，Job 会被静默地不注册——单测全绿、``status``
    里少一项。这条断言把"映射必须显式"钉死。
    """

    from klonet_agent.memory.maintenance.jobs import JOB_MODULES
    from klonet_agent.memory.maintenance.service import KNOWN_JOB_NAMES

    missing = [name for name in KNOWN_JOB_NAMES if name not in JOB_MODULES]
    assert missing == [], f"JOB_MODULES 缺少映射：{missing}"


def test_build_jobs_discovers_the_embedding_job(monkeypatch) -> None:
    """有嵌入凭据时，``embedding_outbox`` 必须被发现并注册。"""

    import klonet_agent.llm.embeddings as embeddings

    monkeypatch.setattr(
        embeddings,
        "build_default_embedding_provider",
        lambda: (lambda text: (0.0,) * 1024),
    )
    jobs = cli._build_jobs(database=None, config=MaintenanceConfig())
    names = {job.name for job in jobs}
    assert "embedding_outbox" in names, f"未发现 embedding_outbox：{names}"


def test_build_jobs_skips_embedding_job_without_credentials(monkeypatch) -> None:
    """没有嵌入凭据时不该注册一个"永远跑不动"的 Job。"""

    import klonet_agent.llm.embeddings as embeddings

    monkeypatch.setattr(embeddings, "build_default_embedding_provider", lambda: None)
    jobs = cli._build_jobs(database=None, config=MaintenanceConfig())
    assert "embedding_outbox" not in {job.name for job in jobs}


def test_build_jobs_skips_unimplemented_jobs(monkeypatch) -> None:
    """只有已实现的 Job 会被发现；其余名字安静跳过，不报错。

    阶段 3–7 之后全部六个 KNOWN_JOB_NAMES 都有实现。断言"集合包含"而不是
    "集合等于"——真正要守住的是"没实现/没凭据的不会被凭空注册"。
    """

    import klonet_agent.llm.embeddings as embeddings

    monkeypatch.setattr(embeddings, "build_default_embedding_provider", lambda: None)
    names = {job.name for job in cli._build_jobs(database=None, config=MaintenanceConfig())}
    assert {"expiration", "purge", "consolidation", "health_report"}.issubset(names)
    # 没有嵌入凭据时 embedding_outbox / reembedding 不注册
    #（否则是永远跑不动的 Job）。
    assert "embedding_outbox" not in names
    assert "reembedding" not in names
    # KNOWN_JOB_NAMES 之外的名字绝不能凭空出现。
    from klonet_agent.memory.maintenance.service import KNOWN_JOB_NAMES

    assert not (names - set(KNOWN_JOB_NAMES))


# --------------------------------------------------------------------------- #
# 与 config 的契约
# --------------------------------------------------------------------------- #


def test_config_error_class_is_exported() -> None:
    # cli 依赖 config.MaintenanceConfigError；确保不会 import 到不存在的东西。
    assert issubclass(MaintenanceConfigError, ValueError)
