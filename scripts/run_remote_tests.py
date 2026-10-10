"""VM 远端 context 测试脚本：用 /usr/bin/python3 (3.8) 跑。"""
from __future__ import annotations

import sys
import paramiko


HOST = "192.168.1.33"
PORT = 1012
USER = "lzl"
PASSWORD = "123"
REPO = "klonet_agent"


def run(client, cmd, timeout=600):
    print(f"$ {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if len(out) > 4000:
        out = out[:1500] + "\n...[truncated]...\n" + out[-1500:]
    if out:
        print(out)
    if err:
        print(f"stderr: {err[:1000]}")
    print(f"exit={code}")
    return code, out, err


def main() -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=15)
    except Exception as exc:
        print(f"ssh 连接失败: {exc}", file=sys.stderr)
        return 2

    # 0) 确认远端 HEAD 是 0768561
    code, out, _ = run(client, f"cd ~/{REPO} && git rev-parse HEAD")
    if code != 0 or "0768561" not in out:
        print(f"远端 HEAD 不匹配：{out}", file=sys.stderr)
        return 4

    # 1) 跑 context 测试。
    #    ⚠️ 必须用绝对路径解释器：服务器上有 5 个 Python，登录 shell 的
    #    ``python3`` 会解析到系统 /usr/bin/python3 (3.8)，而
    #    config.py:335 的 PEP 604 语法需要 3.10+，会 collection 崩溃。
    #    项目真正的环境是 ~/miniconda3/envs/klonet_agent/bin/python (3.11.15)。
    code, out, err = run(
        client,
        (
            "export PYTHONPATH=$HOME && "
            "cd ~/{0} && "
            "~/miniconda3/envs/klonet_agent/bin/python -m pytest "
            "{0}/tests/test_context_*.py "
            "--no-header --tb=short --basetemp=/tmp/remote_ctx "
            "-p no:cacheprovider 2>&1 | tail -40"
        ).format(REPO),
        timeout=600,
    )
    if code != 0:
        # pytest 非零不代表失败，可能是 collection error；让 caller 看输出
        print("pytest exit != 0，输出见上", file=sys.stderr)

    # 2) 顺手跑全量无 DSN，确认总基线
    code, out, err = run(
        client,
        (
            "export PYTHONPATH=$HOME && "
            "cd ~/{0} && "
            "~/miniconda3/envs/klonet_agent/bin/python -m pytest "
            "--no-header --tb=no -q "
            "--basetemp=/tmp/remote_full "
            "-p no:cacheprovider 2>&1 | tail -10"
        ).format(REPO),
        timeout=1200,
    )

    print("\n✓ 远端测试完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
