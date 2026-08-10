# 平台运行检查、自动修复与故障注入测试报告

- 测试日期：2026-08-08 至 2026-08-09
- Agent 代码：`/home/lzl/klonet_agent`
- 执行账户：`lzl`
- Agent 模式：`ops-privilege`
- 测试对象：102、v4e2e、正式 vemu、test vemu，以及服务器上的其他运行候选和代码目录
- 安全约束：未安装 helper；未把 sudo 密码写入代码、配置或报告；未使用模糊 kill；未执行 `git reset --hard`；未清理用户工作区改动

## 1. 总结

三个测试均完成，最终结果如下：

| 测试 | 最终结果 | 关键结论 |
| --- | --- | --- |
| 测试一：查询正常运行平台数量 | 通过 | Agent 最终识别 4 个健康实例、1 个后端异常运行候选、6 个仅代码目录 |
| 测试二：修复异常平台 | 通过 | 正式和 test 两份 vemu 已按不同根目录、不同端口独立健康；最终重复执行时幂等返回“无需变更” |
| 测试三：102 启动故障注入与修复 | 通过 | Agent 定位并删除注入代码，只恢复 master；源文件 SHA-256、属主和权限恢复，master/worker 均健康 |
| 定向自动化回归 | 通过 | 344 passed |
| Linux 全量自动化回归 | 通过 | 984 passed，退出码 0 |

最终独立接口验收：

| 实例 | 角色 | 端口 | `/server_health/` |
| --- | --- | ---: | --- |
| 102 | master | 27694 | HTTP 200，`code=1` |
| 102 | worker | 27695 | HTTP 200，`code=1` |
| v4e2e | master | 47001 | HTTP 200，`code=1` |
| v4e2e | worker | 47002 | HTTP 200，`code=1` |
| 正式 vemu | master | 45551 | HTTP 200，`code=1` |
| 正式 vemu | worker | 45552 | HTTP 200，`code=1` |
| test vemu | master | 45554 | HTTP 200，`code=1` |
| test vemu | worker | 45555 | HTTP 200，`code=1` |

## 2. 测试一：查询正常运行的平台数量

### 2.1 我怎么测试的

1. 先独立采集代码根目录、运行进程、进程 cwd、实例配置端口、监听 PID 和 `/server_health/`。
2. 启动真实 Agent：

   ```text
   cd /home/lzl
   PYTHONPATH=/home/lzl /home/lzl/miniconda3/envs/klonet_agent/bin/python \
     -m klonet_agent.agent --mode ops-privilege \
     --user-id runtime-e2e-final-test1b \
     --project-id runtime-health-final-test1b
   ```

3. 发送计划规定的只读问题。
4. 检查 Agent 是否按规范化 `project_root` 区分实例，是否把代码目录误算为运行实例，是否同时验证 master、worker 及健康接口。
5. 将 Agent 输出与独立基线逐实例对比。本测试没有确认任何变更，也没有启动代码目录。

### 2.2 模拟用户输入

```text
检查一下当前服务器上有多少正常运行的平台。只有后端服务已经启动，并且后端接口可以正常使用的才算；服务器上仅仅存在代码目录不算。
```

### 2.3 Agent 最终原始回答

以下内容直接来自持久化会话 `runtime-e2e-final-test1b/runtime-health-final-test1b`：

```text
正常运行实例（4）：
- project_root=/home/klonet-agent/102；platform=102；backend_status=healthy；master_port=27694，master_endpoint=healthy；worker_port=27695，worker_endpoint=healthy
- project_root=/home/lzl/klonet_v4_e2e；platform=v4e2e；backend_status=healthy；master_port=47001，master_endpoint=healthy；worker_port=47002，worker_endpoint=healthy
- project_root=/home/lzl/test/vemu_uestc；platform=test；backend_status=healthy；master_port=45554，master_endpoint=healthy；worker_port=45555，worker_endpoint=healthy
- project_root=/home/lzl/vemu_uestc；platform=vemu_uestc；backend_status=healthy；master_port=45551，master_endpoint=healthy；worker_port=45552，worker_endpoint=healthy
后端异常的运行候选（1）：
- project_root=/home/lzl/xxy/klonet；platform=klonet；backend_status=abnormal；master_port=46551，master_endpoint=not_checked；worker_port=46552，worker_endpoint=not_checked
只有代码、没有后端运行证据的目录（6）：
- /home/klonet-agent/platforms/lht_project
- /home/klonet-agent/platforms/lht_project_backup_20260724
- /home/klonet-agent/vemu_uestc
- /home/lzl/XiaoYiCeshi
- /home/lzl/lht/test_klonet/vemu_uestc
- /home/lzl/xxy/xxy_klonet/vemu_uestc
```

