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
```

原因是参数校验、组件白名单、screen 与平台名匹配、project_root 注入防护都在 helper 内完成。放行底层命令会绕过这些校验。

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

真实执行前，Ops Agent 还必须先通过只读工具确认目标平台、screen、进程和项目路径归属，不能只凭用户文字或历史记忆执行。
