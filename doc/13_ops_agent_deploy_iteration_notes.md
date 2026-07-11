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

## 2026-07-11 第五轮：环境恢复建议绕过受控计划

### 测试结果

第四版后重新启动 ops agent，并要求“不要继续旧计划、不要再运行 base_requ_setup.sh，只做只读检查”。agent 表现有明显改善：

- 遵守了暂停要求，只做只读诊断。
- 正确指出 `python3.8`、`pip3.8`、`gunicorn`、`celery` 均不可用，`/usr/local/python3/bin/` 不存在。
- 正确指出前端已 clone，后端被直接 clone 到 `lht_project` 根目录。

但在恢复建议中，agent 给出了类似：

```bash
curl -sS https://bootstrap.pypa.io/get-pip.py | python3.8
python3.8 -m pip install gunicorn celery gevent gevent-websocket
```

这违反了 Ops 的受控执行边界：不能建议用户复制 shell 管道，也不能把环境修复选择题丢给用户。环境恢复应进入 OperationPlan，并使用结构化 `run_ops_command`。

### 第五版优化思路

- 将受控 Python 包安装纳入 `run_ops_command`：
  - 允许 `pythonX -m pip install <安全包名>`。
  - 允许 `pipX install <安全包名>`。
  - 禁止 `-r requirements.txt`、URL wheel、任意 `python -c`。
  - pip 安装标记为 `dangerous`，需要 `confirm-step`。
- 更新 Ops prompt 和工具说明：
  - 环境恢复必须用 OperationPlan。
  - 禁止建议 `curl | python`、手工 sudo 或绕过 helper 的 shell。
  - base_requ_setup 后置条件失败时，优先用受控 apt + 受控 pip 步骤恢复；缺能力时明确报告通用能力缺失。

### 实现文件

- `ops/command_policy.py`
  - 新增 Python/pip 受控安装策略。
- `prompts.py`
  - 增加环境恢复边界和禁止 shell 管道规则。
- `tools/registry.py`
  - 更新工具 schema 描述中的受控命令清单。
- `tests/test_ops_command_policy.py`
  - 覆盖允许的 Python/pip 安装形式和拒绝的非受控形式。
- `tests/test_prompt_style.py`
  - 覆盖环境恢复必须走受控计划。

## 2026-07-11 第六轮：修改步骤被建成 checkpoint 占位

### 测试结果

第五版后，agent 已经不再建议 `curl | python`，而是生成了受控恢复计划 `deploy-5e17f42af1`：

- `apt-install-pip` 使用 `run_ops_command`，需要 `confirm-step`。
- `pip-install-deps` 使用 `python3.8 -m pip install`，需要 `confirm-step`。
- 没有继续运行 `base_requ_setup.sh`。

但计划中 `config-lht`、`config-terminal-port`、`config-nginx`、`config-frontend` 被创建成无 action 的 checkpoint，并且 agent 说明“执行前我会补充 action binding”。这会复发早期问题：无 action 的普通 checkpoint 会被状态机当成已完成，造成配置并未写入但计划显示成功。

### 第六版优化思路

- 状态机层兜底：自定义 `deploy_platform` 计划中，标题/目的看起来会修改环境的无 action 步骤必须 blocked，不能 completed。
- 只读验证类 checkpoint 仍允许完成，例如 `verify-deps`。
- Prompt 明确：安装、clone、复制、写配置、Nginx、reload、启动服务等修改步骤必须在创建计划时就绑定 action + args，不得先占位后补。

### 实现文件

- `ops/operations.py`
  - 新增 `_unbound_step_looks_mutating`。
  - 修改步骤无 action 时的执行分支，对 mutating checkpoint 返回 blocked。
- `prompts.py`
  - 增加“创建计划时必须绑定 action”的规则。
- `tests/test_ops_operations.py`
  - 覆盖修改型 checkpoint blocked。
  - 覆盖只读 checkpoint 仍可完成。
- `tests/test_prompt_style.py`
  - 覆盖 prompt 规则。

## 2026-07-11 第七轮：计划完整但含不安全写入和明文密钥

### 测试结果

