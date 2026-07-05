# Klonet Agent 专用 SSH 账户设计

## 目标

让管理员可以选择把现有 `klonet-agent` 系统账户配置为专用 SSH 登录账户。登录后自动获得项目虚拟环境、受保护的 Agent 环境变量和正确工作目录，可以直接运行：

```bash
python -m klonet_agent.agent --mode mentor --user-id lht --project-id test
python -m klonet_agent.agent --mode coding --user-id lht --project-id test
python -m klonet_agent.agent --mode ops --user-id lht --project-id test
```

Ops 权限边界保持不变：只有 `klonet-agent` 属于 `klonet-ops`，真实特权操作仍只能通过 root-owned `klonet-agent-op` 和 sudoers 白名单执行。

## 选择的方案

扩展现有 `scripts/install-klonet-agent-service.sh`，增加两个显式选项：

- `--enable-ssh-login`：把 `klonet-agent` 的 shell 设置为 `/bin/bash`，安装专用登录环境。
- `--set-password`：安装完成后调用系统 `passwd klonet-agent`，在当前终端交互设置密码；该选项必须与 `--enable-ssh-login` 同时使用。

密码不得出现在脚本源码、命令行参数、环境变量、Git、测试夹具或日志中。安装器不接受 `--password`，也不通过 stdin 管道、`chpasswd` 或 `usermod --password` 注入密码。

默认行为保持安全兼容：如果不传 `--enable-ssh-login`，新建账户继续使用 `/usr/sbin/nologin`。已经启用 `/bin/bash` 的账户在后续重复部署时不会被自动改回 `nologin`，避免意外中断 SSH 使用。

## 登录环境

安装器渲染 root-owned `/etc/profile.d/klonet-agent.sh`，仅在当前登录用户为 `klonet-agent` 时生效。该文件负责：

1. 把部署时指定 Python 所在的虚拟环境 `bin` 目录放到 `PATH` 最前面。
2. 从 `/etc/klonet-agent/klonet-agent.env` 导出环境变量。
3. 切换到 `klonet_agent` 包目录的父目录，使 `python -m klonet_agent.agent` 能正确导入。

所有部署路径必须经过 Shell 安全引用后写入 profile，不能直接拼接未转义内容。profile 为 `root:root 0644`，登录账户不能修改。

## 运行数据权限

Agent 会写入包目录内的 `memory`、`journals`、`workspaces` 和 `tracing`。启用 SSH 登录时，安装器确保这些目录存在，将组设置为 `klonet-agent`，增加组读写与目录进入权限，并为目录设置 setgid，使新文件继承 `klonet-agent` 组。安装器不改变源代码文件的所有者，也不授予 `klonet-agent` 修改 Python 源码的额外权限。

## SSH 服务边界

安装器只配置 Linux 账户，不自动修改全局 `/etc/ssh/sshd_config`，也不自动 reload SSH 服务。原因是 SSH daemon 配置因发行版、堡垒机和服务器安全策略而异，错误修改可能导致管理员失去连接。

安装文档提供检查命令：

```bash
sudo sshd -T | grep -i passwordauthentication
```

如果服务器全局禁止密码登录，管理员必须依据本机策略决定是否为 `klonet-agent` 添加 `Match User` 配置，并在 `sshd -t` 成功后 reload。安装器会明确提示：账户侧已配置不代表 SSH daemon 一定允许密码认证。

## 安装与使用

```bash
cd /home/adminis/lht/agent/klonet_agent
sudo ./scripts/install-klonet-agent-service.sh \
  --project-root "$PWD" \
  --python "$PWD/.venv/bin/python" \
  --mode ops \
  --user-id lht \
  --project-id test \
  --enable-ssh-login \
  --set-password
```

`passwd` 随后在终端提示输入两次新密码。设置完成后：

```bash
ssh klonet-agent@SERVER_ADDRESS
python -m klonet_agent.agent --mode ops --user-id lht --project-id test
```

管理员仍应选择唯一且足够强的服务器密码。任何曾出现在聊天、文档或脚本中的示例密码都不应继续使用。

## 错误处理与幂等性

- `--set-password` 缺少 `--enable-ssh-login` 时立即失败。
- 找不到 `/bin/bash`、`passwd` 或部署 Python 时立即失败。
- `passwd` 返回非零状态时安装器失败，并提示可重新运行 `sudo passwd klonet-agent`；已完成的 helper/sudoers 安装不回滚。
- 重复执行不会覆盖环境文件，也不会重复追加 profile 内容；profile 每次从受版本控制的模板完整重建。
- 已有 `klonet-agent` 如果 UID 属于普通用户范围，安装器继续拒绝接管。
- 已有账户 shell 只能是 `/usr/sbin/nologin`、`/bin/false` 或 `/bin/bash`；其他 shell 需要管理员先人工确认。

## 测试

测试使用临时安装根目录和命令替身，不修改真实账户、密码、`/etc` 或 SSH 服务：

- 默认部署仍创建 `nologin` 账户。
- `--enable-ssh-login` 使用 `/bin/bash` 并安装 profile。
- `--set-password` 单独使用会失败。
- 同时使用两个选项时只调用 `passwd klonet-agent`，命令中不含密码。
- profile 包含虚拟环境 PATH、环境文件和项目父目录，并对路径正确引用。
- 运行目录获得预期组写权限和 setgid 行为。
- 全仓库扫描确认没有新增明文部署密码。

完成后运行安装器相关测试、Ops helper 测试和全量 pytest，再提交并推送 `origin/master`。
