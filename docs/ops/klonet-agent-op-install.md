# klonet-agent-op 安装契约

`klonet-agent-op` 是 Ops Agent 修改服务器环境时唯一应该被 sudoers 放行的入口。部署脚本会默认开启受控真实执行；但每次环境修改仍必须先经过 OperationPlan、计划确认、单步确认，并且只能通过 helper 与 sudoers 白名单进入真实执行链路。

## 一键部署专用账户与服务

在仓库的 `klonet_agent` 目录执行以下命令。`--project-root` 指向包含
`agent.py` 的包目录，`--python` 指向已经装好项目依赖的虚拟环境解释器：

```bash
cd /home/adminis/lht/agent/klonet_agent
sudo ./scripts/install-klonet-agent-service.sh \
  --project-root "$PWD" \
  --python "$PWD/.venv/bin/python" \
  --mode ops \
  --user-id lht \
  --project-id test1
```

脚本可以在多台 Ubuntu 服务器重复执行，并会：

- 创建 `klonet-ops` 系统组；
- 创建不可交互登录的 `klonet-agent` 系统账户，新账户默认 home 为
  `/home/klonet-agent`；
- 只把 `klonet-agent` 加入 `klonet-ops`，不影响日常登录账户；
- 创建 `/home/klonet-agent/.cache/tmp`，并在首次生成环境文件时写入
  `TMPDIR=/home/klonet-agent/.cache/tmp`，避免 jieba 等库复用 `/tmp`
  下其他用户创建的缓存文件；
- 安装 root-owned helper 与 sudoers 白名单并通过 `visudo` 校验；
- 安装并 enable `klonet-agent.service`；
- 在 root 管理的环境文件中写入 `KLONET_AGENT_OPS_REAL_EXECUTION=1`；
- 执行 helper 的 `reload-nginx --dry-run` 验证，不执行任何 `--execute` 操作。

## 配置为专用 SSH 登录账户

如果希望先登录专用账户，再像本地一样用 `python -m` 启动所有模式，可以在
部署时增加 `--enable-ssh-login` 和 `--set-password`：

```bash
sudo ./scripts/install-klonet-agent-service.sh \
  --project-root "$PWD" \
  --python "$PWD/.venv/bin/python" \
  --mode ops \
  --user-id lht \
  --project-id test \
  --enable-ssh-login \
  --set-password
```

脚本会调用系统 `passwd klonet-agent`，由终端提示输入两次密码。密码不会作为
脚本参数传入，也不会写进源码、环境文件、日志或 Git。不要使用曾经出现在聊天
或文档中的示例密码。

安装器只配置 Linux 账户，不修改全局 SSH daemon 策略。先检查服务器是否允许
密码认证：

```bash
sudo sshd -T | grep -i passwordauthentication
```

如果输出为 `passwordauthentication no`，应由服务器管理员依据本机安全策略配置
`sshd_config`；修改后必须先运行 `sudo sshd -t`，确认成功后才能 reload SSH。

账户配置完成后，从客户端登录：

```bash
ssh klonet-agent@SERVER_ADDRESS
```

登录环境会自动加载 `/etc/klonet-agent/klonet-agent.env`、项目虚拟环境并进入
包目录的父目录。mentor、coding 和 ops 都使用相同入口：

```bash
python -m klonet_agent.agent --mode mentor --user-id lht --project-id test
python -m klonet_agent.agent --mode coding --user-id lht --project-id test
python -m klonet_agent.agent --mode ops --user-id lht --project-id test
```

只有 ops 模式会使用 `klonet-ops` 能力，而且仍受 helper、sudoers 白名单、
OperationPlan 和单步确认约束。

重复执行会更新 helper、sudoers 和 systemd unit，但保留已有的
`/etc/klonet-agent/klonet-agent.env`。脚本默认不启动服务；只有显式加入
`--start` 才会在安装后执行 `systemctl restart`：

```bash
sudo ./scripts/install-klonet-agent-service.sh \
  --project-root "$PWD" \
  --python "$PWD/.venv/bin/python" \
  --mode ops \
  --user-id lht \
  --project-id test1 \
  --start
```

### 配置运行环境

不要复制仓库 `.env`，也不要把密钥提交到 Git。使用 root 管理的环境文件：

```bash
sudoedit /etc/klonet-agent/klonet-agent.env
```

示例内容：

```dotenv
OPENAI_API_KEY=替换为服务器密钥
OPENAI_BASE_URL=https://api.deepseek.com
TMPDIR=/home/klonet-agent/.cache/tmp
KLONET_AGENT_OPS_REAL_EXECUTION=1
```

该文件权限为 `root:klonet-agent 0640`。密钥不会出现在模型对话或 Git
历史中。

### 以专用账户运行交互式 Agent

当前入口是交互式 CLI，而不是 Web/API 守护进程。要在终端对话，显式用
`sudo -u klonet-agent` 启动，并从受保护的环境文件加载配置：