### 2.4 最终判定

通过。回答满足以下条件：

- 以 `project_root` 为实例身份，没有按同名 `platform_name` 合并。
- 102 即使不是依赖 screen 健康判定，仍被正确识别为健康。
- 正式与 test vemu 分别列出，没有拼接不同目录的 master/worker。
- 仅代码目录单独报告，没有计入健康数量。
- Nginx、public、web_terminal 没有影响后端健康数量。

## 3. 测试二：修复两份 vemu 实例

### 3.1 我怎么测试的

1. 初始基线中，`/home/lzl/vemu_uestc` 与 `/home/lzl/test/vemu_uestc` 存在跨目录运行证据和缺失角色，不能组成一个平台。
2. 要求 Agent 先诊断，再明确两个根目录都要作为独立实例恢复。
3. 对 Agent 生成的每份计划审查：

   - `project_root` 是否正确；
   - 配置文件是否属于目标实例；
   - formal 是否保留 45551/45552；
   - test 是否使用独立的 45554/45555；
   - 停止目标是否同时绑定 PID、cwd、角色和端口；
   - master/worker 是否拆成单角色 Action；
   - 是否会触碰 102、v4e2e 或 `/home/lzl/xxy/klonet`。

4. 不合格计划全部拒绝，没有确认执行。每次失败先补最小自动化用例，再修复 Discovery、Planner、Binding 或 Action 契约。
5. 对符合边界的计划使用精确 `plan_id + hash` 确认。
6. 修复后分别请求两个实例的 `/server_health/`，并核对监听端口和项目根目录。
7. 最终再次发送两个根目录的修复目标，验证系统是否幂等：已经健康时不得重启或生成无意义计划。

### 3.2 实际恢复阶段的一份 Agent 原始计划

正式实例最后缺少 worker 时，使用了一个单组件恢复计划。以下直接来自会话 `codex_e2e_repair55_20260808/default`：

