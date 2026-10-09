"""记忆仓库契约测试（04 计划阶段 4 的"契约先行"部分）。

阶段 4 是整个升级计划里**唯一一处改动既有 ``MemoryRepository`` 契约**的工作量。
计划明确要求"先写契约测试，红了再进入 SQL 实现"——否则等真库集成阶段才发现
keyset 字段形态错了，就会重演 02 阶段 2 "事后才发现 ``as_of`` 视图写错"。

本文件只覆盖**离线**可验证的部分：

* 新增数据类型（``ExpiredCandidate`` / ``ExpiredCandidatePage`` /
  ``ArchivedExpiredBatch`` / ``DeletedBacklog``）的字段与默认值；
* keyset cursor 的编解码形态（``(valid_to, memory_id)``，稳定、可读、
  解析失败返回 ``None`` 而不是猜位置）；
* Protocol 上确实声明了三个新方法（签名层面）。

需要真库的行为（条件更新、跨租户硬过滤、shared_ops 上下文）在
``tests/test_memory_maintenance_expiration.py`` 与
``..._purge.py`` 里覆盖。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from klonet_agent.memory.domain import Scope
from klonet_agent.memory.repository import (
    ArchivedExpiredBatch,
    DeletedBacklog,
    ExpiredCandidate,
    ExpiredCandidatePage,
    MemoryRepository,
    decode_expired_cursor,
    encode_expired_cursor,
)


# --------------------------------------------------------------------------- #
# 数据类型
# --------------------------------------------------------------------------- #


def test_expired_candidate_fields() -> None:
    moment = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    candidate = ExpiredCandidate(
        memory_id="m-1",
        user_id="alice",
        project_id="demo",
        scope=Scope.PROJECT,
        valid_to=moment,
    )
    assert candidate.memory_id == "m-1"
    assert candidate.project_id == "demo"
    assert candidate.scope is Scope.PROJECT
    assert candidate.valid_to == moment


def test_expired_candidate_page_defaults() -> None:
    page = ExpiredCandidatePage()
    assert page.items == ()
    assert page.next_cursor is None


def test_archived_expired_batch_defaults() -> None:
    batch = ArchivedExpiredBatch()
    assert batch.archived == 0
    assert batch.memory_ids == ()
    assert batch.outbox_deferred == 0


def test_deleted_backlog_defaults() -> None:
    backlog = DeletedBacklog()
    assert backlog.count == 0
    assert backlog.oldest_deleted_at is None
    assert backlog.estimated_bytes == 0
    # memory_ids 是 purge 删完抽查召回的依据——只知道数量就无从抽查。
    assert backlog.memory_ids == ()


# --------------------------------------------------------------------------- #
# keyset cursor
# --------------------------------------------------------------------------- #


def test_cursor_round_trip_preserves_instant_and_id() -> None:
    moment = datetime(2026, 10, 9, 12, 34, 56, 789000, tzinfo=timezone.utc)
    cursor = encode_expired_cursor(moment, "11111111-1111-1111-1111-111111111111")
    decoded = decode_expired_cursor(cursor)
    assert decoded is not None
    assert decoded[0] == moment
    assert decoded[1] == "11111111-1111-1111-1111-111111111111"


def test_cursor_is_human_readable() -> None:
    """形态故意可读：分页出错时运维能一眼看出停在哪。"""

    cursor = encode_expired_cursor(
        datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc), "abc"
    )
    assert cursor == "2026-10-09T12:00:00+00:00|abc"


def test_cursor_normalises_timezone() -> None:
    """同一个瞬间的不同时区表示必须产生同一个 cursor，否则断点会错位。"""

    utc = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)
    plus8 = utc.astimezone(timezone(timedelta(hours=8)))
    assert encode_expired_cursor(utc, "abc") == encode_expired_cursor(plus8, "abc")


def test_cursor_treats_naive_datetime_as_utc() -> None:
    naive = datetime(2026, 10, 9, 4, 0)
    decoded = decode_expired_cursor(encode_expired_cursor(naive, "abc"))
    assert decoded is not None
    assert decoded[0] == datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)


def test_cursor_garbage_decodes_to_none() -> None:
    """不认识的形态一律 None（从头重扫），绝不"猜一个位置"跳过一批。"""

    assert decode_expired_cursor(None) is None
    assert decode_expired_cursor("") is None
    assert decode_expired_cursor("nope") is None
    assert decode_expired_cursor("2026-10-09T12:00:00+00:00|") is None
    assert decode_expired_cursor("not-a-timestamp|abc") is None
    assert decode_expired_cursor("|abc") is None


def test_cursor_rejects_empty_inputs() -> None:
    with pytest.raises(ValueError):
        encode_expired_cursor(None, "abc")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        encode_expired_cursor(datetime.now(timezone.utc), "")


# --------------------------------------------------------------------------- #
# Protocol 签名
# --------------------------------------------------------------------------- #


def test_protocol_declares_new_methods() -> None:
    for name in (
        "list_expired_candidates",
        "archive_expired_batch",
        "inspect_deleted_backlog",
    ):
        assert hasattr(MemoryRepository, name), f"MemoryRepository 缺少 {name}"


def test_list_expired_candidates_signature_is_keyword_only() -> None:
    """关键字参数是刻意的：``archive_expired(cutoff, limit)`` 这种位置传参
    极易把两个时间/数字写反，而写反的后果是"删错东西"。"""

    signature = inspect.signature(MemoryRepository.list_expired_candidates)
    for name in ("cutoff", "cursor", "batch_size"):
        assert name in signature.parameters
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_archive_expired_batch_requires_cutoff_and_ids() -> None:
    signature = inspect.signature(MemoryRepository.archive_expired_batch)
    for name in ("memory_ids", "cutoff", "outbox_retry_after"):
        assert name in signature.parameters
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_purge_existing_contract_unchanged() -> None:
    """``purge_deleted`` 的契约在本阶段**不动**：保留期门槛仍然必传。"""

    signature = inspect.signature(MemoryRepository.purge_deleted)
    assert signature.parameters["older_than"].default is inspect.Parameter.empty
    assert signature.parameters["limit"].default == 1000
