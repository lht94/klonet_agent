"""远端文件上传器（对比测试期间临时使用）。

用法：python scripts/_eval_upload.py <本地路径> <远端路径>

凭据通过环境变量传入，脚本本身不含密码。
仅用于把本地产物送到测试服务器，不执行任何远端命令。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

HOST = os.environ["KL_HOST"]
PORT = int(os.environ.get("KL_PORT", "22"))
USER = os.environ["KL_USER"]
PASSWORD = os.environ["KL_PASS"]

if len(sys.argv) != 3:
    print("用法: python scripts/_eval_upload.py <本地路径> <远端路径>")
    sys.exit(2)

local_path = Path(sys.argv[1])
remote_path = sys.argv[2]

if not local_path.is_file():
    print(f"本地文件不存在: {local_path}")
    sys.exit(2)

transport = paramiko.Transport((HOST, PORT))
try:
    transport.connect(username=USER, password=PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    sftp.put(str(local_path), remote_path)
    size = sftp.stat(remote_path).st_size
    sftp.close()
    print(f"已上传 {local_path.name} -> {remote_path} ({size} 字节)")
finally:
    transport.close()
