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

## 2026-07-11 第九轮：run_ops_command 被拒绝时计划仍可创建

### 测试结果

第八版后重新启动 agent，`dpkg -s` 只读诊断可用，`/usr/bin/python3.8` 断链也被正确识别。agent 生成新计划 `deploy-09d230795f`，用于 `apt install --reinstall python3.8-minimal` 恢复 Python。

新暴露的问题：

- `reinstall-python38` 是系统包修改，但计划中 `risk=controlled`、`requires_step_confirmation=false`。
- 根因不是单纯模型标注错，而是 `decide_ops_command` 当时不允许 `apt install --reinstall ...`，`_custom_steps` 在 command policy 拒绝时没有拒绝计划，反而退回到 action registry 默认风险，导致被包装成可执行的 controlled 步骤。

### 第九版优化思路

- `apt install --reinstall <safe-package>` 是合法恢复动作，但必须和普通 apt install 一样归类为 `dangerous`、`sudo`、`requires_step_confirmation=true`。
- `run_ops_command` 的计划创建必须以 command policy 为硬边界：只要策略拒绝，就直接拒绝 OperationPlan，而不是降级使用 action registry 默认值。
- 这样可以泛化覆盖 apt、pip、git、cp/install、tc、insmod/rmmod 等所有结构化命令，而不是只补 Python 断链这一个场景。

### 实现文件

- `ops/command_policy.py`
  - 允许安全包名的 `apt install --reinstall`，并保持危险级别和二次确认。
- `ops/operations.py`
  - 自定义计划创建时，`run_ops_command` 被 command policy 拒绝即抛错。
- `tests/test_ops_command_policy.py`
  - 覆盖 apt reinstall 强制二次确认。
  - 覆盖被拒绝的 `run_ops_command` 无法创建计划。

## 2026-07-11 第十轮：计划端允许的 apt reinstall 被 helper 端拒绝

### 测试结果

第九版后，新计划 `deploy-3c328e7676` 正确把 `reinstall-python38` 标成 `dangerous` 且要求 `confirm-step`。确认执行后，真实 helper 返回：

```text
apt_args_not_allowed
```

说明 Python 计划端 `ops/command_policy.py` 已允许 `apt install --reinstall ...`，但服务器执行端 `scripts/klonet-agent-op` 仍使用旧白名单，拒绝了同一命令。这是执行链路中的策略漂移。

### 第十版优化思路

- 不绕过到 `dpkg` 手工解包；那会扩大恢复动作的危险面。
- 同步 helper 端 apt 参数白名单，允许 `--reinstall`，并保持由计划端负责 `dangerous` + `confirm-step`。
- 增加直接执行 helper dry-run 的测试，确保真正 sudo helper 合同也接受该命令。

### 实现文件

- `scripts/klonet-agent-op`
  - `_validate_ops_command` 的 apt install 选项允许 `--reinstall`。
- `tests/test_ops_command_policy.py`
  - 新增 helper dry-run 覆盖 `apt install --reinstall -y python3.8-minimal`。

## 2026-07-11 第十一轮：helper 未升级应阻塞而不是生成替代高危方案

### 测试结果

第十版代码提交后，仓库内 `scripts/klonet-agent-op` 已支持 `--reinstall`，但实际执行路径 `/usr/local/bin/klonet-agent-op` 仍是旧 root-owned helper。当前账号无法通过 `sudo -n install ... /usr/local/bin/klonet-agent-op` 覆盖它，手动 sudo 返回 `sudo: a password is required`。

这说明部署继续阻塞在“受控执行基础设施未升级”，而不是业务平台配置问题。agent 在 helper 返回 `apt_args_not_allowed` 后曾倾向于建议 `dpkg` 解包等替代方案，这不合适：问题是 helper 版本漂移，应先升级 helper 或修 sudoers。

### 第十一版优化思路

- helper 端返回 `*_args_not_allowed` 时，Python runner 将其识别为 `helper_policy_mismatch`，状态为 blocked，下一步明确为 `upgrade_installed_ops_helper`。
- sudo 返回 password/no tty 时，识别为 `helper_sudo_not_configured`，下一步明确为 `install_ops_helper_sudoers`。
- 这类基础设施阻塞不应被包装成业务步骤失败，也不应引导模型改走更宽泛、更危险的系统修复命令。

