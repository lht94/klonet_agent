"""阶段 7：记忆评测资产的离线契约测试。

冻结的阈值只有"每次开工都被校验"才算真的冻结。这里不连数据库，只锁三件事：

1. 用例文件的形状（字段齐全、case_id 唯一、期望能被解释）；
2. 阈值文件覆盖了 runner 会核验的每一项；
3. runner 的聚合与判定逻辑本身是对的（用合成数据喂给它，不依赖真库）。
"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from klonet_agent.config import PROJECT_ROOT


def _load_runner():
    """``evals/`` 的脚本按路径加载；不注册进 ``sys.modules`` 的话
    ``@dataclass`` 拿不到模块命名空间（dataclasses 需要回查 ``sys.modules``）。"""

    path = PROJECT_ROOT / "evals" / "run_memory_eval.py"
    spec = importlib.util.spec_from_file_location("memory_eval_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()

_CASE_FILE = PROJECT_ROOT / "evals" / "memory_cases.jsonl"
_THRESHOLD_FILE = PROJECT_ROOT / "evals" / "memory_eval_thresholds.json"

_REQUIRED_THRESHOLDS = {
    "hybrid_recall_at_k_min",
    "hybrid_precision_at_k_min",
    "keyword_recall_at_k_min",
    "expired_false_recall_max",
    "cross_tenant_leakage_max",
    "conflict_flag_rate_min",
    "hybrid_token_ratio_vs_markdown_max",
}

_REQUIRED_CASE_FIELDS = {"case_id", "description", "tenant", "seed", "queries"}


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return runner._load_cases()


@pytest.fixture(scope="module")
def thresholds() -> dict:
    return json.loads(_THRESHOLD_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 用例文件
# --------------------------------------------------------------------------- #


def test_every_case_has_the_required_shape(cases) -> None:
    assert cases, "评测用例不能为空"
    ids = [case["case_id"] for case in cases]
    assert len(ids) == len(set(ids)), "case_id 必须唯一"

    for case in cases:
        missing = _REQUIRED_CASE_FIELDS - set(case)
        assert not missing, f"{case.get('case_id')} 缺字段 {missing}"
        assert case["queries"], f"{case['case_id']} 至少要有一个查询"
        assert case["seed"], f"{case['case_id']} 至少要播种一条记忆"

        keys = [entry["key"] for entry in case["seed"]]
        assert len(keys) == len(set(keys)), f"{case['case_id']} 的 key 必须唯一"
        # must_not_recall 允许引用外来租户的 key（隔离用例就是这么写的）。
        declared = set(keys)
        for block in case.get("foreign") or []:
            declared.update(entry["key"] for entry in block["seed"])

        for query in case["queries"]:
            assert str(query.get("text") or "").strip(), "查询文本不能为空"
            expect = query.get("expect") or {}
            assert "relevant" in expect, f"{case['case_id']} 的查询必须声明 relevant"
            for key in expect.get("relevant") or []:
                assert key in declared, (
                    f"{case['case_id']} 的 relevant 引用了不存在的 key {key}"
                )
            for key in expect.get("must_not_recall") or []:
                assert key in declared, (
                    f"{case['case_id']} 的 must_not_recall 引用了不存在的 key {key}"
                )


def test_seed_references_are_backward_only(cases) -> None:
    """``replaces`` / ``contradicts`` 只能指向更早的条目，否则播种顺序无法解释。"""

    for case in cases:
        seen: list[str] = []
        for entry in case["seed"]:
            for field in ("replaces", "contradicts"):
                value = entry.get(field)
                targets = value if isinstance(value, list) else ([value] if value else [])
                for target in targets:
                    assert target in seen, (
                        f"{case['case_id']} 的 {entry['key']}.{field} 指向了"
                        f"尚未定义或靠后的条目 {target}"
                    )
            seen.append(entry["key"])


def test_the_six_planned_scenario_kinds_are_covered(cases) -> None:
    """计划 §阶段 7 要求单跳、时间、冲突、多跳、偏好、跨项目隔离六类用例。"""

    ids = {case["case_id"] for case in cases}
    assert "mem_temporal_as_of" in ids, "缺时态用例"
    assert "mem_conflict_flagging" in ids, "缺冲突用例"
    assert "mem_multi_hop" in ids, "缺多跳用例"
    assert "mem_preference_recall" in ids, "缺偏好用例"
    assert "mem_cross_tenant_isolation" in ids, "缺跨租户隔离用例"

    hybrid_only = [
        case["case_id"]
        for case in cases
        for query in case["queries"]
        if (query.get("expect") or {}).get("hybrid_only")
    ]
    assert hybrid_only, "至少要有一个只有混合检索能赢的语义用例"


def test_foreign_tenant_block_is_shaped_correctly(cases) -> None:
    for case in cases:
        for block in case.get("foreign") or []:
            assert set(block) >= {"tenant", "seed"}, "foreign 块必须含 tenant 与 seed"
            assert block["seed"], "foreign 块不能为空"


# --------------------------------------------------------------------------- #
# 阈值文件
# --------------------------------------------------------------------------- #


def test_threshold_file_covers_every_checkable_metric(thresholds) -> None:
    limits = runner._limits_of(thresholds)
    missing = _REQUIRED_THRESHOLDS - set(limits)
    assert not missing, f"阈值文件缺少 {missing}"
    assert thresholds.get("frozen_at"), "冻结的阈值必须写明冻结时间"
    for name, value in limits.items():
        assert isinstance(value, (int, float)), f"{name} 必须是数值"
        assert float(value) >= 0, f"{name} 不应为负"


def test_limits_tolerates_flat_layout() -> None:
    assert runner._limits_of({"thresholds": {"a": 1}}) == {"a": 1}
    assert runner._limits_of({"a": 1}) == {"a": 1}


# --------------------------------------------------------------------------- #
# runner 逻辑（离线，合成数据）
# --------------------------------------------------------------------------- #


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = runner.HashingEmbedder(128)
    first = embedder("Python 运行时要求 3.11")
    second = embedder("Python 运行时要求 3.11")
    assert first == second, "同一输入必须给出同一向量（跨进程可重放）"
    assert len(first) == 128, "维度必须与声明一致"
    norm = sum(value * value for value in first) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-9), "向量必须 L2 归一化"
    assert embedder("") == (), "空文本不应造出全零向量去污染余弦相似度"


def test_markdown_only_run_works_without_a_database(cases) -> None:
    """没有 DSN 时也必须能产出 baseline（否则 CI 里这份资产是死的）。"""

    outcomes = runner._markdown_only_outcomes(runner.HashingEmbedder(64), cases)
    assert len(outcomes) == len(cases)
    rows = [row for outcome in outcomes for row in outcome.rows]
    assert rows and all(row.arm == "markdown" for row in rows)
    # Markdown 路径把全部正文注入，所以有 relevant 的查询必然全召回。
    assert all(row.recall_at_k == 1.0 for row in rows if row.hit_keys)


def _row(case_id: str, arm: str, keys, relevant, must_not=(), **kwargs) -> object:
    case = {"case_id": case_id}
    query = {
        "text": "q",
        "expect": {
            "relevant": list(relevant),
            "must_not_recall": list(must_not),
            "conflict": kwargs.get("conflict", False),
            "hybrid_only": kwargs.get("hybrid_only", False),
        },
    }
    hit_keys = list(keys)
    found = [key for key in relevant if key in set(hit_keys)]
    return runner._evaluate_rows(
        case,
        query,
        arm,
        hit_keys,
        ["x" * 4 for _ in hit_keys],
        {},
        set(kwargs.get("foreign", ())),
        conflict_detected=kwargs.get("conflict_detected"),
    )


def test_check_thresholds_reports_violations_instead_of_hiding_them() -> None:
    outcomes = [
        runner.CaseOutcome(
            case_id="c1",
            description="",
            rows=[
                # 混合检索漏掉了唯一相关项 —— 必须被判 FAIL。
                _row("c1", "db_hybrid", ["noise"], ["target"]),
                _row("c1", "db_keyword", ["target"], ["target"]),
                _row("c1", "markdown", ["target", "noise"], ["target"]),
            ],
        )
    ]
    limits = {
        "hybrid_recall_at_k_min": 0.9,
        "hybrid_precision_at_k_min": 0.5,
        "keyword_recall_at_k_min": 0.5,
        "expired_false_recall_max": 0.0,
        "cross_tenant_leakage_max": 0.0,
        "conflict_flag_rate_min": 0.5,
        "hybrid_token_ratio_vs_markdown_max": 10.0,
    }
    checks = {item["name"]: item for item in runner._check_thresholds(outcomes, limits)}
    assert checks["hybrid_recall_at_k_min"]["ok"] is False
    assert checks["hybrid_recall_not_below_keyword"]["ok"] is False
    assert checks["keyword_recall_at_k_min"]["ok"] is True


def test_expired_false_recall_and_leakage_are_detected() -> None:
    row = _row(
        "c1",
        "db_hybrid",
        ["stale", "other_tenant"],
        ["fresh"],
        must_not=["stale"],
        foreign=["other_tenant"],
    )
    assert row.expired_false_recall is True
    assert row.leaked is True


def test_conflict_rate_only_counts_conflict_cases() -> None:
    outcomes = [
        runner.CaseOutcome(
            case_id="c1",
            description="",
            rows=[
                _row("c1", "db_hybrid", ["a", "b"], ["a", "b"], conflict=True,
                     conflict_detected=True),
                _row("c1", "db_hybrid", ["a"], ["a"], conflict=False,
                     conflict_detected=False),
            ],
        )
    ]
    assert runner._conflict_rate(outcomes) == 1.0


def test_hybrid_only_win_counter_counts_pairs() -> None:
    outcomes = [
        runner.CaseOutcome(
            case_id="c1",
            description="",
            rows=[
                _row("c1", "db_hybrid", ["target"], ["target"], hybrid_only=True),
                _row("c1", "db_keyword", [], ["target"], hybrid_only=True),
            ],
        )
    ]
    assert runner._hybrid_only_wins(outcomes) == (1, 1)
