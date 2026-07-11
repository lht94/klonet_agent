# Ops Agent 部署测试与优化记录

## 2026-07-11 第一轮：lht 平台部署

### 测试目标

使用真实命令启动 ops agent：

```bash
python -m klonet_agent.agent --mode ops --user-id lht --project-id test
```

扮演用户要求部署平台 `lht`：后端源码和前端资源都通过 Git 拉取，服务端口自动选择且不与现有平台冲突。

### 观察到的问题

1. agent 能从知识库找到标准仓库地址，也能盘点端口、Nginx、Docker 基础服务，这是正向表现。
2. 首版计划中 `create-dirs` 是无 action 的普通 checkpoint，确认后被标记为 completed，但结果是 `environment unchanged`，目录实际没有创建。这会让部署状态机产生假进度。
3. `git clone <repo> <dest>` 步骤没有携带 `cwd`，策略返回泛化的 `git_args_not_allowed`，agent 难以判断是参数缺失还是 clone 形式不允许。
4. `install -d <path>` 被复用到系统文件安装策略中，只允许安装到 `/usr/lib`、`/usr/local/lib`、`/lib/modules`，导致部署目录 `/home/klonet-agent/platforms/...` 被拒绝。
5. agent 在受限后试图改用从 `102` 复制源码，这违反了用户“源码和前端都通过 git 拉取”的约束。
6. 只读诊断命令 `git`、`hostname`、`ip` 被拒绝，导致 agent 不能用安全方式确认 remote、主机名和网络信息。
7. 用户说“不要从 102 复制”时，意图线索里出现 `port=102`；用户说“如果被拒绝就停止”时，线索里出现 `action=stop`。这说明意图抽取需要区分操作目标和条件/引用文本。

### 第一版优化思路

本轮先修底层通用能力，而不是给 `lht` 写特殊规则：

- 将“工作区目录创建”和“系统文件安装”拆开处理。
- 允许受控 `mkdir -p`、`install -d` 在安全 cwd 下创建目录，但拒绝 `/`、`/etc`、`/usr`、`/var`、`/root` 等系统基准目录。
- 保持 `git clone` 必须带 `cwd` 且目标必须位于 `cwd` 内，额外让缺失 `cwd` 返回 `git_clone_requires_cwd`，方便 agent 正确恢复。
- 只读诊断补充 `git remote/status/rev-parse/config --get`、`hostname`、`ip addr/link/route show` 的安全子集。

### 实现文件

- `ops/command_policy.py`
  - 新增 `mkdir` 策略。
  - 为 `install -d` 增加工作区目录创建分支。
  - 为 `git clone` 增加更精确的拒绝原因。
- `tools/read_only_terminal.py`
  - 增加安全只读的 `git`、`hostname`、`ip` 命令子集。
- `tests/test_ops_command_policy.py`
  - 覆盖工作区目录创建、`git clone .`、缺 cwd 的精确报错。
- `tests/test_read_only_terminal.py`
  - 覆盖新增只读诊断命令的允许和拒绝路径。

### 后续待优化

- OperationPlan 中无 action 的部署步骤不应被当作“已执行成功”，尤其是标题/目的明显描述会修改环境时。
- prompt 或 planner 应明确要求 `run_ops_command.argv` 使用 JSON array，而不是字符串形式。
- 意图抽取需要避免把引用的历史平台号识别成端口，把条件性的“停止”识别成当前操作。

## 2026-07-11 第二轮：真实 clone 卡死

### 测试结果

第一版优化后，agent 重新生成计划 `deploy-eeb03e7c45`：

- `mkdir -p /home/klonet-agent/platforms/lht_project` 成功。
- `git clone gitee:uestc-minenet/vemu_uestc.git .` 成功，后端源码已拉取。
- `mkdir -p /home/klonet-agent/platforms/lht_project/vemu_frontend` 成功。
- `git clone git@github.com:lht94/vemu-web.git .` 长时间无输出，外部进程显示卡在 `ssh git@github.com git-upload-pack 'lht94/vemu-web.git'`。

这说明第一版策略解决了目录和 git clone 放行问题，但真实执行层缺少超时和非交互认证保护。

### 第二版优化思路

受控命令执行必须保证“失败可返回”，不能让交互式 SSH、网络连接或认证等待拖住整个 agent：

- 默认 `_run_command` 增加 `timeout=120`。
- git 命令执行时设置 `GIT_TERMINAL_PROMPT=0`，禁止 Git 交互式提示用户名/密码。
- git 命令执行时设置 `GIT_SSH_COMMAND=ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15`，让 SSH 缺密钥或连接失败快速返回。
- 捕获 `subprocess.TimeoutExpired`，将步骤置为 `blocked` 并要求 `inspect_runtime`，而不是让异常或挂起中断对话。
- 同步更新 `/usr/local/bin/klonet-agent-op` 对应脚本源码中的 `run-ops-command`，保持 sudo helper 路径和普通路径一致。

