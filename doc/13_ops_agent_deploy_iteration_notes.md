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