```text
V4 change plan priv-v4-93931ee0e7
Goal: 继续完成 /home/lzl/vemu_uestc 正式实例恢复。当前 master 45551 已健康，worker 45552 仍缺失；只启动正式 worker 并验证 master、worker 的 /server_health/。不要修改配置或 Git，不要重启 master，不要停止或修改 celery、web_terminal、data_server、public/Nginx、102、v4e2e、test 实例或 /home/lzl/xxy/klonet。
Risk: medium
Frozen resources:
- instance_root (path/frozen): /home/lzl/vemu_uestc
- entry_source_root (path/frozen): /home/lzl/vemu_uestc/mains
- runtime_cwd (path/frozen): /home/lzl
- python_executable (path/frozen): /home/lzl/miniconda3/bin/python3.13
- active_config (identifier/frozen): WtxConfig
- master_port (port/frozen): 45551
- worker_port (port/frozen): 45552
- instance_identifier (identifier/frozen): vemu_uestc
- config_path (path/frozen): /home/lzl/vemu_uestc/vemu_config/config.py
Changes:
- change-1: 启动正式 vemu_uestc worker 角色 — 在 /home/lzl/vemu_uestc 正式实例中启动缺失的 worker 角色，使其监听 45552 端口并提供 /server_health/ 健康端点，同时保持 master 45551 健康不受影响。; recover worker for /home/lzl/vemu_uestc
  expected: 启动正式 vemu_uestc worker 进程，使用 /home/lzl/miniconda3/bin/python3.13 与 WtxConfig 配置，工作目录 /home/lzl，入口位于 /home/lzl/vemu_uestc/mains; worker 进程监听 127.0.0.1:45552 并提供 /server_health/ 端点; master 45551 保持运行且健康，不受 worker 启动影响; 不修改 vemu_config/config.py 或任何 Git 内容，不重启 master，不触碰其它实例; start missing worker role at 45552 and backend health succeeds
  verify: port_listening, http_status, process_running, backend_health
  binding: registered_action: start_screen_component args={'component': 'worker', 'project_root': '/home/lzl/vemu_uestc/mains', 'platform': 'vemu_uestc', 'screen_session': 'vemu_uestc_w', 'instance_root': '/home/lzl/vemu_uestc', 'entry': '/home/lzl/vemu_uestc/mains', 'cwd': '/home/lzl', 'command': '/home/lzl/miniconda3/bin/python3.13', 'config': 'WtxConfig', 'master_health': '45551', 'port_45551': '45551', 'worker_health': '45552', 'port_45552': '45552', 'instance_name': 'vemu_uestc', 'path': '/home/lzl/vemu_uestc/vemu_config/config.py', 'master_port': '45551', 'worker_port': '45552'}
Exact plan hash: b1f9079fd38a31660b6aabfb5a60f405cfb95930fb8c67601ebf8127da5a5456
Confirm this exact V4 change plan with:
confirm-priv-v4 priv-v4-93931ee0e7 b1f9079fd38a31660b6aabfb5a60f405cfb95930fb8c67601ebf8127da5a5456
```

确认后 Agent 的原始回答：

```text
V4 change plan completed.
```

### 3.3 最终幂等复测输入

```text
两个都要修复成能够独立正常运行的平台，不能共用同一个端口，也不能把一个目录的 master 和另一个目录的 worker 拼成一个平台。目标根目录是 /home/lzl/vemu_uestc 和 /home/lzl/test/vemu_uestc。
```

### 3.4 Agent 最终原始回答

以下直接来自会话 `runtime-e2e-final-test2b/runtime-health-final-test2b`：

```text
目标实例已经分别满足后端健康标准，无需变更或重启：
- project_root=/home/lzl/test/vemu_uestc；platform=test；backend_status=healthy；master_port=45554，master_endpoint=healthy；worker_port=45555，worker_endpoint=healthy
- project_root=/home/lzl/vemu_uestc；platform=vemu_uestc；backend_status=healthy；master_port=45551，master_endpoint=healthy；worker_port=45552，worker_endpoint=healthy
```

### 3.5 最终判定

通过。

- `/home/lzl/vemu_uestc`：master 45551、worker 45552，均健康。
- `/home/lzl/test/vemu_uestc`：master 45554、worker 45555，均健康。
- 两个实例没有共用 master/worker 端口。
- 最终运行清单按两个根目录分别归属进程和端口。
- 102 与 v4e2e 的四个健康接口在修复后仍为 HTTP 200、`code=1`。
- `/home/lzl/xxy/klonet` 不在本测试的最终授权边界内，没有确认针对它的变更计划。

## 4. 测试三：向 102 注入启动故障并由 Agent 修复

### 4.1 故障注入前准备

1. 确认 102 master 27694、worker 27695 均健康。
2. 保存 `/home/klonet-agent/102/mains/master_main.py` 的原始内容和独立应急备份。
3. 记录原始属性：

   ```text
   sha256=e4ed12ede18433ad1d400feffcf2f5ee606642692513f5d5e4a2b00950cb1271
   uid=997 gid=997 mode=775 size=1929
   ```

4. 记录已有 Git 工作区状态，不清理或覆盖用户修改。

### 4.2 注入方式

在 `master_main.py` 模块加载路径加入唯一故障标识：

```python
def KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE():
    raise RuntimeError("KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE")


KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE()
```

保持文件属主和权限不变，通过受控 reload/restart 触发。条件轮询确认：