### 实现文件

- `ops/recipes.py`
  - 新增 `RUN_OPS_COMMAND_TIMEOUT_SECONDS`。
  - `_run_command` 增加 timeout 和 Git 非交互环境。
  - `_run_ops_command` 捕获 timeout / OSError 并返回结构化 blocked。
- `scripts/klonet-agent-op`
  - helper 侧 `run-ops-command` 同样增加 Git 非交互环境。
- `tests/test_ops_command_policy.py`
  - 覆盖 Git 命令 env/timeout。
  - 覆盖 timeout 会变成 blocked。

## 2026-07-11 第三轮：部分部署恢复与暂停指令

### 测试结果

第二版后继续执行旧计划：

- agent 能识别后端已 clone、前端目录为空，并通过 `confirm deploy-eeb03e7c45` 把上次中断的 `running` 步骤转为 `blocked`。
- agent 做了只读检查后恢复 `clone-frontend`，前端最终 clone 成功。
- 后续 `copy-startup-files` 被拒，原因是计划使用了多源 `cp`：`cp file1 file2 ... .`，而策略只支持单源单目标。
- 更重要的是，首版部署计划把后端仓库直接 clone 到 `/home/klonet-agent/platforms/lht_project`。但 `mains/gun.py`、`web_terminal_main.py` 等文件引用 `from vemu_uestc...`，标准结构应区分平台父目录和后端包目录。
- 当用户明确说“暂停，不要继续执行旧计划，只总结”时，agent 仍继续调用 `resolve_ops_blocked_step` 和读取文件，没有尊重暂停指令。

### 第三版优化思路

本轮优化两个通用方向：

1. 文件整理能力应覆盖 Klonet 部署常见原子操作：
   - 允许工作区内多源 `cp` 到 cwd 内目标。
   - 允许工作区内 `ln -s <source> <link>`，但 source 和 link 都必须在 cwd 内，拒绝指向 `/etc` 等外部路径。
2. 决策提示应防止继续扩大错误计划：
   - 用户明确暂停时，本轮禁止 approve/execute/resolve，只做总结。
   - 标准 VEMU 后端应 clone 到平台父目录下的 `vemu_uestc/`，不要直接 clone 到父目录后靠 symlink 掩盖结构问题。

### 实现文件

- `ops/command_policy.py`
  - `cp` 支持多源到工作区目标，分类为 `workspace_file_copy`。
  - 新增 `ln -s` 受控策略，分类为 `workspace_symlink_create`。
- `prompts.py`
  - 增加暂停指令硬约束。
  - 增加标准 VEMU 后端目录结构要求。
- `tools/registry.py`
  - 更新工具说明中的受控命令清单。
- `tests/test_ops_command_policy.py`
  - 覆盖多源 `cp`、安全/不安全 symlink。
- `tests/test_prompt_style.py`
  - 覆盖暂停规则和 `vemu_uestc/` 目录结构规则。

## 2026-07-11 第四轮：安装脚本误判成功

### 测试结果

第四轮恢复计划 `deploy-8ea38bb518` 执行了 `/root/vemu_install_new_gen/base_requ_setup.sh NORMAL`。脚本输出显示大量高影响操作和失败：

- 运行 Redis `make test`，耗时且输出极长。
- 导入 Docker 镜像、安装 InfluxDB、修改 Docker/Libvirt 相关环境。
- `libpcap`、`curl`、`python3.8` 编译出现 `Permission denied`、缺头文件、`pip3.8: command not found` 等错误。
- `libvirt-bin` 无安装候选，`libvirtd` 启动失败。

但 helper 最终输出 `environment_changed=true`，recipe 将步骤标记为 completed。随后 agent 认为“安装脚本成功”，继续执行后续步骤。这是错误的：历史安装脚本可能吞掉内部错误并返回 0，不能只信 exit code。

### 第四版优化思路

对 `base_requ_setup.sh NORMAL` 增加确定性后置条件校验：

- 脚本执行后必须能找到 `python3.8`、`pip3.8`、`gunicorn`、`celery`，或历史固定路径存在对应可执行文件。
- 若缺失，步骤返回 `blocked`，输出 `postcondition_failed=missing_commands=...`，并要求 `inspect_runtime`。
- 不用自由扫描长输出作为主要判断，避免误报；后置条件直接对应部署后续步骤所需能力。

### 实现文件

- `ops/recipes.py`
  - 新增 `_install_script_postcondition_problem`。
  - `run_install_script` 成功返回后先检查后置条件，不满足则 blocked。
- `tests/test_ops_operations.py`
  - 更新 root 脚本 helper 测试。
  - 新增 helper 返回成功但后置条件缺失时 blocked 的回归测试。
