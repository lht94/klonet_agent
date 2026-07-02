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