### 实现文件

- `ops/recipes.py`
  - `_helper_failure_result` 增加 helper policy mismatch 和 sudoers/password failure 的 blocked 分类。
- `tests/test_ops_command_policy.py`
  - 覆盖 helper 策略漂移阻塞并提示升级 helper。
  - 覆盖 sudoers/password 问题阻塞并提示安装 sudoers。

## 2026-07-11 第十二轮：helper 策略漂移后模型尝试 apt-get/dpkg 绕路

### 测试结果

第十一版提交前的旧 agent 在 `apt_args_not_allowed` 后继续尝试：

- 生成 `dpkg` 恢复计划，被 `program_not_allowlisted=dpkg` 拒绝。
- 随后生成 `apt-get install --reinstall ...` 新计划，试图绕过 helper 对 `apt` 的拒绝。

这说明仅靠执行器把 helper mismatch 标为 blocked 还不够，prompt 也需要明确：基础设施合约漂移不是业务恢复路径的一部分。

### 第十二版优化思路

- 当结果包含 `helper_policy_mismatch` 或 helper 返回 `*_args_not_allowed`，且 Python 计划端已允许该命令时，必须停止业务部署并要求升级 installed helper。
- 禁止把 `apt` 换成 `apt-get`、改用 `dpkg` 解包、手工 sudo 或其它同类系统包命令来绕过 helper 版本漂移。

### 实现文件

- `prompts.py`
  - 增加 helper policy mismatch 的处理规则。
- `tests/test_prompt_style.py`
  - 覆盖不得通过 apt-get/dpkg 绕过 helper 版本漂移。

## 2026-07-12 第十三轮：放宽替代路径表达，保留用户约束和审计边界

### 复盘反馈

用户指出第五轮和第十二轮的表述过硬：

- agent 在无法执行时尝试寻找替代路径，本身是自主能力，不应被整体禁止。
- 真正的问题是替代路径是否违反用户明确约束，例如本轮用户要求源码和前端通过 git 拉取，因此从 `102` 复制源码不合适。
- `curl | python`、手工 sudo、apt-get/dpkg 等也不应被绝对从回答空间删除；它们可以作为外部管理员救援方案，但不能伪装成 agent 已执行或受控 helper 内的步骤。

### 第十三版优化思路

- 保留默认路径：agent 能执行的修改仍优先进入 OperationPlan 和受控 action。
- 放宽替代路径：受控能力不足时，可以提出外部管理员备选方案，但必须说明风险、来源校验、需要用户/管理员显式选择，且不得保存密码或密钥。
- 对 helper policy mismatch：默认应升级 installed helper；apt-get/dpkg/手工 sudo 可以列为外部救援选项，但不能自动生成成同一个受控部署步骤。
- 对源码来源：不做全局“禁止从其它实例复制”的规则；只是在用户明确要求 git 拉取时，替代源码来源必须先询问或说明会改变约束。

### 实现文件

- `prompts.py`
  - 将“不得建议/不得绕过”的绝对语气改为“默认受控计划；外部救援可列出但需风险说明和显式确认”。
- `tests/test_prompt_style.py`
  - 将测试从绝对禁止改为覆盖分层 fallback 语义。

## 2026-07-12 第十四轮：reload-nginx 无参数 sudoers 匹配失败

### 测试结果

按用户补充的管理员路径重新安装 helper/service 后，`apt install --reinstall -y python3.8-minimal` 成功恢复 `/usr/bin/python3.8`，pip 依赖安装、启动文件复制、后端配置、Nginx staging 都完成。

新阻塞出现在 `reload_nginx`：

- `install-nginx-config --execute --source-path ... --config-name ...` 可以通过 sudoers。
- `reload-nginx --execute` 返回 `sudo: a password is required`。

根因是 sudoers 模板只有：

```text
/usr/local/bin/klonet-agent-op reload-nginx --execute *
```

但 `reload-nginx` 没有额外参数，`--execute *` 不能匹配无参数命令。

