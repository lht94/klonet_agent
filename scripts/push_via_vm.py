"""VM 推送脚本：fetch bundle + merge + push 到 GitHub。

按 MEMORY：本机 git push 走 HTTPS 弹 GUI 凭据 → bundle → SFTP → VM push。
"""
from __future__ import annotations

import os
import sys
import paramiko


HOST = "192.168.1.33"
PORT = 1012
USER = "lzl"
PASSWORD = "123"
REPO = "klonet_agent"
# 远端 bundle 路径。可用 KLONET_VM_BUNDLE 覆盖（每次推送换名避免撞旧文件）。
BUNDLE = os.environ.get("KLONET_VM_BUNDLE", "/home/klonet-agent/klonet-push.bundle")


def run(client, cmd, timeout=30):
    print(f"$ {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out)
    if err:
        print(f"stderr: {err}")
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

    # 1) 确认 bundle 在
    code, _, _ = run(client, f"ls -la {BUNDLE}")
    if code != 0:
        print(f"bundle 不在 VM 上：{BUNDLE}", file=sys.stderr)
        return 3

    # 2) cd 进仓库、查 HEAD、验证 bundle
    code, _, _ = run(client, f"cd ~/{REPO} && pwd && git rev-parse HEAD")
    if code != 0:
        print(f"找不到仓库 ~/{REPO}", file=sys.stderr)
        return 4

    code, out, _ = run(
        client,
        f"cd ~/{REPO} && git bundle verify {BUNDLE}",
    )
    if code != 0:
        print("bundle verify 失败", file=sys.stderr)
        return 5
    # bundle verify 输出 commits 行包含 commit sha
    if "0768561" not in out:
        print(f"bundle 不含目标提交 0768561，verify 输出:\n{out}", file=sys.stderr)
        return 6

    # 3) fetch from bundle：bundle 只暴露 HEAD 一个 ref，所以 refspec 写成
    #    HEAD:refs/remotes/bundle/master（不能写 refs/heads/* 因为 bundle 里
    #    根本没有 master 这个 ref）。
    code, _, _ = run(
        client,
        f"cd ~/{REPO} && git fetch {BUNDLE} HEAD:refs/remotes/bundle/master",
    )
    if code != 0:
        print("git fetch bundle 失败", file=sys.stderr)
        return 7

    # 4) merge --ff-only
    code, _, _ = run(
        client,
        f"cd ~/{REPO} && git merge --ff-only refs/remotes/bundle/master",
    )
    if code != 0:
        print("git merge --ff-only 失败（可能不是快进）", file=sys.stderr)
        return 8

    # 5) push
    code, _, err = run(
        client,
        "cd ~/{0} && git push origin master".format(REPO),
        timeout=120,
    )
    if code != 0:
        print(f"git push 失败:\n{err}", file=sys.stderr)
        return 9

    # 6) 清理
    run(client, f"rm -f {BUNDLE}")
    run(client, "cd ~/{0} && git remote update --prune".format(REPO))

    print("\n✓ 推送完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
