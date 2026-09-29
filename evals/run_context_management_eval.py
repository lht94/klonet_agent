"""上下文管理专项评测（01 计划阶段 6 / 02 计划阶段 0）。

作用是把“上下文主链路在大改之前是什么样”变成可重放、可对比的数字：

1. 对每个 case 构造真实 history，用真实 ContextCompiler 编译；
2. 记录旧路径（完整 history 直接发送）与新路径（编译 + checkpoint 覆盖）的
   token 估算，得到 token 基线；
3. 记录硬预算是否被拒绝、是否生成 checkpoint、被覆盖的原始历史是否还出现在
   请求里、当前用户输入是否 100% 保留、重启后恢复多少事件；
4. 输出 `evals/context_management_summary.md` 作为冻结基线。

为了在离线环境可重复，压缩器替换成返回固定 checkpoint JSON 的假客户端；
被压缩的事件里带 `COVERED-*-MARKER`，当前输入带 `CURRENT-*-MARKER`，
通过标记判断“覆盖是否扣减、当前输入是否保留”。

用法：
    PYTHONPATH=<仓库父目录> python -m evals.run_context_management_eval
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

from klonet_agent.config import PROJECT_ROOT

_CASE_FILE = PROJECT_ROOT / "evals" / "context_management_cases.jsonl"
_OUTPUT_FILE = PROJECT_ROOT / "evals" / "context_management_summary.md"

_COVERED_MARKER = "COVERED-"
_CURRENT_MARKER = "CURRENT-"

# 填充用的是 ASCII 字符，估算口径为 4 字符 1 token（见 context/tokens.py）。
# 每轮补 needed * 4 * 1.2 个字符，约等于补 1.2 倍缺口，两三轮内收敛。
_ASCII_CHARS_PER_TOKEN = 4.0
_PADDING_FACTOR = 1.2

# 固定预算口径，保证基线可重放。取一个比所有模式系统提示词都宽裕、又不需要
# 生成十几万字符填充的窗口；真实部署可用环境变量覆盖。
_BUDGET_ENV = {
    "KLONET_AGENT_CONTEXT_WINDOW": "32768",
    "KLONET_AGENT_MAX_OUTPUT_TOKENS": "2048",
    "KLONET_AGENT_SAFETY_MARGIN_TOKENS": "1024",
    "KLONET_AGENT_COMPACTION_MIN_TOKENS": "512",
}

_VALID_CHECKPOINT = json.dumps(
    {
        "goal": "继续处理当前任务",
        "status": "in_progress",
        "constraints": ["不得中断现有任务"],
        "decisions": [],
        "completed_steps": ["已完成前期探查"],
        "pending_steps": ["完成剩余步骤"],
        "failed_attempts": [],
        "evidence_refs": ["eval_fixture"],
        "touched_files": [],
        "verification": [],
        "unresolved_questions": [],
        "next_action": "继续推进下一步",
    },
    ensure_ascii=False,
)


class _FakeResponse:
    def __init__(self, content: str):
        self.usage = type("Usage", (), {"total_tokens": 64})()
        message = type("Message", (), {"content": content, "tool_calls": None})()
        self.choices = [type("Choice", (), {"message": message})()]


class _FakeLLM:
    """返回固定 checkpoint JSON；记录每次真正收到的消息。"""

    model = "glm-5.2"

    def __init__(self, content: str = _VALID_CHECKPOINT):
        self.content = content
        self.received: list[list[dict]] = []

    def complete(self, messages, tools=None, stream=False):
        self.received.append(list(messages))
        return _FakeResponse(self.content)


def _load_cases() -> list[dict]:
    cases = []
    for line in _CASE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def _sanitized(messages: list[dict]) -> list[dict]:
    from klonet_agent.memory.store import sanitize_openai_tool_history

    return sanitize_openai_tool_history([dict(message) for message in messages])


def _pad_history(
    history: list[dict],
    compiler,
    model: str,
    tools: list[dict],
    filler_chars: int,
    *,
    rounds: int = 6,
) -> tuple[list[dict], str | None]:
    """把最老的 user 事件撑到“超过软阈值但不到硬阈值”的区间内。

    压缩类 case 必须真的跨过软阈值，否则断言没有意义；直接写死长度会随系统
    提示词变化而失效，所以按实测预算反推需要多少填充字符。
    """

    if filler_chars <= 0:
        return history, None
    padded = [dict(message) for message in history if message.get("role") != "system"]
    system = [message for message in history if message.get("role") == "system"]
    pad_index = next(
        (index for index, message in enumerate(padded) if message.get("role") == "user"),
        None,
    )
    if pad_index is None:
        return history, "no_user_message_to_pad"
    for _ in range(rounds):
        current = system + padded
        try:
            compiled = compiler.compile_history(
                current, model=model, tool_definitions=tools
            )
        except Exception as exc:  # pragma: no cover - 诊断用
            return current, f"compile_failed:{type(exc).__name__}:{exc}"
        if compiled.compression_required:
            return current, None
        needed = compiled.soft_input_limit - compiled.estimated_input_tokens
        if needed <= 0:
            return current, "no_headroom_below_soft_limit"
        padded[pad_index]["content"] = (
            str(padded[pad_index].get("content") or "")
            + "x" * max(512, int(needed * _ASCII_CHARS_PER_TOKEN * _PADDING_FACTOR))
        )
    return system + padded, "padding_did_not_cross_soft_limit"


def _trace_compile_events(trace_file: Path) -> list[dict]:
    if not trace_file.exists():
        return []
    events = []
    for line in trace_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "context_compile":
            events.append(row)
    return events


def _run_case(case: dict) -> dict:
    from klonet_agent.agents import get_profile
    from klonet_agent.context.budget import build_context_budget
    from klonet_agent.context.compiler import ContextOverflowError
    from klonet_agent.context.tokens import estimate_messages_tokens
    from klonet_agent.memory.store import MemoryStore
    from klonet_agent.orchestrator import AgentOrchestrator
    from klonet_agent.session import AgentSession
    from klonet_agent.tracing.logger import TraceLogger

    mode = case.get("mode", "mentor")
    result: dict = {
        "case_id": case["case_id"],
        "mode": mode,
        "description": case.get("description", ""),
        "expect": case.get("expect", {}),
        "notes": [],
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        store = MemoryStore(root / "memory", root / "USER.md")
        llm = _FakeLLM()
        trace_file = root / "trace.jsonl"
        session = AgentSession(
            user_id="eval-user",
            project_id="eval-project",
            mode=mode,
            workspace_path=root / "workspace",
            journal_path=root / "journal.md",
        )
        orchestrator = AgentOrchestrator(
            profile=get_profile(mode),
            session=session,
            llm=llm,
            trace_logger=TraceLogger(trace_file),
            memory_store=store,
        )

        payload = [dict(message) for message in case.get("messages", [])]
        result["has_covered_marker"] = any(
            _COVERED_MARKER in str(message.get("content") or "") for message in payload
        )
        for message in payload:
            if message.get("role") != "system":
                store.append_history(message)

        # 与真实运行一致：会话历史来自 history.jsonl，带 event_id 恢复。
        history = orchestrator.init_history()
        huge = int(case.get("huge_system_chars") or 0)
        if huge:
            history = history + [
                {"role": "system", "content": "超长系统规则 " + "s" * huge}
            ]

        tools = orchestrator._visible_tools()
        history, pad_note = _pad_history(
            history,
            orchestrator.context_compiler,
            llm.model,
            tools,
            int(case.get("filler_chars") or 0),
        )
        if pad_note:
            result["notes"].append(pad_note)

        # 预算由 model + tool schema 唯一决定，与走哪条代码路径无关；用它作为
        # 基线显示口径，保证“必需区超硬预算”这种没走到正常编译的 case 也有数字。
        budget = build_context_budget(llm.model, tools)
        result["hard_input_limit"] = budget.hard_input_limit
        result["soft_input_limit"] = budget.soft_input_limit
        result["profile_source"] = budget.profile_source
        result["legacy_input_tokens"] = estimate_messages_tokens(_sanitized(history))

        # “压缩前”必须独立取一次：orchestrator 只在压缩完成后记录一次
        # context_compile，其 compression_required 已经是压缩后的结果，直接读
        # trace 会把所有 case 都误判成“无需压缩”。这里在发送前用同一个编译器
        # 对同一份（已清洗）历史再编一次，得到真正的压缩前判定。
        pre_required: bool | None = None
        try:
            pre_compiled = orchestrator.context_compiler.compile_history(
                _sanitized(history), model=llm.model, tool_definitions=tools
            )
            pre_required = pre_compiled.compression_required
            result["areas"] = pre_compiled.areas
        except ContextOverflowError as exc:
            pre_required = True
            result["areas"] = exc.areas
        result["compression_required_pre"] = pre_required

        overflow = False
        provider_called = False
        try:
            orchestrator.chat_with_llm(history)
            provider_called = bool(llm.received)
        except ContextOverflowError as exc:
            overflow = True
            result["overflow_areas"] = exc.areas
        except Exception as exc:  # pragma: no cover - 诊断用
            result["notes"].append(
                f"unexpected_error:{type(exc).__name__}:{exc}"
            )
            result["traceback"] = traceback.format_exc(limit=3)

        compile_events = _trace_compile_events(trace_file)
        last_compile = compile_events[-1] if compile_events else {}
        compiled_tokens = int(last_compile.get("estimated_input_tokens") or 0)

        sent = llm.received[-1] if llm.received else []
        sent_text = "\n".join(str(message.get("content") or "") for message in sent)
        current_input = str(payload[-1].get("content") or "")
        current_marker = (
            _CURRENT_MARKER
            if _CURRENT_MARKER in current_input
            else current_input.strip()
        )

        checkpoint = orchestrator.checkpoint_store.load_latest()
        result.update(
            {
                "compiled_input_tokens": compiled_tokens,
                # 末次编译的“请求最终构成”比压缩前分布更贴近真实发送内容；
                # 走溢出分支时没有正常编译记录，退回必需区分区数字。
                "areas": last_compile.get("areas") or result.get("areas") or {},
                "compression_required_post": bool(
                    last_compile.get("compression_required")
                ),
                "hard_overflow": overflow,
                "provider_called": provider_called,
                "checkpoint_created": checkpoint is not None,
                "checkpoint_source_range": (
                    f"{checkpoint.source_event_start}..{checkpoint.source_event_end}"
                    if checkpoint is not None
                    else None
                ),
                "covered_history_dropped": (
                    None if overflow else _COVERED_MARKER not in sent_text
                ),
                "current_input_kept": (
                    None if overflow else bool(current_marker) and current_marker in sent_text
                ),
                "sent_message_count": len(sent),
                "event_id_leaked": any(
                    isinstance(message, dict) and "event_id" in message
                    for message in sent
                ),
                "restart_recovery_count": None,
            }
        )

        append_pairs = int(case.get("append_after_compaction") or 0)
        if append_pairs:
            for index in range(append_pairs):
                store.append_history({"role": "user", "content": f"重启后消息{index}"})
                store.append_history(
                    {"role": "assistant", "content": f"重启后回答{index}"}
                )
            restarted = AgentOrchestrator(
                profile=get_profile(mode),
                session=session,
                llm=_FakeLLM(),
                trace_logger=TraceLogger(trace_file),
                memory_store=store,
            )
            result["restart_recovery_count"] = len(restarted._load_recovered_history())

    legacy = result["legacy_input_tokens"]
    compiled_tokens = result["compiled_input_tokens"]
    # 请求根本没发出去（硬预算拒绝）时没有“新路径 token”可言，节省率记为空，
    # 不能用 0 当成“省了 100%”。
    result["token_saving_ratio"] = (
        round(1 - compiled_tokens / legacy, 4)
        if legacy and compiled_tokens
        else None
    )
    result["passed"] = _check(result)
    return result


def _check(result: dict) -> bool:
    """按 case 声明的期望核对；不适用于当前场景的检查项自动跳过。"""

    expect = result.get("expect", {})
    if expect.get("hard_overflow"):
        return (
            result["hard_overflow"] is True
            and result["provider_called"] is False
            and result["checkpoint_created"] is False
        )
    checks = [
        result["event_id_leaked"] is False,
        result["provider_called"] is True,
    ]
    for key in ("checkpoint_created", "compression_required_pre"):
        if expect.get(key) is not None:
            checks.append(result.get(key) == expect[key])
    if expect.get("covered_history_dropped") is not None:
        checks.append(
            result["covered_history_dropped"] == expect["covered_history_dropped"]
        )
    if expect.get("must_keep_current_input"):
        checks.append(result["current_input_kept"] is True)
    if expect.get("restart_recovery_count") is not None:
        checks.append(
            result["restart_recovery_count"] == expect["restart_recovery_count"]
        )
    if expect.get("restart_recovery_at_least") is not None:
        # 只要求“不低于”是刻意的：该 case 验证的是恢复不再被固定条数上限截断，
        # 精确值取决于最新用户输入是否落在 checkpoint 覆盖区间内（按设计永不
        # 被覆盖），用下限断言更稳，精确值仍写进 summary 明细作为冻结基线。
        count = result.get("restart_recovery_count")
        checks.append(
            count is not None and count >= expect["restart_recovery_at_least"]
        )
    return all(checks)


def _render(results: list[dict]) -> str:
    lines = [
        "# Klonet 上下文管理专项评测",
        "",
        f"- cases: {len(results)}",
        f"- passed: {sum(1 for item in results if item['passed'])}/{len(results)}",
        f"- budget: window={_BUDGET_ENV['KLONET_AGENT_CONTEXT_WINDOW']}, "
        f"max_output={_BUDGET_ENV['KLONET_AGENT_MAX_OUTPUT_TOKENS']}, "
        f"safety={_BUDGET_ENV['KLONET_AGENT_SAFETY_MARGIN_TOKENS']}",
        "",
        "## Token 与行为基线",
        "",
        "| case | mode | 旧路径 token | 新路径 token | 节省 | 压缩触发(前) | checkpoint | 覆盖已扣减 | 当前输入保留 | 硬拒绝 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        saving = item.get("token_saving_ratio")
        lines.append(
            "| {case_id} | {mode} | {legacy} | {compiled} | {saving} | {required} | {ckpt} | {dropped} | {kept} | {overflow} |".format(
                case_id=item["case_id"],
                mode=item["mode"],
                legacy=item["legacy_input_tokens"],
                compiled=item["compiled_input_tokens"],
                saving="-" if saving is None else f"{saving:.1%}",
                required="是" if item["compression_required_pre"] else "否",
                ckpt="是" if item["checkpoint_created"] else "否",
                dropped={True: "是", False: "否", None: "-"}[
                    item["covered_history_dropped"]
                ],
                kept={True: "是", False: "否", None: "-"}[item["current_input_kept"]],
                overflow="是" if item["hard_overflow"] else "否",
            )
        )
    lines.extend(["", "## 逐条明细", ""])
    for item in results:
        lines.extend(
            [
                f"### {item['case_id']}",
                f"- 说明：{item['description']}",
                f"- 结果：{'PASS' if item['passed'] else 'FAIL'}",
                f"- hard/soft limit：{item['hard_input_limit']} / {item['soft_input_limit']}",
                f"- 分区 token：{json.dumps(item['areas'], ensure_ascii=False)}",
                f"- checkpoint 区间：{item['checkpoint_source_range']}",
                f"- 是否调用供应商：{'是' if item['provider_called'] else '否'}",
                f"- 进入请求的消息数：{item['sent_message_count']}",
                f"- event_id 是否泄漏进请求：{'是' if item['event_id_leaked'] else '否'}",
                f"- 重启恢复事件数：{item['restart_recovery_count']}",
            ]
        )
        if item.get("notes"):
            lines.append(f"- 备注：{'; '.join(item['notes'])}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    for key, value in _BUDGET_ENV.items():
        os.environ.setdefault(key, value)

    cases = _load_cases()
    results = [_run_case(case) for case in cases]
    _OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FILE.write_text(_render(results), encoding="utf-8")

    failed = [item["case_id"] for item in results if not item["passed"]]
    print(f"context management eval: {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed cases: " + ", ".join(failed))
    print(f"summary: {_OUTPUT_FILE}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