### 第十四版优化思路

- 对无额外参数的 helper 子命令，在 sudoers 中增加精确命令项。
- Nginx helper 遇到 sudo password/no tty 时，应和通用 helper 一样归类为基础设施 blocked，而不是 failed。

### 实现文件

- `scripts/klonet-agent-op.sudoers`
  - 增加 `/usr/local/bin/klonet-agent-op reload-nginx --execute,` 精确项。
- `ops/recipes.py`
  - `_nginx_helper_failure_result` 识别 sudoers/password failure，返回 `helper_sudo_not_configured` blocked。
- `tests/test_ops_helper_install_contract.py`
  - 覆盖 reload-nginx 精确 sudoers 项。
- `tests/test_ops_operations.py`
  - 覆盖 reload_nginx sudoers 缺失时 blocked。

## 2026-07-12 第十五轮：start-platform-screens 命令成功但服务未存活

### 测试结果

修复 sudoers 后，前端配置收尾完成，agent 进入平台冷启动。它先错误尝试 `restart_platform`、`screen`、`gunicorn`、`python3.8 -m gunicorn`、`bash` 等路径，最终在用户纠正后用 `deploy_platform` 默认模板触发 `start_platform_screens`。

`start_platform_screens` 返回 completed，但健康验收显示：

- `screen` 在 `klonet-agent` 用户下不可见。
- 27700/27701/27702 均未监听。
- `gunicorn/celery/web_terminal` 进程不存在。

根因是 helper 只检查了 `screen -dmS ...` 命令是否返回成功，没有检查 screen 会话和端口是否在短时间后仍存活。服务启动后立即崩溃时，计划会被误标为完成。

### 第十五版优化思路

- helper 执行 `start-platform-screens --execute` 后，等待短时间做后置条件验证。
- 验证目标：
  - `lht_m/lht_c/lht_web/lht_w` screen 会话存在。
  - `master_port`、`worker_port`、`web_terminal_port`/`terminal_port` 监听。
- 不把 `public_port`/Nginx 8380 或 data server 端口作为后端 screen 必须监听的条件。
- 后置条件失败时返回非 0，并输出 `environment_changed=unknown`，让 Python runner blocked，而不是 completed。

### 实现文件

- `scripts/klonet-agent-op`
  - 新增 `runtime_required_ports` 和 `wait_for_started_platform`。
  - `start-platform-screens` 执行后检查 screen 和核心端口。
- `tests/test_ops_helper_script.py`
  - 更新成功用例以覆盖启动后 screen/端口验收。
  - 新增 screen/端口后置条件失败时返回 blocked 信号。

## 2026-07-12 第十六轮：端口解析扫描了所有配置类

### 测试结果

第十五版 helper 安装后，真实执行 `start-platform-screens` 的后置条件失败输出包含大量端口：

```text
missing_listening_ports=12000,12001,5005,45551,...
```

这些端口来自 `config.py` 中其它历史配置类，而当前文件底部实际是：

```python
PROJ_CONFIG = LhtConfig()
```

`LhtConfig` 的核心端口只有 27700、27701、27702。

### 第十六版优化思路

- `configured_ports` 和 `runtime_required_ports` 优先解析 `PROJ_CONFIG = <Class>()` 指向的类体。
- 找不到 `PROJ_CONFIG` 或类定义时才回退到全文扫描，保持旧配置兼容。
- 后置条件只要求 master/worker/web_terminal/terminal 端口，不要求 Nginx public_port。

### 实现文件

- `scripts/klonet-agent-op`
  - 新增 `active_config_content` 和 `_ports_from_content`。
  - 端口解析限定到激活配置类。
- `tests/test_ops_helper_script.py`
  - 覆盖只读取 `PROJ_CONFIG` 激活类端口。

## 2026-07-12 第十七轮：平台启动失败缺少真实 Python 异常

### 测试结果

第十六版 helper 安装后，真实执行：

```text
start-platform-screens --execute --platform lht --project-root /home/klonet-agent/platforms/lht_project
```

后置条件已经能正确失败，但输出仍只有：