```bash
sudo -u klonet-agent -H /bin/bash -c '
  set -a
  source /etc/klonet-agent/klonet-agent.env
  set +a
  cd /home/adminis/lht/agent
  exec /home/adminis/lht/agent/klonet_agent/.venv/bin/python \
    -m klonet_agent.agent --mode ops --user-id lht --project-id test1
'
```

这样 Python 进程的 Linux 身份就是 `klonet-agent`，它只能通过
`sudo -n /usr/local/bin/klonet-agent-op ...` 使用 sudoers 白名单中的受控操作。

### systemd 状态与日志

配置环境文件后可以运行：

```bash
sudo systemctl start klonet-agent
sudo systemctl status klonet-agent --no-pager
sudo journalctl -u klonet-agent -n 100 --no-pager
sudo systemctl restart klonet-agent
```

但当前 CLI 在 systemd 的 `StandardInput=null` 下会读到 EOF 并正常退出，
因此它暂时不能作为常驻聊天服务。unit 的作用是先固化专用账户、环境和启动
契约；需要常驻服务时，应先实现 Web/API 或消息队列入口。交互使用请采用上面的
`sudo -u klonet-agent` 命令。

### 部署后验证

```bash
id klonet-agent
sudo -l -U klonet-agent
sudo visudo -cf /etc/sudoers.d/klonet-agent-op
sudo -u klonet-agent /usr/local/bin/klonet-agent-op reload-nginx --dry-run
```

## 安装 helper

在服务器上执行：

```bash
sudo install -m 0755 scripts/klonet-agent-op /usr/local/bin/klonet-agent-op
sudo chown root:root /usr/local/bin/klonet-agent-op
sudo chmod 0755 /usr/local/bin/klonet-agent-op
```

## 安装 sudoers

先选择一个专用 Linux 组，例如 `klonet-ops`，只把允许执行 Ops 变更的账号加入该组。

```bash
sudo install -m 0440 scripts/klonet-agent-op.sudoers /etc/sudoers.d/klonet-agent-op
sudo visudo -cf /etc/sudoers.d/klonet-agent-op
```

如果你不用 `%klonet-ops` 组，需要先编辑 `/etc/sudoers.d/klonet-agent-op`，把组名替换成服务器实际策略。

## 安全边界

不要直接放行 screen、kill、bash、python、nginx。sudoers 只允许：

```text
/usr/local/bin/klonet-agent-op restart-screen-component --execute *
/usr/local/bin/klonet-agent-op stop-screen-component --execute *
/usr/local/bin/klonet-agent-op stop-platform-screens --execute *
/usr/local/bin/klonet-agent-op start-platform-screens --execute *
/usr/local/bin/klonet-agent-op reload-nginx --execute *
```

原因是参数校验、组件白名单、screen 与平台名匹配、project_root 注入防护、启动命令模板都在 helper 内完成。放行底层命令会绕过这些校验。

## Agent 调用方式

dry-run 由 Agent 普通用户直接调用 helper，不经过 sudo，也不会修改服务器：

```text
/usr/local/bin/klonet-agent-op <action> --dry-run ...
```

真实执行统一使用非交互 sudo：

```text
sudo -n /usr/local/bin/klonet-agent-op <action> --execute ...
```

`-n` 禁止 sudo 弹出密码提示；如果专用账户、用户组或 sudoers 未正确配置，命令会立即失败。不要通过对话、stdin、环境变量或工具参数向 Agent 提供 sudo 密码。只有运行 Agent 的专用 `klonet-agent` 账户应加入 `klonet-ops`，日常登录账户不应加入该组。

## 真实执行开关

使用 `install-klonet-agent-service.sh` 部署时，环境文件会默认包含：

```bash
KLONET_AGENT_OPS_REAL_EXECUTION=1
```

如果未使用部署脚本，需要手动把该变量加入运行 Agent 的环境。真实执行仍然必须满足：

- `/usr/local/bin/klonet-agent-op` 已安装并归属 `root:root`
- `/etc/sudoers.d/klonet-agent-op` 已通过 `visudo -cf`
- 当前 Linux 用户只拥有 helper 入口的 sudo 权限
- 本轮 OperationPlan 已经展示给用户并收到精确 `confirm <plan_id>`
- 特权步骤已经收到精确 `confirm-step <plan_id> <step_id>`

## 验证

```bash
/usr/local/bin/klonet-agent-op restart-screen-component --dry-run \
  --platform 102 \
  --component master \
  --screen 102_m \
  --project-root /home/adminis/lht/102_project
```

输出中应包含：

```text
klonet_agent_op
action=restart-screen-component
dry_run=true
environment_changed=false
```

停止单个 screen 组件的 dry-run 验证：