- master `/server_health/` 不可用；
- worker `/server_health/` 仍为 HTTP 200、`code=1`；
- 故障只影响 102 master。

### 4.3 模拟用户输入

```text
102 平台刚新增了一点代码后无法正常启动了。请检查后端日志和启动入口，找出真正原因并修复，修复后重新验证 master 和 worker 接口。
```

### 4.4 Agent 最终原始计划回答

以下直接来自会话 `runtime-e2e-test3-pass25/runtime-health-test3-pass25`：

```text
V4 change plan priv-v4-4273277ac8
Goal: 102 平台刚新增了一点代码后无法正常启动了。请检查后端日志和启动入口，找出真正原因并修复，修复后重新验证 master 和 worker 接口。
Risk: high
Frozen resources:
- master_main_path (path/frozen): /home/klonet-agent/102/mains/master_main.py
- master_port (port/frozen): 27694
- worker_port (port/frozen): 27695
- master_pid (identifier/frozen): 3236506
- home_klonet_agent_102_master_pid (identifier/frozen): 3236506
- master_uid (identifier/frozen): 997
- master_python_executable (path/frozen): /usr/bin/python3.8
Changes:
- change-1: 移除 master_main.py 中注入的启动失败函数 — 从 /home/klonet-agent/102/mains/master_main.py 中删除 KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE 函数定义及其无条件调用，使 master 模块能够正常导入并启动。; recover master for /home/klonet-agent/102
  expected: 删除 master_main.py 中 KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE 函数定义; 删除 master_main.py 中 KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE() 调用; master_main.py 文件内容更新，不再包含注入的启动失败代码; restart unhealthy master role at 27694 and backend health succeeds; 停止旧的 master 进程 (PID 3236506); 启动新的 master 进程，加载修复后的 master_main.py; master 进程开始监听 27694 端口
  verify: file_not_contains, python_file_syntax_valid, process_running, port_listening, backend_health, backend_health
  binding: registered_action: replace_text_in_file args={'path': '/home/klonet-agent/102/mains/master_main.py', 'old_text': 'def KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE():\n    raise RuntimeError("KLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE")\n\n\nKLONET_E2E_INJECTED_102_MASTER_BOOT_FAILURE()\n\n\n', 'new_text': '', 'master_port': '27694', 'port_27694': '27694', 'worker_port': '27695', 'port_27695': '27695', 'pid': '3236506', 'master_pid': '3236506', 'run_as_uid': '997', 'python_executable': '/usr/bin/python3.8'}
  binding: registered_action: stop_klonet_component args={'runtime_cwd': '/home/klonet-agent/102', 'component': 'master', 'pid': '3236506', 'port': '27694', 'path': '/home/klonet-agent/102/mains/master_main.py', 'master_port': '27694', 'port_27694': '27694', 'worker_port': '27695', 'port_27695': '27695', 'master_pid': '3236506', 'run_as_uid': '997', 'python_executable': '/usr/bin/python3.8'}
  binding: registered_action: start_screen_component args={'component': 'master', 'path': '/home/klonet-agent/102/mains/master_main.py', 'master_port': '27694', 'port_27694': '27694', 'worker_port': '27695', 'port_27695': '27695', 'pid': '3236506', 'master_pid': '3236506', 'run_as_uid': '997', 'python_executable': '/usr/bin/python3.8', 'platform': '102', 'project_root': '/home/klonet-agent/102/mains', 'screen_session': '102_m'}
Exact plan hash: dc3a273a963725c4a681f8259a2931f4a035f02838ec54c80c759e6dd39a80bc
Confirm this exact V4 change plan with:
confirm-priv-v4 priv-v4-4273277ac8 dc3a273a963725c4a681f8259a2931f4a035f02838ec54c80c759e6dd39a80bc
```

测试框架审查根目录、文件、旧 PID、角色、端口、运行 UID 和 Python 解释器后，发送精确确认：

```text
confirm-priv-v4 priv-v4-4273277ac8 dc3a273a963725c4a681f8259a2931f4a035f02838ec54c80c759e6dd39a80bc
```