```text
start_postcondition_failed missing_started_screens=lht_m,lht_c,lht_web,lht_w missing_listening_ports=27700,27701,27702
```

继续以前台方式运行组件入口后发现两个更具体的问题：

- helper 启动模板仍使用 `/usr/local/bin/gunicorn`、`/usr/local/bin/celery`、`/usr/local/python3/bin/python3.8`，但当前机器实际 Python 是 `/usr/bin/python3.8`，依赖安装在 `klonet-agent` 用户的 `~/.local`。
- 应用入口还存在真实导入错误，例如 master 预检报 `ModuleNotFoundError`，首先缺少 `numpy`；其它组件还暴露出部署目录名和源码内 `vemu_uestc` 包名不匹配的问题。

### 第十七版优化思路

- helper 通过 sudo 获得 root 授权时，不应把平台 screen 和 Python 进程直接启动成 root；应回到发起 sudo 的 `SUDO_USER` 下执行 screen 的 start/stop/list。
- 启动模板不要依赖历史安装目录中的 `gunicorn/celery/python3.8` 绝对路径，改为用 `/usr/bin/python3.8 -m gunicorn`、`/usr/bin/python3.8 -m celery`，让 Python 使用运行用户自己的 site-packages。
- `start-platform-screens` 在真正创建 screen 前做启动预检：
  - gunicorn master/worker 使用 `--check-config`。
  - celery 和 web terminal 做入口导入检查。
  - 预检失败时返回 `startup_preflight_failed component=... detail=...`，并保持 `environment_changed=false`。
  - 预检详情使用更长的单行摘要，保留 `No module named '...'` 这类可直接行动的包名线索。
- 这样 agent 下一轮可以基于具体异常安装缺失依赖、修正包名/部署目录，而不是对“screen 秒退”盲猜。

### 实现文件

- `scripts/klonet-agent-op`
  - 启动命令改为 `/usr/bin/python3.8 -m ...`。
  - 新增 `runtime_user`、`_as_runtime_user`、`_screen_command`，将 screen 操作统一归属到 `SUDO_USER`。
  - 新增 `STARTUP_PREFLIGHT_COMMANDS` 和 `startup_preflight_problem`。
- `tests/test_ops_helper_script.py`
  - 更新启动命令 contract。
  - 覆盖 root helper 回到 `SUDO_USER` 执行 screen。
  - 覆盖启动预检失败时不创建 screen、返回可诊断错误。

## 2026-07-12 第十八轮：计划执行层吞掉 helper 预检诊断

### 测试结果

第十七版 helper 安装后，真实 `start_platform_screens` 已经能返回：

```text
error=startup_preflight_failed component=master ... ModuleNotFoundError: No module named 'concurrent_log_handler'
```

但 agent 执行 `deploy_platform` 计划时只向模型展示：

```text
结果：命令执行失败，返回码 2。
```

于是 agent 没法直接利用 helper 的诊断，转而反复读配置、搜索源码，并误判到 `mask_indices`。这说明诊断能力已经在 helper 层具备，但在 OperationPlan runner 的失败分类/摘要中丢失了。

### 第十八版优化思路

- 将 helper 的 `startup_preflight_failed` 从普通 `helper_failed` 中单独分类。
- 返回 `blocked` 而不是 `failed`，因为环境未改变，下一步应继续修依赖/导入错误，而不是把计划终结为不可恢复失败。
- 保留更长 stderr 摘要，让 `No module named '...'` 传回模型。
- 设置 `next_required_action=inspect_startup_preflight`，提示 agent 围绕启动预检继续恢复。

### 实现文件

- `ops/recipes.py`
  - `_helper_failure_result` 识别 `startup_preflight_failed`，返回 `helper_startup_preflight_failed ...`。
  - helper stderr 摘要放宽到 1200 字符。
- `tests/test_ops_operations.py`
  - 覆盖 start platform 预检失败时计划步骤 blocked，且保留缺失模块名。

## 2026-07-12 第十九轮：warning 和长 traceback 遮蔽最后异常

### 测试结果

继续启动后，helper 预检输出变成：

```text
CryptographyDeprecationWarning: Python 3.8 is no longer supported ...
Failed to read config file: gun.py
Traceback ...
```

