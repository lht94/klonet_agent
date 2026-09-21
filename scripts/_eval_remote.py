"""远端命令执行器（对比测试期间临时使用）。

用法：从 stdin 读取要执行的 shell 命令，在远端执行并回显输出。

    echo 'ls -l' | python scripts/_eval_remote.py

凭据通过环境变量传入，脚本本身不含密码。只用于侦察与验证，
不承担任何变更操作。
"""

from __future__ import annotations

import os
import sys

import paramiko

HOST = os.environ["KL_HOST"]
PORT = int(os.environ.get("KL_PORT", "22"))
USER = os.environ["KL_USER"]
PASSWORD = os.environ["KL_PASS"]

command = sys.stdin.read()
if not command.strip():
    print("stdin 为空，未提供命令。")
    sys.exit(2)

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(
        HOST, port=PORT, username=USER, password=PASSWORD,
        timeout=25, banner_timeout=25, auth_timeout=25,
        look_for_keys=False, allow_agent=False,
    )
    _stdin, stdout, stderr = client.exec_command(command, timeout=900)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out.rstrip())
    if err:
        print("[stderr]")
        print(err.rstrip())
    print(f"[exit={code}]")
finally:
    client.close()