Agent 的原始完成回答：

```text
V4 change plan completed.
```

### 4.5 Agent 外部的独立验收

Agent 完成后，测试框架在 Agent 会话外独立检查，结果为：

```text
master_main.py sha256=e4ed12ede18433ad1d400feffcf2f5ee606642692513f5d5e4a2b00950cb1271
uid=997 gid=997 mode=775 size=1929
old_master_pid=3236506 absent
new_master_arbiter_pid=3543762
new_master_uid=997 gid=997
new_master_cwd=/home/klonet-agent/102/mains
new_master_exe=/usr/bin/python3.8
new_master_screen=102_m
worker_pid=2607775
worker_pgid=2607770
worker_port=27695
```

最终接口结果：

```text
port=27694 http=200 body={"code":1,"msg":"serverisrunning"}
port=27695 http=200 body={"code":1,"msg":"serverisrunning"}
port=47001 http=200 body={"code":1,"msg":"serverisrunning"}
port=47002 http=200 body={"code":1,"msg":"serverisrunning"}
port=45551 http=200 body={"code":1,"msg":"serverisrunning"}
port=45552 http=200 body={"code":1,"msg":"serverisrunning"}
port=45554 http=200 body={"code":1,"msg":"serverisrunning"}
port=45555 http=200 body={"code":1,"msg":"serverisrunning"}
```

### 4.6 最终判定

通过。

- Agent 从日志和启动源码定位到唯一注入标识，没有换端口或无限重启。
- 只修改目标文件并只停止/启动 102 master。
- worker 进程组未被重启。
- 源文件最终 SHA-256 与注入前完全一致。
- 源文件 UID、GID、权限和大小恢复。
- 102 master、worker 均恢复健康。
- v4e2e 和两份 vemu 的端口及健康状态未回退。
- 没有使用应急备份代替 Agent 修复，因此本次属于 Agent 自主修复成功。

## 5. 测试中发现并修复的架构问题

本轮没有为特定目录或端口添加硬编码结果。主要通用修复包括：

1. 运行实例以规范化 `project_root` 为唯一身份。
2. 增加确定性批量 `running_platforms` 探针，一次遍历全部候选。
3. 每条进程、端口、接口和 screen 证据携带实例根目录。
4. 端口监听必须核对进程 cwd，不能只判断端口有人监听。
5. master、worker 分角色判定和输出，screen 不作为健康必要条件。
6. 修复 Discovery 探针预算、重复补证和多实例目标边界问题。
7. 超长 Planner JSON 截断后改为紧凑语义计划，并限制重试。
8. 多字段、多角色步骤在 Binding 前确定性原子化。
9. 强制校验 Action 的项目根目录、组件角色、端口、Screen 名称和依赖顺序。
10. 停止组件前同时核对 PID、进程组、cwd、角色和端口。
11. 源码修复使用精确文本编辑及语法检查，不通过导入有副作用地验证。
12. 启动动作冻结运行 UID 与 Python 解释器，支持跨用户目标进程。
13. 已达到目标状态时返回幂等成功，不生成无意义修复计划。
14. 只读平台回答使用确定性结构化渲染，始终保留 `project_root`。

## 6. 自动化回归结果

定向回归覆盖 Discovery、Coordinator、Planner、Binding、Mutation、Response、Action Runner 和环境工具：

```text
344 passed in 25.12s
```

Linux 全量回归：

```text
984 passed in 46.29s
exit_code=0
```

`git diff --check` 通过。当前修改尚未提交；原有未跟踪文件 `Miniconda3-latest-Linux-x86_64.sh` 未被修改。

## 7. 关于 lzl、sudo 与 helper

