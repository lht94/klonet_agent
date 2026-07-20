# Klonet Agent 手动运行与 Python 路径修复设计

## 目标

修复现有 `scripts/install-klonet-agent-service.sh` 的两个部署问题：

1. 安装器不再启用或启动 `klonet-agent.service`，Agent 仅在登录
   `klonet-agent` 用户后手动运行。
2. 安装器保留用户通过 `--python` 指定的虚拟环境解释器路径，不再把
   `.venv/bin/python` 解析为基础系统 Python。

本次不新建安装脚本，也不自动猜测不同服务器上的 Python 路径。

## 安装器接口

`--python PATH` 继续作为必填参数。用户必须为当前服务器显式提供可执行的
Python，例如：

```bash
sudo ./scripts/install-klonet-agent-service.sh \
  --project-root "$PWD" \
  --python "$PWD/.venv/bin/python" \
  --mode ops \
  --user-id lht \
  --project-id test \
  --enable-ssh-login
```

安装器可以把相对路径转成绝对路径，但不能解析路径最后一段的符号链接。
因此 `.venv/bin/python` 必须原样保留为虚拟环境入口，而不能变成
`/usr/local/python3/bin/python3.8`。

安装器在写入登录配置前使用指定解释器执行依赖预检：

```bash
"$python_path" -c 'import rank_bm25'
```

预检失败时安装立即停止，错误信息应包含指定解释器，并提示用户使用同一解释器
安装 `requirements.txt`。安装器不自行选择 Python，也不静默向系统 Python
安装依赖。

## 运行模式

保留现有安装脚本和账户、SSH 登录环境、Ops helper、sudoers 以及运行目录权限
配置。删除 `--start` 参数，并移除安装器中的 `systemctl enable` 与
`systemctl restart` 行为。安装器可以继续生成兼容的 unit 文件，但不得使其
进入 enabled 或 running 状态。

安装完成后的正常使用方式是：

```bash
ssh klonet-agent@SERVER_ADDRESS
python -m klonet_agent.agent --mode ops --user-id lht --project-id test
```

命令退出后 Agent 即停止，不由 systemd 自动重启。已有部署需要由管理员单独
执行一次 `systemctl disable --now klonet-agent.service`；安装器不暗中停止当前
正在运行的旧服务。

## 错误处理

- 未传 `--python`：安装失败并说明该参数必填。
- 指定路径不存在或不可执行：安装失败并打印该路径。
- 指定解释器无法导入 `rank_bm25`：安装失败，并给出使用该解释器安装项目依赖
  的命令提示。
- 传入已经删除的 `--start`：按未知参数处理，避免用户误以为仍支持常驻运行。

## 测试

更新 `tests/test_klonet_agent_service_installer.py`，覆盖：

- 虚拟环境 `python` 是符号链接时，生成的 profile 仍引用 `.venv/bin`。
- 安装器不调用 `systemctl enable` 或 `systemctl restart`。
- `--start` 不再被接受。
- `--python` 仍然必填。
- 指定解释器缺少 `rank_bm25` 时，安装器在写入部署配置前失败并给出修复提示。
- 现有账户、SSH profile、环境文件保留和 sudoers 安全测试继续通过。

## 范围外事项

- 不修改 Agent 的业务逻辑和 BM25 检索实现。
- 不自动创建虚拟环境或联网安装依赖。
- 不自动选择服务器上的 Python。
- 不删除 Linux 用户、已有 unit 或运行数据。
