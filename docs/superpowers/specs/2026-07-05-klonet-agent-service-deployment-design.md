# Klonet Agent 专用账户与 systemd 部署设计

## 目标

提供一个可在多台 Ubuntu 服务器重复运行的部署脚本，统一完成 `klonet-ops` 权限组、`klonet-agent` 系统账户、root-owned Ops helper、sudoers 白名单和 systemd 服务的安装。Agent 开机后以专用账户运行，不使用日常登录账户，也不把 sudo 密码或 API 密钥写入仓库、命令参数或对话记录。

## 范围

本次新增：

- 幂等部署脚本 `scripts/install-klonet-agent-service.sh`。
- 可审查的 systemd 服务模板。
- 部署脚本与安装契约的自动化测试。
- 专用账户启动、环境配置、状态检查、日志查看和更新部署说明。

本次不做：

- 不复制仓库中的 `.env`。
- 不把 API key、token 或密码提交到 Git。
- 不默认设置 `KLONET_AGENT_OPS_REAL_EXECUTION=1`。
- 不执行 helper 的任何 `--execute` 操作。
- 不自动安装 Python、虚拟环境或项目依赖；部署前必须已有可运行的 Python 环境。
- 不实现删除账户或清理运行数据的一键卸载。

## 方案

采用“幂等 Shell 安装器 + 静态 systemd 模板”。模板便于审查，安装器负责参数替换、权限设置、校验和服务生命周期操作。与动态拼接整个 unit 相比，这能减少 Shell 转义风险；与 Ansible role 相比，它不要求目标服务器预装额外部署工具。

## 安装器接口

安装器必须以 root 身份运行，支持以下显式参数：

- `--project-root PATH`：`klonet_agent` 包目录，必须包含 `agent.py` 和 `scripts/klonet-agent-op`。
- `--python PATH`：运行 Agent 的 Python 解释器，必须是可执行文件。
- `--mode MODE`：默认 `ops`。
- `--user-id ID`：默认 `default`。
- `--project-id ID`：默认 `default`。
- `--service-name NAME`：默认 `klonet-agent`。
- `--env-file PATH`：默认 `/etc/klonet-agent/klonet-agent.env`。
- `--no-start`：只安装和 enable，不立即启动或重启服务。

账户名固定为 `klonet-agent`，权限组固定为 `klonet-ops`，helper 固定安装到 `/usr/local/bin/klonet-agent-op`，sudoers 固定安装到 `/etc/sudoers.d/klonet-agent-op`。固定安全边界避免参数组合导致 sudoers 主体与 systemd 用户不一致。

重复运行时，脚本应识别已存在的组和账户、重新安装受版本控制的 helper/sudoers/unit、重新校验配置，并安全地 reload/restart 服务。若同名账户已存在但不是系统账户，或者其登录 shell 允许交互登录，脚本必须拒绝继续并给出明确错误。

## systemd 服务

服务使用：

- `User=klonet-agent`
- `Group=klonet-agent`
- `WorkingDirectory` 指向包目录的父目录，使 `python -m klonet_agent.agent` 可导入。
- `ExecStart` 使用部署参数指定的 Python，传入 mode、user-id 和 project-id。
- `EnvironmentFile=-/etc/klonet-agent/klonet-agent.env`；前导 `-` 允许首次安装时环境文件尚未配置。
- `Restart=on-failure`，避免正常退出后无限重启交互式 CLI。

环境文件由 root 创建为空白示例，权限为 `root:klonet-agent 0640`。管理员在其中配置 `OPENAI_API_KEY`、base URL 等运行环境。默认不包含 `KLONET_AGENT_OPS_REAL_EXECUTION=1`；管理员完成安全验证后才可手动加入。

由于当前程序是交互式 CLI，systemd 服务需要绑定标准输入来源。服务使用 `StandardInput=null` 时 CLI 会立刻读到 EOF 并正常退出，因此不能提供长期对话服务。部署脚本会安装和 enable unit，但默认仅在程序具有非交互服务入口时才适合持续运行。当前阶段使用 systemd 启动 ops CLI 时，应通过显式输入通道或后续 Web/API 服务入口解决。为避免制造“已常驻”的假象，安装器在当前 CLI 架构下默认使用 `--no-start` 语义；只有显式传入 `--start` 才启动。

## 安装顺序与失败处理

1. 校验 root 权限、参数、源文件、Python、`useradd`、`install`、`visudo` 和 `systemctl`。
2. 创建 `klonet-ops` 组和 `klonet-agent` 系统账户，设置 home `/var/lib/klonet-agent`、shell `/usr/sbin/nologin`，加入 `klonet-ops`。
3. 安装 helper 为 `root:root 0755`。
4. 将 sudoers 模板安装到临时文件，运行 `visudo -cf`；成功后原子替换为 `root:root 0440`。
5. 创建环境目录及环境文件，保留已有环境文件内容。
6. 渲染并安装 systemd unit，执行 `systemctl daemon-reload` 和 `systemctl enable`。
7. 若显式要求启动，则 restart 服务；否则输出后续配置和启动命令。
8. 验证账户组关系、文件所有权、sudoers 语法、`sudo -l -U klonet-agent` 和 helper dry-run。

任一步失败均返回非零状态。已经创建的系统账户和组不自动删除，以免误删服务器上既有资源；失败信息必须指出修复后可安全重跑脚本。

## 测试

自动化测试不修改真实 `/etc`、用户数据库或 systemd：

- 静态契约测试检查脚本包含 root 检查、幂等账户/组创建、安全权限、`visudo`、systemd 和 dry-run 验证。
- 使用临时目录和命令替身执行安装器，验证首次安装、重复安装、保留环境文件、`--no-start`/`--start` 行为和失败退出。
- systemd 模板测试检查专用用户、工作目录、环境文件和启动参数。
- 现有 Ops helper 与 recipe 测试继续通过。

服务器验收命令包括：

```bash
id klonet-agent
sudo -l -U klonet-agent
sudo visudo -cf /etc/sudoers.d/klonet-agent-op
sudo systemctl status klonet-agent
sudo journalctl -u klonet-agent -n 100 --no-pager
sudo -u klonet-agent /usr/local/bin/klonet-agent-op reload-nginx --dry-run
```

## Git 与秘密检查

推送前检查全部已跟踪和未跟踪候选文件，拒绝提交 `.env`、私钥、API key、token 或密码。用户要求的“全部修改”解释为全部安全、属于项目且未被 `.gitignore` 排除的源码、测试、文档和共享 Ops 记忆。完成验证后提交到当前 `master` 并推送 `origin/master`。