- Agent 确实从 `/home/lzl/klonet_agent` 启动，进程账户是 `lzl`。
- 本轮没有安装 helper。
- 102 的后端进程属于 UID 997。普通 `lzl` 进程不能稳定读取该用户 `/proc/<pid>/cwd`，所以跨用户 PID/cwd 归属核验需要 ops-privilege 的已有 sudo 通道。
- 测试期间仅建立临时 sudo 凭据和保活；测试结束后已精确停止保活并执行 `sudo -k`，没有留下额外高权限会话。
- 这说明 helper 不是 ops-privilege 的必需组件；但涉及其他 Unix 用户进程时，仍需要一种受控提权方式。本次使用的是现有 sudo，而不是新安装 helper。

## 8. 后续交互质量与 v4e2e 重启回归

针对实际对话中暴露的语言漂移、权限感知、跨轮审批和 Screen 可操作性问题，又完成了一轮架构修复与真实验证。

### 8.1 修复内容

1. 用户输出统一使用中文阶段名、计划标签和运行步骤描述；Action、checker、参数名作为可审计代码标识保留。
2. 每个 ops-privilege 运维目标都会主动探测当前 UID、无交互 sudo、既有 helper 和跨用户 `/proc` 可见性，不再等用户提醒“其实有权限”。本轮没有安装 helper。
3. 会话目录持久化已解析目标、中文 locale、目标根目录、阶段和静态证据；跨进程继续审批时复用静态证据，但 PID、Screen、端口和健康状态必须刷新。
4. 显式重启健康角色绑定 `restart_screen_component`；缺失角色才绑定 `start_screen_component`。
5. 新建 Screen 使用交互式 bash 和受控 rc 文件，不使用 `bash -lc ... exec ...` 替换 shell；Screen 内提供 `klonet_start`、`klonet_status` 和 `klonet_command`。
6. 重启既有交互式 Screen 时，在原会话中发送 Ctrl-C，等待目标端口释放，再输入 `klonet_start`，不销毁 Screen。

### 8.2 Agent 最终原始计划回答

模拟用户输入：

```text
帮我重启 v4e2e 的 master 和 worker
```

Agent 生成并经测试框架审查通过的最终计划：

```text
实施绑定：语义步骤 1/1：正在把“重启 v4e2e 的后端角色”展开为原子实施计划…
实施绑定：实现子步骤 1/2：正在为“重启 master Screen 组件”绑定原子能力…
实施绑定：能力边界：步骤“重启 master Screen 组件”固定使用已注册动作：restart_screen_component。
实施绑定：实现子步骤 2/2：正在为“重启 worker Screen 组件”绑定原子能力…
实施绑定：能力边界：步骤“重启 worker Screen 组件”固定使用已注册动作：restart_screen_component。
工作流协调器：V4 变更计划 priv-v4-1a711eec02
目标：帮我重启 v4e2e 的 master 和 worker
风险：destructive
冻结资源：
- instance_root (path/frozen): /home/lzl/klonet_v4_e2e
- instance_identifier (identifier/frozen): v4e2e
- master_port (port/frozen): 47001
- worker_port (port/frozen): 47002
- run_as_uid (identifier/frozen): 1000
- python_executable (path/frozen): /home/lzl/miniconda3/envs/klonet-py38/bin/python3.8
变更步骤：
- restart-backend-roles: 重启 v4e2e 的后端角色 — 按项目根目录 /home/lzl/klonet_v4_e2e 重启 master 和 worker
  预期：按要求重启 master 角色（端口 47001），并确认后端健康; 按要求重启 worker 角色（端口 47002），并确认后端健康
  验收：backend_health, backend_health
  执行绑定：已注册动作 restart_screen_component 参数={'component': 'master', 'instance_root': '/home/lzl/klonet_v4_e2e', 'project_root': '/home/lzl/klonet_v4_e2e/mains', 'platform': 'v4e2e', 'master_port': '47001', 'worker_port': '47002', 'run_as_uid': '1000', 'python_executable': '/home/lzl/miniconda3/envs/klonet-py38/bin/python3.8', 'screen_session': 'v4e2e_m'}
  执行绑定：已注册动作 restart_screen_component 参数={'component': 'worker', 'instance_root': '/home/lzl/klonet_v4_e2e', 'project_root': '/home/lzl/klonet_v4_e2e/mains', 'platform': 'v4e2e', 'master_port': '47001', 'worker_port': '47002', 'run_as_uid': '1000', 'python_executable': '/home/lzl/miniconda3/envs/klonet-py38/bin/python3.8', 'screen_session': 'v4e2e_w'}
```

