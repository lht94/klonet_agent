# klonet-agent-op 安装契约

`klonet-agent-op` 是 Ops Agent 修改服务器环境时唯一应该被 sudoers 放行的入口。Agent 侧默认仍然 dry-run；只有 OperationPlan 已确认、单步已确认，并且服务端明确安装 helper 与 sudoers 后，才允许进入真实执行链路。

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

不要直接放行 screen、kill、bash、python。sudoers 只允许：

```text
/usr/local/bin/klonet-agent-op restart-screen-component --execute *
/usr/local/bin/klonet-agent-op stop-screen-component --execute *
/usr/local/bin/klonet-agent-op stop-platform-screens --execute *
/usr/local/bin/klonet-agent-op start-platform-screens --execute *
```

原因是参数校验、组件白名单、screen 与平台名匹配、project_root 注入防护、启动命令模板都在 helper 内完成。放行底层命令会绕过这些校验。

## 启用真实执行

Agent 侧默认仍然 dry-run。即使 helper 和 sudoers 已安装，`execute_ops_operation_step` 也只会生成预览，除非运行 Agent 的环境显式设置：

```bash
export KLONET_AGENT_OPS_REAL_EXECUTION=1
```

建议只在完成以下检查后设置该变量：

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

如果真实执行阶段的底层命令失败，helper 会返回非零退出码并输出 `error=command_failed`、`failed_command=...` 和 `environment_changed=unknown`。这表示命令可能已经执行到中途，Ops Agent 必须重新读取环境状态后再决定是否继续计划。
