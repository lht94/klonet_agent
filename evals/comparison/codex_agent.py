"""对照臂：官方 Codex CLI（接入 DeepSeek 官方 API）。

为什么是 Codex CLI 而不是自写循环
--------------------------------
简历上要写的对照物是「通用 Coding Agent」。自写一个最小循环虽然可控，但读者
不会认；Codex CLI 是真实产品，工具链、系统提示词、上下文管理都是它自己的。
代价是它的系统提示词不可对齐 —— 但这恰恰是被测对象的一部分（通用 Agent 的
固有开销），端到端口径下应当计入。

为什么接 DeepSeek 而不是中转站
------------------------------
同一句提示连续三次实测 49.6s / 22.2s / 13.0s（3.8 倍抖动），任何耗时结论都是
噪声。DeepSeek 直连抖动在 1 秒级，且模型与实验臂完全相同。

为什么需要 models.json
----------------------
Codex 不内置 DeepSeek 的模型元数据，缺失时会退回降级元数据，日志里出现
``Model metadata for ... not found. Defaulting to fallback metadata; this can
degrade performance`` —— 基线被削弱会使对比失去意义。官方脚本内嵌了完整目录，
本模块直接取用，不自行编造。

隔离性
------
全部写操作都在 ``KLONET_CODEX_HOME``（默认 ``~/.codex-eval``）内，
不触碰用户既有的 ``~/.codex``。API Key 只经环境变量传递，不落盘。

用法：

    python evals/comparison/codex_agent.py --setup-only
    python evals/comparison/codex_agent.py \
        --task-id C01 --prompt "..." \
        --out-dir evals/comparison/runs/matrix \
        --sandbox danger-full-access
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[2]

OFFICIAL_SETUP_URL = "https://cdn.deepseek.com/api-docs/codex-deepseek-setup.sh"

DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com/"
DEFAULT_EFFORT = "medium"
# 与 baseline_agent.py 的 MAX_STEPS 对齐，保证两臂步数预算一致
DEFAULT_TIMEOUT = 600


def read_env_file(key: str) -> str:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def codex_home() -> Path:
    override = os.environ.get("KLONET_CODEX_HOME", "").strip()
    return Path(override) if override else Path.home() / ".codex-eval"


def extract_models_json(script_text: str) -> str:
    """从官方脚本的 heredoc 里取出模型目录。

    官方脚本形如 ``cat > "$1" <<'CODEX_MODELS_JSON'`` ... ``CODEX_MODELS_JSON``。
    按标记提取而不是按行号，脚本改版也不会取错。
    """

    lines = script_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if "CODEX_MODELS_JSON" in line and "<<" in line:
            start = index + 1
            break
    if start is None:
        raise RuntimeError("官方脚本中未找到 CODEX_MODELS_JSON heredoc")

    collected: list[str] = []
    for line in lines[start:]:
        if line.strip() == "CODEX_MODELS_JSON":
            break
        collected.append(line)
    if not collected:
        raise RuntimeError("CODEX_MODELS_JSON heredoc 内容为空")
    return "\n".join(collected) + "\n"


def setup_home(*, model: str, effort: str, quiet: bool) -> Path:
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)

    catalog = home / "models.json"
    if not catalog.exists() or catalog.stat().st_size == 0:
        with urllib.request.urlopen(OFFICIAL_SETUP_URL, timeout=60) as response:
            script_text = response.read().decode("utf-8")
        content = extract_models_json(script_text)
        parsed = json.loads(content)
        slugs = [item.get("slug") for item in parsed.get("models", [])]
        if not slugs:
            raise RuntimeError("官方模型目录不含任何模型条目")
        catalog.write_text(content, encoding="utf-8")
        if not quiet:
            print("已写入模型目录 %s（%s）" % (catalog, ", ".join(slugs)))

    config = home / "config.toml"
    # 每轮都重写：实验参数必须由本模块决定，不接受环境里遗留的旧值。
    config.write_text(
        "\n".join(
            [
                'model = "%s"' % model,
                'model_provider = "deepseek"',
                # 走 API Key 而不是 ChatGPT 登录，避免占用 Codex 订阅额度
                'preferred_auth_method = "apikey"',
                'forced_login_method = "api"',
                'model_reasoning_effort = "%s"' % effort,
                'model_catalog_json = "%s"' % catalog,
                "",
                "[model_providers.deepseek]",
                'name = "deepseek"',
                'base_url = "%s"' % DEFAULT_BASE_URL,
                # codex-cli 0.149 起 wire_api="chat" 已被移除，只认 responses；
                # DeepSeek 原生支持 Responses 协议，因此可以直连。
                'wire_api = "responses"',
                'env_key = "DEEPSEEK_API_KEY"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    if not quiet:
        print("已写入配置 %s（model=%s effort=%s）" % (config, model, effort))
    return home


def codex_version() -> str:
    try:
        completed = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=30,
        )
        return (completed.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def parse_events(raw: str) -> dict:
    """把 `codex exec --json` 的 JSONL 事件流汇总成指标。"""

    summary: dict = {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_input_tokens": 0,
        "tool_calls": 0,
        "commands": [],
        "final_text": "",
        "event_counts": {},
        "errors": [],
    }
    seen_commands: set[str] = set()
    messages: list[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = event.get("type", "")
        summary["event_counts"][kind] = summary["event_counts"].get(kind, 0) + 1

        if kind == "turn.completed":
            usage = event.get("usage") or {}
            summary["input_tokens"] += int(usage.get("input_tokens") or 0)
            summary["output_tokens"] += int(usage.get("output_tokens") or 0)
            summary["reasoning_tokens"] += int(usage.get("reasoning_output_tokens") or 0)
            summary["cached_input_tokens"] += int(usage.get("cached_input_tokens") or 0)
            continue

        if kind != "item.completed":
            continue
        item = event.get("item") or {}
        item_type = item.get("type")

        if item_type == "command_execution":
            # item.started 与 item.completed 是同一个 id，按 id 去重
            key = item.get("id") or item.get("command") or ""
            if key not in seen_commands:
                seen_commands.add(key)
                summary["tool_calls"] += 1
                summary["commands"].append(
                    {
                        "command": item.get("command"),
                        "exit_code": item.get("exit_code"),
                        "output": (item.get("aggregated_output") or "")[:4000],
                    }
                )
        elif item_type == "agent_message":
            text = item.get("text") or ""
            if text:
                messages.append(text)
        elif item_type == "error":
            summary["errors"].append(item.get("message") or "")

    summary["final_text"] = messages[-1] if messages else ""
    summary["total_tokens"] = (
        summary["input_tokens"] + summary["output_tokens"]
    )
    return summary


def run(argv: list[str], *, cwd: Path, timeout: int, env: dict) -> tuple[int | None, str, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            # codex exec 在非 TTY 下会把 stdin 当作追加输入并一直等，
            # 必须显式关闭，否则整个调用挂死。
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return completed.returncode, (completed.stdout or ""), ""
    except subprocess.TimeoutExpired as exc:
        return None, (exc.stdout or ""), "timeout"
    except FileNotFoundError:
        return None, "", "codex-not-found"


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex CLI 对照臂")
    parser.add_argument("--task-id", required=False, default="smoke")
    parser.add_argument("--prompt", required=False, default="")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "evals/comparison/runs/matrix")
    parser.add_argument("--cwd", type=Path, default=None,
                        help="Codex 的工作目录，默认仓库根目录")
    parser.add_argument("--sandbox", default="danger-full-access",
                        choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or read_env_file("DEEPSEEK_API_KEY")
    if not key:
        print("错误：未找到 DEEPSEEK_API_KEY（环境变量或仓库 .env）。", file=sys.stderr)
        return 2

    home = setup_home(model=args.model, effort=args.effort, quiet=args.quiet)
    if args.setup_only:
        return 0

    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    env["DEEPSEEK_API_KEY"] = key

    cwd = (args.cwd or ROOT).resolve()
    argv = [
        "codex", "exec",
        "--skip-git-repo-check",
        "--json",
        "--sandbox", args.sandbox,
        "--cd", str(cwd),
        args.prompt,
    ]

    if not args.quiet:
        print("harness   : codex-cli %s" % codex_version())
        print("model     : %s（reasoning=%s）" % (args.model, args.effort))
        print("sandbox   : %s" % args.sandbox)
        print("CODEX_HOME: %s" % home)
        print("-" * 60)

    started = time.monotonic()
    exit_code, stdout, error = run(argv, cwd=cwd, timeout=args.timeout, env=env)
    duration = round(time.monotonic() - started, 3)
    summary = parse_events(stdout)

    if error == "timeout":
        status = "timeout"
    elif error:
        status = error
    elif exit_code != 0:
        status = "nonzero_exit"
    elif summary["final_text"]:
        status = "ok"
    else:
        # 跑完但没有最终文本 —— 与自写基线一样，属于「未给出结论」
        status = "stopped_without_answer"

    artifact = {
        "task_id": args.task_id,
        "harness": "codex-cli",
        "codex_version": codex_version(),
        "model": args.model,
        "base_url": DEFAULT_BASE_URL,
        "reasoning_effort": args.effort,
        "config_source": "locked-catalog",
        "sandbox": args.sandbox,
        "status": status,
        "exit_code": exit_code,
        "error": error,
        # Codex CLI 的 JSON 事件不暴露单次模型往返次数，此处留空，
        # 效率对比以 token 与工具调用次数为准（不要用 0 冒充未知）。
        "llm_calls": None,
        "tool_calls": summary["tool_calls"],
        "commands": summary["commands"],
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "reasoning_tokens": summary["reasoning_tokens"],
        "cached_input_tokens": summary["cached_input_tokens"],
        "total_tokens": summary["total_tokens"],
        "duration_seconds": duration,
        "final_text": summary["final_text"],
        "errors": summary["errors"],
        "event_counts": summary["event_counts"],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "raw_events": stdout,
    }

    artifacts_dir = args.out_dir / "codex_artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / ("%s.json" % args.task_id)).write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    if not args.quiet:
        print("status=%s 耗时=%.1fs 工具调用=%s token=%s（输入 %s / 输出 %s）" % (
            status, duration, summary["tool_calls"],
            summary["total_tokens"], summary["input_tokens"], summary["output_tokens"],
        ))
        print("最终答复：%s" % (summary["final_text"][:400] or "（空）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