由于摘要取前 1200 字，真正的最后异常：

```text
ModuleNotFoundError: No module named 'nsenter'
```

被截断掉了。agent 因此推断到 pip/OpenSSL 冲突等旁支，没能直接处理当前缺失模块。

### 第十九版优化思路

- 启动预检摘要不能只保留开头，因为 Python traceback 的根因通常在最后一行。
- 从完整 stderr/stdout 中提取最后一个 `*Error:` 或 `*Exception:` 行，作为 `last_error=...` 放在摘要最前。
- 仍保留一段 compact detail，方便看到 warning 和导入链上下文。

### 实现文件

- `scripts/klonet-agent-op`
  - 新增 `startup_preflight_detail` 和 `last_python_error_line`。
  - `startup_preflight_failed` detail 改为优先包含 `last_error=...`。
- `tests/test_ops_helper_script.py`
  - 覆盖 warning + traceback 场景下保留最后缺失模块名。

## 2026-07-12 第二十轮：pip 被用户 site 依赖污染后缺少受控恢复路径

### 测试结果

批量安装依赖后，`python3.8 -m pip` 自身开始崩溃：

```text
AttributeError: module 'lib' has no attribute 'X509_V_FLAG_NOTIFY_POLICY'
```

原因是用户 site-packages 中安装了新版 `cryptography 47.0.0`，但系统 `pyOpenSSL 19.0.0` 会优先混用它，导致 pip 导入链崩溃。验证发现：

```text
PYTHONNOUSERSITE=1 python3.8 -m pip --version
```

可以恢复 pip，因为 pip 运行时忽略用户 site，仅使用系统 Python 包。

### 第二十版优化思路

- 不允许 agent 通过 shell 前缀随意注入环境变量。
- 在 `run_ops_command` 的结构化参数中增加很窄的 env allowlist。
- 目前仅允许 `PYTHONNOUSERSITE=1`，且只允许用于非 sudo 的 `python/pip install` 类受控命令。
- 执行器将 allowlist env 合并进 subprocess 环境，并在 dry-run/执行结果中显示 `env=...`，方便审计。

### 实现文件

- `ops/command_policy.py`
  - `OpsCommandDecision` 新增 `env`。
  - 新增 env 校验，仅允许 `PYTHONNOUSERSITE=1`。
  - env 只可附加到 `python_package_install`，不支持 sudo 命令。
- `ops/recipes.py`
  - `_run_command` 支持合并受控 env。
  - `run_ops_command` 结果显示 env。
- `tests/test_ops_command_policy.py`
  - 覆盖安全 env 允许、危险 env 拒绝、执行时 env 传入 subprocess。

## 2026-07-12 第二十一轮：等价的 `python -s -m pip` 恢复形态被拒

### 测试结果

agent 在 pip 被用户 site 污染后选择了：

```text
python3.8 -s -m pip install --user flask-socketio
```

这是与 `PYTHONNOUSERSITE=1 python3.8 -m pip ...` 等价的恢复思路：`-s` 禁用用户 site，避免 pip 自身导入坏掉的用户依赖；`--user` 仍把目标包安装到运行用户 site。  
但命令策略只允许 `python -m pip install`，因此返回 `python_args_not_allowed`。

### 第二十一版优化思路

- 允许 Python 安全前缀 `-s`，但只允许出现在 `-m pip install` 前。
- 允许 pip 的 `--user` 选项。
- 风险级别仍是 `dangerous`，并保持 `confirm-step`，因为它仍然会修改 Python 环境。

### 实现文件

- `ops/command_policy.py`
  - `_decide_python` 支持 `python -s -m pip install ...`。
  - `_decide_pip_install` 允许 `--user` 并保留 Python 前缀。
- `tests/test_ops_command_policy.py`
  - 覆盖 `python3.8 -s -m pip install --user flask-socketio` 被允许且需要二次确认。

## 2026-07-12 第二十二轮：受控写文件过窄导致 agent 试图绕到 sed/python -c

### 测试结果

Docker socket 权限问题来自 `config_prometheus.py` 顶层 `import docker`。agent 正确选择“小范围代码修复”，但 `write_ops_file` 拒绝了普通项目 `.py`：

