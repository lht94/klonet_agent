"""记忆系统专项评测（02 计划阶段 7）。

作用是把"记忆库能不能替代 Markdown 记忆"变成可重放、可对比的数字，并在
cutover 之前先冻结验收阈值（见 ``evals/memory_eval_thresholds.json``）。

三条被测路径（arm）：

1. ``markdown``：当前生产行为——把该租户的全部记忆正文一次性注入上下文。
   它天然"全召回"，代价是 token 最高，且**过期/被替代的旧值也会被注入**
   （这正是要修的"过期误召回"）。
2. ``db_keyword``：数据库路径但只开全文 + 精确标识符通道（没有向量）。
3. ``db_hybrid``：数据库路径完整形态——全文 + 向量 + 精确，加权 RRF 融合，
   并标注 ``contradicts`` 冲突。

指标：recall@k、precision@k、过期误召回率、跨租户泄漏率、冲突标注率、
平均注入 token。

**为什么用确定性哈希嵌入而不是真实供应商**：评测必须离线可重放，且这一版
要量的是**管线**（三通道 + RRF + 冲突标注能不能把语义命中用起来），不是某个
供应商的向量质量。用例里的 ``concepts`` 是给语义通道注入的确定性概念标记，
用来模拟"词面不同、语义相同"。真实语义增益只能在线上用真模型测。

用法：
    PYTHONPATH=<仓库父目录> KLONET_AGENT_TEST_PG_DSN=... \
        python -m evals.run_memory_eval
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from klonet_agent.config import PROJECT_ROOT

_CASE_FILE = PROJECT_ROOT / "evals" / "memory_cases.jsonl"
_THRESHOLD_FILE = PROJECT_ROOT / "evals" / "memory_eval_thresholds.json"
_OUTPUT_FILE = PROJECT_ROOT / "evals" / "memory_summary.md"

_TEST_DSN_ENV = "KLONET_AGENT_TEST_PG_DSN"

ARMS = ("markdown", "db_keyword", "db_hybrid")


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 确定性嵌入
# --------------------------------------------------------------------------- #


class HashingEmbedder:
    """稳定词袋嵌入：blake2b 哈希到固定维度后做 L2 归一化。

    用 ``blake2b`` 而不是内置 ``hash()``——后者按进程加盐，跨进程结果不同，
    评测就没法重放了。
    """

    def __init__(self, dimensions: int):
        self.dimensions = int(dimensions)

    def _tokens(self, text: str) -> list[str]:
        try:
            from klonet_agent.knowledge.tokenizer import DEFAULT_TOKENIZER

            return list(DEFAULT_TOKENIZER.tokenize(text))
        except Exception:  # pragma: no cover - 分词依赖缺失时退化
            return str(text).split()

    def _index(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dimensions

    def __call__(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimensions
        for token in self._tokens(text):
            vector[self._index(token)] += 1.0
        norm = sum(value * value for value in vector) ** 0.5
        if norm == 0.0:
            return ()
        return tuple(value / norm for value in vector)


@dataclass(frozen=True)
class _QueryEmbedder:
    """把用例声明的查询侧 ``concepts`` 拼进查询文本再嵌入。"""

    base: HashingEmbedder
    concepts: tuple[str, ...] = ()

    def __call__(self, text: str) -> tuple[float, ...]:
        return self.base(str(text) + "\n" + " ".join(self.concepts))


def _embedding_text(entry: Mapping[str, Any]) -> str:
    concepts = entry.get("concepts") or []
    return str(entry["content"]) + "\n" + " ".join(str(item) for item in concepts)


# --------------------------------------------------------------------------- #
# 用例装载
# --------------------------------------------------------------------------- #


def _load_cases() -> list[dict]:
    cases: list[dict] = []
    for line in _CASE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def _load_thresholds() -> dict:
    if not _THRESHOLD_FILE.exists():
        return {}
    return json.loads(_THRESHOLD_FILE.read_text(encoding="utf-8"))


def _limits_of(thresholds: Mapping[str, Any]) -> Mapping[str, Any]:
    """冻结文件里的 ``thresholds`` 段；兼容"直接把阈值写在顶层"的简写。"""

    nested = thresholds.get("thresholds")
    if isinstance(nested, Mapping):
        return nested
    return thresholds


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #


@dataclass
class QueryRow:
    case_id: str
    query: str
    as_of: str | None
    arm: str
    hit_keys: tuple[str, ...]
    recall_at_k: float
    precision_at_k: float
    expired_false_recall: bool
    leaked: bool
    conflict_detected: bool | None
    conflict_expected: bool
    hybrid_only: bool
    tokens: int


@dataclass
class CaseOutcome:
    case_id: str
    description: str
    rows: list[QueryRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


# --------------------------------------------------------------------------- #
# 真库：建库、播种、召回
# --------------------------------------------------------------------------- #


def _command(entry: Mapping[str, Any], tenant: Any) -> Any:
    from klonet_agent.memory.domain import (
        MemorySource,
        MemoryType,
        Scope,
        SourceType,
    )
    from klonet_agent.memory.repository import NewRecordCommand

    scope = Scope(str(entry["scope"]))
    return NewRecordCommand(
        user_id=tenant.user_id,
        scope=scope,
        memory_type=MemoryType(str(entry["type"])),
        subject_key=str(entry["subject"]),
        content=str(entry["content"]),
        project_id=tenant.project_id if scope is Scope.PROJECT else None,
        confidence=float(entry.get("confidence", 0.7)),
        valid_from=_dt(entry.get("valid_from")),
        sources=(
            MemorySource(
                source_type=SourceType.USER_STATEMENT,
                source_id="rows-1",
                observed_at=_now(),
                source_excerpt=str(entry["content"])[:80],
            ),
        ),
    )


def _seed_tenant(repo: Any, embedder: HashingEmbedder, entries: Sequence[Mapping]) -> dict:
    """按声明顺序播种一个租户，返回 ``{key: MemoryRecord}``。"""

    created: dict[str, Any] = {}
    for entry in entries:
        key = str(entry["key"])
        command = _command(entry, repo.tenant)
        parent = entry.get("replaces")
        if parent:
            record = repo.replace_active(
                created[str(parent)].id, command, reason="eval-supersede"
            )
        else:
            record = repo.add_record(command)
        created[key] = record
        valid_to = _dt(entry.get("valid_to"))
        if valid_to is not None:
            repo.mark_expired(record.id, valid_to)
        for target in entry.get("contradicts") or []:
            repo.add_relation(record.id, "contradicts", created[str(target)].id)

    # 向量统一在最后写：先写正文再补向量，与运行时的"正文先提交"一致。
    fresh: dict[str, Any] = {}
    for entry in entries:
        key = str(entry["key"])
        record = repo.get_record(created[key].id)
        fresh[key] = record
        vector = embedder(_embedding_text(entry))
        if vector and record is not None and record.active_version_id:
            repo.set_embedding(
                record.active_version_id,
                vector,
                embedding_model="eval-hashing",
                embedding_version="v1",
            )
    return fresh


def _seed_case(
    database: Any,
    embedder: HashingEmbedder,
    case: Mapping[str, Any],
    namespace: str,
) -> tuple:
    """播种主租户与（可选的）外来租户。

    每个用例用**自己的租户**（``namespace`` 后缀）：全部用例共用一个临时库，
    不隔离的话同名的 subject_key 会撞 ``uq_memory_records_active_subject``，
    而且语义通道会把别的用例的记录也召回来，指标就不再有意义。
    """

    from klonet_agent.memory.domain import Tenant
    from klonet_agent.memory.postgres import PostgresMemoryRepository

    def _tenant(block: Mapping[str, Any]) -> Any:
        project_id = block.get("project_id")
        return Tenant(
            user_id=f"{block['user_id']}-{namespace}",
            project_id=f"{project_id}-{namespace}" if project_id else None,
        )

    main_tenant = _tenant(case["tenant"])
    repo = PostgresMemoryRepository(database, main_tenant)
    created = _seed_tenant(repo, embedder, case.get("seed") or [])

    foreign_keys: set[str] = set()
    for block in case.get("foreign") or []:
        foreign_repo = PostgresMemoryRepository(database, _tenant(block["tenant"]))
        foreign_created = _seed_tenant(
            foreign_repo, embedder, block.get("seed") or []
        )
        foreign_keys.update(foreign_created)
    return repo, created, foreign_keys


def _tokens_of(texts: Iterable[str]) -> int:
    from klonet_agent.context.tokens import estimate_tokens

    return int(estimate_tokens("\n".join(texts)))


# --------------------------------------------------------------------------- #
# 三条 arm
# --------------------------------------------------------------------------- #


def _run_query(
    repo: Any,
    embedder: HashingEmbedder,
    query: Mapping[str, Any],
) -> dict:
    """在给定租户仓库上跑一次查询，返回各 arm 的原始结果。"""

    from klonet_agent.memory.domain import MemoryQuery
    from klonet_agent.memory.retriever import MemoryRetriever

    limit = int(query.get("limit") or 5)
    as_of = _dt(query.get("as_of"))
    text = str(query["text"])
    concepts = tuple(str(item) for item in (query.get("concepts") or []))

    keyword_query = MemoryQuery(text=text, limit=limit, as_of=as_of)
    keyword_hits = repo.search(keyword_query)

    retriever = MemoryRetriever(repo, embedder=_QueryEmbedder(embedder, concepts))
    report = retriever.retrieve(MemoryQuery(text=text, limit=limit, as_of=as_of))

    return {
        "keyword": [(hit.record.id, hit.version.content) for hit in keyword_hits],
        "hybrid": [(hit.record.id, hit.version.content) for hit in report.hits],
        "hybrid_conflict_ids": list(report.conflict_ids),
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def _evaluate_rows(
    case: Mapping[str, Any],
    query: Mapping[str, Any],
    arm: str,
    hit_keys: Sequence[str],
    contents: Sequence[str],
    id_to_key: Mapping[str, str],
    foreign_keys: set[str],
    *,
    conflict_detected: bool | None,
) -> QueryRow:
    expect = query.get("expect") or {}
    relevant = [str(item) for item in (expect.get("relevant") or [])]
    must_not = set(str(item) for item in (expect.get("must_not_recall") or []))

    hit_set = set(hit_keys)
    found = [key for key in relevant if key in hit_set]
    recall = (len(found) / len(relevant)) if relevant else 1.0
    precision = (len(found) / max(len(hit_keys), 1)) if relevant else 0.0

    return QueryRow(
        case_id=str(case["case_id"]),
        query=str(query["text"]),
        as_of=str(query.get("as_of") or "") or None,
        arm=arm,
        hit_keys=tuple(hit_keys),
        recall_at_k=round(recall, 4),
        precision_at_k=round(precision, 4),
        expired_false_recall=bool(must_not & hit_set),
        leaked=bool(set(hit_keys) & foreign_keys),
        conflict_detected=conflict_detected,
        conflict_expected=bool(expect.get("conflict")),
        hybrid_only=bool(expect.get("hybrid_only")),
        tokens=_tokens_of(contents),
    )


def _run_case(
    database: Any,
    embedder: HashingEmbedder,
    case: Mapping[str, Any],
    namespace: str,
) -> CaseOutcome:
    outcome = CaseOutcome(
        case_id=str(case["case_id"]), description=str(case.get("description") or "")
    )
    try:
        repo, created, foreign_keys = _seed_case(database, embedder, case, namespace)
    except Exception as exc:  # noqa: BLE001 - 单个用例失败不影响其它用例
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.notes.append(traceback.format_exc(limit=3))
        return outcome

    id_to_key = {record.id: key for key, record in created.items()}
    markdown_keys = [str(entry["key"]) for entry in (case.get("seed") or [])]
    markdown_texts = [str(entry["content"]) for entry in (case.get("seed") or [])]

    for query in case.get("queries") or []:
        # arm 1：Markdown baseline —— 全部正文一次性注入。
        outcome.rows.append(
            _evaluate_rows(
                case,
                query,
                "markdown",
                markdown_keys,
                markdown_texts,
                id_to_key,
                foreign_keys,
                conflict_detected=None,
            )
        )

        try:
            raw = _run_query(repo, embedder, query)
        except Exception as exc:  # noqa: BLE001 - 诊断为重
            outcome.error = f"query_failed: {type(exc).__name__}: {exc}"
            outcome.notes.append(traceback.format_exc(limit=3))
            continue

        for arm, key in (("db_keyword", "keyword"), ("db_hybrid", "hybrid")):
            pairs = raw[key]
            keys = [id_to_key.get(record_id, f"<unknown:{record_id}>") for record_id, _ in pairs]
            contents = [content for _, content in pairs]
            conflict_detected = None
            if arm == "db_hybrid":
                conflict_detected = bool(
                    set(raw["hybrid_conflict_ids"]) & set(record_id for record_id, _ in pairs)
                )
            outcome.rows.append(
                _evaluate_rows(
                    case,
                    query,
                    arm,
                    keys,
                    contents,
                    id_to_key,
                    foreign_keys,
                    conflict_detected=conflict_detected,
                )
            )
    return outcome


def _aggregate(outcomes: Sequence[CaseOutcome], arm: str) -> dict:
    rows = [row for outcome in outcomes for row in outcome.rows if row.arm == arm]
    scored = [row for row in rows if row.hit_keys or row.recall_at_k > 0]
    return {
        "queries": len(rows),
        "recall_at_k": round(
            sum(row.recall_at_k for row in rows) / len(rows), 4
        ) if rows else None,
        "precision_at_k": round(
            sum(row.precision_at_k for row in rows) / len(rows), 4
        ) if rows else None,
        "expired_false_recall_rate": round(
            sum(1 for row in rows if row.expired_false_recall) / len(rows), 4
        ) if rows else None,
        "leakage_rate": round(
            sum(1 for row in rows if row.leaked) / len(rows), 4
        ) if rows else None,
        "mean_tokens": round(
            sum(row.tokens for row in rows) / len(rows), 2
        ) if rows else None,
        "answered_queries": len(scored),
    }


def _conflict_rate(outcomes: Sequence[CaseOutcome]) -> float | None:
    rows = [
        row
        for outcome in outcomes
        for row in outcome.rows
        if row.arm == "db_hybrid" and row.conflict_expected
    ]
    if not rows:
        return None
    return round(sum(1 for row in rows if row.conflict_detected) / len(rows), 4)


def _hybrid_only_wins(outcomes: Sequence[CaseOutcome]) -> tuple[int, int]:
    """返回 ``(混合检索赢下的语义用例数, 语义用例总数)``。"""

    total = 0
    won = 0
    keys = {(outcome.case_id, row.query, row.as_of) for outcome in outcomes for row in outcome.rows}
    for case_id, query, as_of in sorted(keys, key=str):
        rows = {
            row.arm: row
            for outcome in outcomes
            for row in outcome.rows
            if row.case_id == case_id and row.query == query and row.as_of == as_of
        }
        hybrid = rows.get("db_hybrid")
        keyword = rows.get("db_keyword")
        if hybrid is None or keyword is None or not hybrid.hybrid_only:
            continue
        total += 1
        if hybrid.recall_at_k > keyword.recall_at_k:
            won += 1
    return won, total


def _check_thresholds(outcomes: Sequence[CaseOutcome], thresholds: Mapping[str, Any]) -> list[dict]:
    aggregates = {arm: _aggregate(outcomes, arm) for arm in ARMS}
    hybrid = aggregates["db_hybrid"]
    keyword = aggregates["db_keyword"]
    markdown = aggregates["markdown"]
    conflict_rate = _conflict_rate(outcomes)
    won, total = _hybrid_only_wins(outcomes)

    checks: list[dict] = []

    def add(name: str, value: Any, threshold: Any, ok: bool, note: str = "") -> None:
        checks.append(
            {
                "name": name,
                "value": value,
                "threshold": threshold,
                "ok": bool(ok),
                "note": note,
            }
        )

    if hybrid.get("recall_at_k") is not None:
        threshold = thresholds.get("hybrid_recall_at_k_min")
        add(
            "hybrid_recall_at_k_min",
            hybrid["recall_at_k"],
            threshold,
            threshold is not None and hybrid["recall_at_k"] >= float(threshold),
        )
    if hybrid.get("precision_at_k") is not None:
        threshold = thresholds.get("hybrid_precision_at_k_min")
        add(
            "hybrid_precision_at_k_min",
            hybrid["precision_at_k"],
            threshold,
            threshold is not None and hybrid["precision_at_k"] >= float(threshold),
        )
    if keyword.get("recall_at_k") is not None:
        threshold = thresholds.get("keyword_recall_at_k_min")
        add(
            "keyword_recall_at_k_min",
            keyword["recall_at_k"],
            threshold,
            threshold is not None and keyword["recall_at_k"] >= float(threshold),
        )
    if hybrid.get("expired_false_recall_rate") is not None:
        threshold = thresholds.get("expired_false_recall_max")
        add(
            "expired_false_recall_max",
            hybrid["expired_false_recall_rate"],
            threshold,
            threshold is not None
            and hybrid["expired_false_recall_rate"] <= float(threshold),
            note=f"Markdown baseline 为 {markdown.get('expired_false_recall_rate')}",
        )
    if hybrid.get("leakage_rate") is not None:
        threshold = thresholds.get("cross_tenant_leakage_max")
        add(
            "cross_tenant_leakage_max",
            hybrid["leakage_rate"],
            threshold,
            threshold is not None and hybrid["leakage_rate"] <= float(threshold),
        )
    if conflict_rate is not None:
        threshold = thresholds.get("conflict_flag_rate_min")
        add(
            "conflict_flag_rate_min",
            conflict_rate,
            threshold,
            threshold is not None and conflict_rate >= float(threshold),
        )
    if (
        hybrid.get("mean_tokens") is not None
        and markdown.get("mean_tokens")
    ):
        ratio = round(hybrid["mean_tokens"] / markdown["mean_tokens"], 4)
        threshold = thresholds.get("hybrid_token_ratio_vs_markdown_max")
        add(
            "hybrid_token_ratio_vs_markdown_max",
            ratio,
            threshold,
            threshold is not None and ratio <= float(threshold),
            note=f"markdown 平均 token = {markdown.get('mean_tokens')}",
        )
    if (
        hybrid.get("recall_at_k") is not None
        and keyword.get("recall_at_k") is not None
    ):
        add(
            "hybrid_recall_not_below_keyword",
            round(hybrid["recall_at_k"] - keyword["recall_at_k"], 4),
            0.0,
            hybrid["recall_at_k"] >= keyword["recall_at_k"],
        )
    if total:
        add(
            "hybrid_only_cases_won",
            won,
            total,
            won == total,
            note="语义用例必须由混合检索赢下（词面不同、语义相同）",
        )
    return checks


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def _render(
    outcomes: Sequence[CaseOutcome],
    checks: Sequence[Mapping[str, Any]],
    thresholds: Mapping[str, Any],
    *,
    dsn_available: bool,
) -> str:
    aggregates = {arm: _aggregate(outcomes, arm) for arm in ARMS}
    lines = [
        "# Klonet 记忆系统专项评测",
        "",
        f"- cases: {len(outcomes)}",
        f"- 真库：{'可用' if dsn_available else '不可用（只跑了 Markdown baseline）'}",
        f"- 阈值冻结时间：{thresholds.get('frozen_at') or '未冻结'}",
        "",
        "## 三条路径总览",
        "",
        "| arm | 查询数 | recall@k | precision@k | 过期误召回率 | 跨租户泄漏率 | 平均 token |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in ARMS:
        item = aggregates[arm]
        lines.append(
            "| {arm} | {queries} | {recall} | {precision} | {expired} | {leak} | {tokens} |".format(
                arm=arm,
                queries=item["queries"],
                recall=_fmt(item["recall_at_k"]),
                precision=_fmt(item["precision_at_k"]),
                expired=_fmt(item["expired_false_recall_rate"]),
                leak=_fmt(item["leakage_rate"]),
                tokens=_fmt(item["mean_tokens"], digits=2),
            )
        )
    conflict_rate = _conflict_rate(outcomes)
    won, total = _hybrid_only_wins(outcomes)
    lines.extend(
        [
            "",
            f"- 冲突标注率（混合检索，仅冲突用例）：{_fmt(conflict_rate)}",
            f"- 语义用例由混合检索赢下：{won}/{total}",
            "",
            "## 阈值核验",
            "",
        ]
    )
    if checks:
        lines.extend(
            [
                "| 检查项 | 实测 | 阈值 | 结果 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for check in checks:
            lines.append(
                "| {name} | {value} | {threshold} | {ok} |".format(
                    name=check["name"],
                    value=check["value"],
                    threshold=check["threshold"],
                    ok="PASS" if check["ok"] else "FAIL",
                )
            )
    else:
        lines.append("（真库不可用，未做阈值核验）")

    lines.extend(["", "## 逐条明细", ""])
    for outcome in outcomes:
        status = "ERROR" if outcome.error else "OK"
        lines.append(f"### {outcome.case_id}（{status}）")
        lines.append(f"- 说明：{outcome.description}")
        if outcome.error:
            lines.append(f"- 错误：{outcome.error}")
        for row in outcome.rows:
            lines.append(
                f"- `{row.arm}`｜as_of={row.as_of or '-'}｜recall={row.recall_at_k}"
                f"｜precision={row.precision_at_k}｜token={row.tokens}"
                f"｜过期误召回={'是' if row.expired_false_recall else '否'}"
                f"｜泄漏={'是' if row.leaked else '否'}"
                f"｜命中={list(row.hit_keys)}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _fmt(value: Any, *, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def _markdown_only_outcomes(
    embedder: HashingEmbedder, cases: Sequence[Mapping[str, Any]]
) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        outcome = CaseOutcome(
            case_id=str(case["case_id"]),
            description=str(case.get("description") or ""),
        )
        seed = case.get("seed") or []
        keys = [str(entry["key"]) for entry in seed]
        texts = [str(entry["content"]) for entry in seed]
        for query in case.get("queries") or []:
            outcome.rows.append(
                _evaluate_rows(
                    case, query, "markdown", keys, texts, {}, set(), conflict_detected=None
                )
            )
        outcomes.append(outcome)
    return outcomes


def main() -> int:
    cases = _load_cases()
    thresholds = _load_thresholds()
    dsn = (os.environ.get(_TEST_DSN_ENV) or "").strip()

    if not dsn:
        outcomes = _markdown_only_outcomes(HashingEmbedder(1024), cases)
        _OUTPUT_FILE.write_text(
            _render(outcomes, [], thresholds, dsn_available=False), encoding="utf-8"
        )
        print(
            f"memory eval: 未设置 {_TEST_DSN_ENV}，只生成了 Markdown baseline；"
            f"summary: {_OUTPUT_FILE}"
        )
        return 0

    from klonet_agent.memory.database import MemoryDatabase, temporary_database
    from klonet_agent.memory.repository import EMBEDDING_DIMENSIONS

    embedder = HashingEmbedder(EMBEDDING_DIMENSIONS)

    outcomes: list[CaseOutcome] = []
    with temporary_database(dsn) as temp_dsn:
        database = MemoryDatabase(temp_dsn, min_size=1, max_size=4)
        database.open()
        try:
            database.run_migrations()
            for index, case in enumerate(cases, start=1):
                outcomes.append(_run_case(database, embedder, case, f"c{index}"))
        finally:
            database.close()

    checks = _check_thresholds(outcomes, _limits_of(thresholds))
    _OUTPUT_FILE.write_text(
        _render(outcomes, checks, thresholds, dsn_available=True), encoding="utf-8"
    )

    failed = [check["name"] for check in checks if not check["ok"]]
    errors = [outcome.case_id for outcome in outcomes if outcome.error]
    print(f"memory eval: {len(cases)} cases, 阈值核验 {len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        print("未达标：" + ", ".join(failed))
    if errors:
        print("用例错误：" + ", ".join(errors))
    print(f"summary: {_OUTPUT_FILE}")
    return 1 if (failed or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