第六版后，agent 成功生成 `deploy-57bf9448a0`，并且所有修改步骤都有 action 绑定，说明“checkpoint 占位”问题已改善。

新暴露的问题：

- `config-lht-class` 的 `write_ops_file.content` 含 `redis_password = '123456'`，并且回答中明文展示了该值。
- `config-nginx-location` 直接用 `write_ops_file` 修改 `/etc/nginx/sites-available/default`，绕过了既有的 staging `.conf` + `install_nginx_config` + `reload_nginx` 流程。
- `config.py` 路径指向 `/home/klonet-agent/platforms/lht_project/vemu_uestc/vemu_config/config.py`，但真实环境中 `vemu_uestc/` 子目录不存在。这说明 prompt 的标准结构规则仍需要结合“本轮 resolved_path 证据”，后续还要优化。

### 第七版优化思路

本轮先把高风险问题前移到计划创建阶段：

- `write_ops_file` 不能直接写 `/etc/nginx/...`；Nginx 必须通过 staging 文件和 `install_nginx_config`。
- `write_ops_file.content` 中不能包含明显的 password/token/secret/api key 赋值。
- Prompt 明确不得在回答或 OperationPlan action args 中写入明文敏感值；配置类应继承已有安全默认值或用 `[REDACTED]` 展示。

### 实现文件

- `ops/operations.py`
  - 新增计划级 action args 校验。
  - 拒绝直接写 `/etc/nginx`。
  - 拒绝敏感赋值内容进入 `write_ops_file.content`。
- `tests/test_ops_action_registry.py`
  - 覆盖直接 Nginx 写入和敏感内容拒绝。
- `prompts.py`
  - 强化 Nginx staging + install 流程。
  - 强化敏感字段脱敏和继承规则。
- `tests/test_prompt_style.py`
  - 覆盖 Nginx 直写禁令和明文密钥禁令。

## 2026-07-11 第八轮：只读诊断误报 allowlist，条件性停止误触发

### 测试结果

第七版后，agent 生成 `deploy-c30bb6e1a9`：

- 所有修改步骤都已绑定 action + args。
- 后端路径纠正为 `/home/klonet-agent/platforms/lht_project/vemu_config/config.py`。
- Nginx 使用 staging conf -> `install_nginx_config` -> `reload_nginx`，未直接写 `/etc/nginx`。
- `LhtConfig` 不再写入明文 `redis_password`。
- 用户要求“只生成 OperationPlan，不执行”时，计划保持 pending，未自动执行。

继续确认计划并执行到 `pip-install-deps` 后，新暴露两个通用问题：

- `python3.8` 不在 PATH，agent 诊断 `/usr/bin/python3.8` 时，`run_readonly_command` 返回 `program_not_allowlisted=/usr/bin/python3.8`。实际问题是绝对路径允许但可能不存在、不可执行或是断链；把它报成 allowlist 问题会误导后续判断。
- 用户说“如果任何步骤失败，先停止并报告原因”时，Ops 路由摘要出现 `action=stop`。这是条件性安全约束，不是当前停止平台操作。

### 第八版优化思路

- 只读终端解析绝对路径时先按 basename 走同一套允许程序和参数校验，再区分 `program_path_not_found`、`program_path_broken_symlink`、`program_path_not_executable` 等可诊断错误。
- 增加只读 `dpkg -l/-s`，使 agent 能诊断 apt 包安装状态，而不是只能靠 `which` 和 `ls`。
- Ops 路由在判断 stop action 前剔除“如果失败/报错/阻塞就停止”这类条件性停止短语，避免把恢复策略中的安全约束误解为当前操作。

### 实现文件

- `tools/read_only_terminal.py`
  - 绝对路径程序返回具体路径问题，不再混同为 `program_not_allowlisted`。
  - 新增安全 `dpkg` 诊断。
- `ops/routing.py`
  - 新增条件性停止约束清洗。
- `tests/test_read_only_terminal.py`
  - 覆盖断链绝对路径、允许的绝对路径执行、安全 `dpkg`。
- `tests/test_ops_routing.py`
  - 覆盖“失败就停止”不触发 `action=stop`。