精确确认后，Agent 的原始回答：

```text
工作流协调器：变更计划已执行并通过验证。
```

### 8.3 独立验收结果

- `v4e2e_m` 的 Screen PID 始终为 `3634962`，重启过程中会话没有被销毁。
- `v4e2e_w` 的 Screen PID 始终为 `3635212`，重启过程中会话没有被销毁。
- master/worker Gunicorn PID 均发生更新，证明实际完成重启，而不是只返回成功文本。
- 所有新 Gunicorn 进程 UID 为 lzl，cwd 为 `/home/lzl/klonet_v4_e2e/mains`。
- 47001、47002 分别由该根目录的 master、worker 监听。
- 两个 `/server_health/` 最终均返回 HTTP 200、`code=1`。
- 单独对 worker Screen 发送 Ctrl-C 后，47002 停止监听但 Screen 仍存在；向同一 Screen 输入 `klonet_start` 后接口恢复。
- `screen -r v4e2e_m` 或 `screen -r v4e2e_w` 后，用户可以 Ctrl-C 停止前台服务，并运行 `klonet_start` 再次启动。

### 8.4 最终回归与跨用户只读归属

最终检查还覆盖了 lzl 无法直接读取 UID 997 的 `/proc/<pid>/cwd` 的情况。运行清单现在只沿已观察到的 PPID 链，从 Screen 父进程命令中的精确 `cd <project_root>/mains` 向 Gunicorn 子进程传播根目录，不按名称猜测，也不需要用户再次提示 sudo 密码。修复后只读清单为：

```text
runtime_candidate_count=5
healthy_count=4
abnormal_count=1
code_only_count=6
102=/home/klonet-agent/102 master=27694 healthy worker=27695 healthy
v4e2e=/home/lzl/klonet_v4_e2e master=47001 healthy worker=47002 healthy
test=/home/lzl/test/vemu_uestc master=45554 healthy worker=45555 healthy
formal=/home/lzl/vemu_uestc master=45551 healthy worker=45552 healthy
```

最终 Linux 全量回归：

```text
998 passed in 44.38s
```

其中包含新增的跨用户 PPID 根目录传播用例。

## 9. 诊断与任务执行的统一结果循环

针对“为什么必须用户反复说继续，Plan-Replan 不能自行走到最终答案”的问题，本轮把原有的单次 Discovery/Synthesis 流程改为有明确终止条件的结果循环。

### 9.1 终止合同

每轮只允许进入以下四种状态：

- `achieved`：已经得到满足用户目标的、可由证据支持的最终结论；因果诊断必须确认根因或确认当前没有故障，只有现象摘要不能结束。
- `continue`：仍缺少能够通过已注册只读探针获得的技术事实；Coordinator 自动执行探针、更新证据并再次规划，不把工作退回给用户。
- `needs_user_decision`：确实存在无法从服务器事实推出的目标、范围或授权选择，才询问用户。
- `blocked`：已注册能力、权限或外部系统形成真实阻塞，并明确给出已经尝试的证据。

诊断补证循环当前有 8 轮安全上限，并对语义相同的探针请求去重。到达上限不是成功，必须返回具体阻塞原因。`你自己定位`、`继续查`、`查清楚`、`接着排查` 等跨轮表达会复用上一轮诊断目标和静态证据；进程、PID、端口、Screen 和健康状态仍重新采集。

变更任务沿用同一个结果合同：已确认计划若在执行或验收中暂停，Coordinator 自动把已完成步骤、失败步骤、返回码和检查结果转为 `plan_execution` 证据，先进入只读诊断循环；根因明确后只为尚未完成的效果生成恢复计划。恢复计划仍需新的精确 `confirm <plan_id>`，因此“自动诊断/重规划”不会绕过变更审批，也不会重复已成功的步骤。