```bash
/usr/local/bin/klonet-agent-op stop-screen-component --dry-run \
  --platform 102 \
  --component master \
  --screen 102_m
```

输出中应包含：

```text
klonet_agent_op
action=stop-screen-component
dry_run=true
environment_changed=false
```

停止整个平台四个后端 screen 的 dry-run 验证：

```bash
/usr/local/bin/klonet-agent-op stop-platform-screens --dry-run \
  --platform 102
```

输出中应包含：

```text
klonet_agent_op
action=stop-platform-screens
screen_sessions=102_m,102_c,102_web,102_w
dry_run=true
environment_changed=false
```

新平台四个后端 screen 的 dry-run 验证：

```bash
/usr/local/bin/klonet-agent-op start-platform-screens --dry-run \
  --platform 103 \
  --project-root /home/adminis/lht/103_project/vemu_uestc
```

输出中应包含：

```text
klonet_agent_op
action=start-platform-screens
screen_sessions=103_m,103_c,103_web,103_w
dry_run=true
environment_changed=false
```

Nginx 配置校验与 reload 的 dry-run 验证：

```bash
/usr/local/bin/klonet-agent-op reload-nginx --dry-run
```

输出中应包含：

```text
klonet_agent_op
action=reload-nginx
test_command=nginx -t
reload_command=nginx -s reload
dry_run=true
environment_changed=false
```

真实执行前，Ops Agent 还必须先通过只读工具确认目标平台、screen、进程和项目路径归属，不能只凭用户文字或历史记忆执行。

## 部署前置检查

`deploy_platform` 计划如果提供了 `operation_args.project_root`，第一步 `precheck` 会先执行只读 `validate_project_files` recipe，确认该目录下存在：

```text
gun.py
master_main.py
celery_worker.py
web_terminal_main.py
worker_gun.py
worker_main.py
```

这些文件应位于实际启动目录，也就是项目根目录 `vemu_uestc` 下；如果仍只在 `mains/` 目录内，precheck 会阻断后续 `start-services`。

真实执行 `start-platform-screens --execute` 前，helper 还会执行只读 `screen -ls` 检查。如果目标 screen 名（例如 `103_m`、`103_c`、`103_web`、`103_w`）已经存在，helper 会拒绝启动并返回 `screen_session_already_exists=...`，避免重复创建同名平台进程。

真实执行 `restart-screen-component --execute` 或 `stop-screen-component --execute` 前，helper 会确认目标 screen 仍然存在；如果不存在，会返回 `screen_session_not_found=...` 并拒绝宣称环境已修改。真实执行 `stop-platform-screens --execute` 前，helper 会先找出目标平台实际存在的 screen；如果一个都不存在，会返回 `screen_session_not_found=...`，如果只存在部分 screen，则只停止实际存在的目标 screen。

`start-platform-screens --execute` 和 `restart-screen-component --execute` 都会在 helper 层再次检查这些启动文件是否存在；如果缺失，会返回 `missing_project_entry_files=...` 并拒绝创建或重启 screen。这是为了防止绕过 Agent 侧 `precheck` 时误启动到错误目录。

真实执行 `start-platform-screens --execute` 前，helper 还会读取项目根目录下的 `vemu_config/config.py`，提取 `master_port`、`worker_port`、`public_port`、`web_terminal_port` 等平台端口；如果读不到可识别端口，会返回 `missing_config_ports=vemu_config/config.py` 并拒绝启动。读取到端口后，helper 会通过只读 `ss -ltn` 检查是否已经被监听；如果冲突，会返回 `port_already_listening=...` 并拒绝创建新平台 screen。

真实执行 `reload-nginx --execute` 时，helper 会先执行 `nginx -t`；只有配置测试成功才继续执行 `nginx -s reload`。如果配置测试失败，会返回 `nginx_test_failed` 和 `environment_changed=false`，计划执行层会阻断该步骤，但不会要求重新确认运行态是否未知。

如果真实执行阶段的底层命令失败，helper 会返回非零退出码并输出 `error=command_failed`、`failed_command=...` 和 `environment_changed=unknown`。这表示命令可能已经执行到中途；计划执行层会把该步骤标记为 `blocked` 而不是 `failed`，并要求先重新读取运行态环境后再决定是否继续计划。
## Install preparation helper checks

Archive extraction dry-run:

```bash
/usr/local/bin/klonet-agent-op extract-archive --dry-run \
  --archive-path /home/adminis/vemu_install_2024_12_5.tar \
  --destination-dir /root
```

Install script dry-run:

```bash
/usr/local/bin/klonet-agent-op run-install-script --dry-run \
  --script-dir /root/vemu_install_new_gen \
  --script-name base_requ_setup.sh \
  --script-args NORMAL
```

Do not pass sudo passwords through chat. Real execution should use the root-owned helper plus sudoers NOPASSWD allowlist and `KLONET_AGENT_OPS_REAL_EXECUTION=1`.