```text
unsupported_file_type=config_prometheus.py
```

随后 agent 尝试改用 `sed`，又被 `program_not_allowlisted=sed` 拒绝；再尝试 `python3.8 -c` 过滤文件，也会被 Python 策略拒绝。  
这说明结构化写文件能力太窄，反而诱导 agent 寻找更不透明的 shell/脚本绕路。

### 第二十二版优化思路

- 允许非系统路径下的普通 `.py` 通过 `write_ops_file` 修改。
- 继续拒绝 `/etc`、`/usr`、`/bin`、`/sbin`、`/lib*` 等系统 Python 路径。
- 保留敏感文件名拒绝、Nginx 直接写入限制和备份机制。

### 实现文件

- `ops/recipes.py`
  - `.py` 文件支持从固定启动文件白名单扩展到非系统路径。
  - 新增 `_is_system_ops_write_path`。
- `tests/test_ops_operations.py`
  - 更新普通项目 `.py` 可写契约。
  - 新增系统 `.py` 路径仍被拒绝的测试。

## 2026-07-12 第二十三轮：write_ops_file 的 anchor 被清洗丢失缩进

### 测试结果

agent 在 `link_operate.py` 中尝试把：

```python
    client =  docker.from_env()
```

替换成：

```python
    import docker
    client =  docker.from_env()
```

但 `write_ops_file` 连续返回：

```text
anchor_match_count=0 expected=1
```

根因不是文件内容不匹配，而是 OperationPlan 保存 action args 时对所有字符串做 `_one_line(...).strip()`，导致 `anchor` 的前导缩进被清掉。对 Python 代码来说，anchor 的空白是语义和匹配的一部分。

### 第二十三版优化思路

- `write_ops_file.content` 已经保留原始字符串，`anchor` 也应同样保留。
- 只对 `write_ops_file.anchor` 放宽，避免影响其它 action 参数的紧凑化/安全展示。

### 实现文件

- `ops/operations.py`
  - `_apply_action_bindings` 对 `write_ops_file.anchor` 保留原始字符串。
- `tests/test_ops_operations.py`
  - 覆盖带前导空格的 `replace_text` anchor 可以成功匹配并保留缩进。

## 2026-07-12 第二十四轮：缺少受控 Docker 组授权 action

### 测试结果

Python 依赖和若干顶层 Docker 导入问题修复后，平台仍在启动预检中访问 Docker socket。源码中存在大量模块级 `docker.from_env()`，逐个 lazy import 工作量大且风险高；Klonet 平台运行本身也需要 Docker 权限。

agent 正确提出：

```text
usermod -aG docker klonet-agent
```

但 `run_ops_command` 拒绝 `usermod`：

```text
program_not_allowlisted=usermod
```

于是 agent 只能要求管理员外部执行，这不满足“通过受控 helper 自主完成部署”的目标。

### 第二十四版优化思路

- 不把 `usermod` 加进通用命令白名单。
- 新增专用 helper action：`ensure-user-group --user klonet-agent --group docker`。
- helper 内部只允许 `("klonet-agent", "docker")` 这一组 membership，防止变成任意提权工具。
- agent 侧 action 标记为 `dangerous` 且 `confirm-step`，因为 docker 组约等于 root 权限。

### 实现文件

- `scripts/klonet-agent-op`
  - 新增 `ensure-user-group` 子命令。
  - 新增窄 allowlist `ALLOWED_USER_GROUP_MEMBERSHIPS`。
- `scripts/klonet-agent-op.sudoers`
  - 允许 helper 的 `ensure-user-group --execute *`。
- `ops/actions.py`
  - 注册 `ensure_user_group` action。
- `ops/recipes.py`
  - 新增 `_ensure_user_group` handler。
- `tests/test_ops_helper_script.py`
  - 覆盖 dry-run contract 和非 allowlist membership 拒绝。
- `tests/test_ops_helper_install_contract.py`
  - 覆盖 sudoers 项。
- `tests/test_ops_operations.py`
  - 覆盖 action 需要二次确认并调用 helper。