### 9.2 根目录与证据连续性修复

注册探针返回的 `project_root`、`git_root`、`repository` 和进程 `cwd` 现在可以扩展本轮可信项目根目录。`.../mains` 会归一化为实例根目录。这样从 Screen 或进程确认 `/home/lzl/klonet_v4_e2e` 后，后续日志和源码读取无需用户再提示权限，也不会再错误报告 `path_outside_grounded_project_roots`。

### 9.3 真实 Agent 诊断测试

测试命令使用 lzl 账户和真实 ops-privilege Agent：

```bash
PYTHONPATH=/home/lzl /home/lzl/miniconda3/envs/klonet_agent/bin/python \
  -m klonet_agent.agent --mode ops-privilege \
  --user-id runtime-e2e-diagnostic-loop --project-id v4e2e-diagnosis
```

模拟用户只输入一次：

```text
检查 v4e2e_m 当前 Screen 里为什么报错，你自己持续定位到根因
```

Agent 的关键原始输出如下（省略每个探针的固定“本轮只读补证完成”提示）：

```text
Discovery：正在收集只读证据：privilege_capabilities
Discovery：正在执行只读检查：screen
Discovery：正在执行只读检查：screen_session
Discovery：正在执行只读检查：process
Discovery：正在执行只读检查：system_environment
Discovery：正在执行只读检查：process_logs
Discovery：正在执行只读检查：logs
工作流协调器：诊断补证循环 1/8：继续获取形成最终结论所需的证据。
Discovery：正在执行只读检查：ops_file
Discovery：正在执行只读检查：logs
工作流协调器：诊断补证循环 2/8：继续获取形成最终结论所需的证据。
Discovery：正在执行只读检查：process
Discovery：正在执行只读检查：path_permissions
工作流协调器：诊断补证循环 3/8：继续获取形成最终结论所需的证据。
工作流协调器：已定位根因：Gunicorn worker 在 post_worker_init 阶段执行
worker.log.info 时，concurrent_log_handler 尝试创建日志锁文件，但配置计算出的
/home/lzl/klonet_v4_e2e/mains/vemu_uestc/logs 目录不存在，因而抛出
FileNotFoundError。可选择创建缺失目录，或把日志路径改到现有且可写的目录。
```

整个诊断只有一次用户输入；Agent 自行进行了 3 轮 Plan→Probe→Synthesis→Replan，没有要求用户提供日志、异常顶部、PID 或源码。

### 9.4 独立根因核验

Agent 的结论与独立证据一致：

```text
vemu_config/config.py:347
loggging_error_filepath = f'{os.getcwd()}/vemu_uestc/logs/error.log'

Screen 完整 traceback
FileNotFoundError:
/home/lzl/klonet_v4_e2e/mains/vemu_uestc/logs/.__access.lock
```

异常来自 `concurrent_log_handler` 的 `atomic_open`，并在 `gun.py` 的 `post_worker_init` 日志调用处触发；目标日志目录实际不存在。因此这次最终回答是由日志、源码配置和路径状态共同确认的因果根因，不是根据报错关键词猜测。

### 9.5 自动化与运行状态验收

新增或扩展的回归覆盖：

- 可发现的技术缺口必须转为注册探针，禁止要求用户自行提供日志或进程信息；
- 语义重复探针会被拒绝并修复；
- 只有真实目标选择才进入 `needs_user_decision`；
- 因果问题没有根因证据时不能错误返回 `achieved`；
- “你自己定位”等后续输入复用上一诊断目标；
- Screen/进程证据自动建立可信项目根目录；
- 已确认变更执行暂停后自动诊断并生成只包含未完成效果的新恢复计划；
- 新恢复计划继续执行精确审批，不绕过安全边界。

最终 Linux 全量回归：

```text
1009 passed in 45.43s
```

`git diff --check` 和相关模块 `py_compile` 均通过。最终运行态复核中，27694、27695、47001、47002、45551、45552、45554、45555 的 `/server_health/` 全部返回 HTTP 200、`code=1`，没有造成其他实例状态回退。
