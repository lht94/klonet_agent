# Ops-Privilege 知识能力矩阵

## 目的

Ops-Privilege V3 把规划、实现绑定、执行和验证分开：

1. RAG Runbook：提供领域经验，不规定固定路径。
2. `probe`：执行有边界的只读事实采集。
3. `action`：优先实现已确认的语义步骤。
4. Shell Artifact：注册 Action 不覆盖时的固定、单次、逐步确认实现。
5. `checker` 与 Verifier：独立检查真实状态并形成反思证据。

失败重规划使用同一套能力目录：

```text
步骤失败
  → 停止后续变更
  → Verifier 按需选择补充只读探测并形成反思
  → 组成包含执行绑定、证据和环境变化的 Failure Packet
  → 同一个语义 Planner 结合统一环境事实和 Klonet RAG 自主重新规划
  → Execution Agent 重新选择注册 Action 或一次性 Shell
  → 拒绝没有实质差异的候选计划并限制循环次数
  → 展示新的自然语言修复计划
  → 用户重新确认后执行
  → checker 验收
```

原 `DomainWorkflowRegistry` 已删除，其经验迁移到
`knowledge/klonet/ops/agentic_operations_runbook.md` 并由 RAG 检索。注册表一致性
测试仍保证直接动作有实现、常见领域具备探测和验证能力。

## 知识库归纳出的高频能力

| 领域 | 主要知识来源 | 只读事实 | 受控动作 | 结果验证 |
| --- | --- | --- | --- | --- |
| 源码获取与更新 | `source_acquisition_git.md`、`dev/git_workflow.md` | Git remote、分支、revision、dirty 状态、文件哈希、归档清单 | clone、fetch、`pull --ff-only`、switch、submodule、revert、reset、restore、tag、push、归档解压、本地目录同步 | revision、目标文件/目录存在 |
| 基础环境部署 | `environment_setup.md` | OS、磁盘、Python、安装脚本、归档内容、命令路径 | 安全解压；固定参数运行标准安装脚本；按 SHA-256 执行已审查脚本；安装系统/Python 包 | 包状态、命令可用、脚本退出和工作流后置检查 |
| 公共依赖服务 | `environment_setup.md`、`common_troubleshooting.md` | systemd、Docker 容器/镜像/网络、端口、日志 | 服务启停/重启；容器启停/删除/重启策略；镜像加载/标记/删除；标准共享服务脚本 | 服务、容器、镜像、网络、监听端口 |
| Klonet 运行态 Redis | `environment_setup.md`、`startup_shutdown.md` | 当前 `PROJ_CONFIG`、`redis.conf`、Redis 进程、容器映射、实际监听端口 | 从明确二进制和配置文件启动独立 Redis；精确替换配置 | 配置端口必须一致且真实监听 |
| 平台配置与生命周期 | `startup_shutdown.md`、`multi_platform_startup.md` | 项目/包/入口目录关系、Python 运行时、端口、screen、进程 cwd、日志 | 校验/准备入口文件；启动、停止、重启平台或单组件；写入非敏感配置 | 六个入口文件、screen 会话、进程、端口、HTTP |
| Nginx 与前端路由 | `startup_shutdown.md`、前后端链路案例 | Nginx 目录、已启用配置、路由、端口、HTTP | 安装明确 `.conf`；语法检查后 reload | 配置文件存在、`nginx -t`、服务状态、HTTP 状态 |
| Docker daemon | `environment_setup.md` | `daemon.json` 语法/顶层键、Docker 状态、现有容器和网络 | 深度合并 JSON，修改前备份；重启 Docker | JSON 可解析、Docker 服务和相关容器状态 |
| Worker 注册与拓扑进度 | Worker 注册、拓扑进度案例 | Master/Worker 双向 TCP/HTTP、心跳、Celery、Redis、日志、Docker/OVS/libvirt 真实资源 | 精确修改配置、重启明确组件；其余修复按确认根因组合动作 | Worker 健康、注册稳定、首个失败边界和资源一致性 |
| KVM/libvirt | KVM 与虚机网络文档、案例 | `/dev/kvm`、virsh domain/network、进程、磁盘、日志 | domain start/shutdown/reboot；有归属证据时 destroy/undefine；运行已审查初始化脚本 | domain 状态或不存在 |
| OVS、tap、veth、Docker 网络 | 网络文档、ONOS/KVM 案例 | link、route、OVS bridge/port/controller、Docker network、端口 | OVS bridge/port 增删；链路 up/down/有归属删除；Docker 网络创建/连接/断开/有归属删除 | OVS 资源、链路管理状态、容器网络挂载 |
| 定向清理 | 故障案例和清理原则 | 容器/domain/OVS/link/PID/路径身份和归属 | 只删除目标明确且已有归属证据的资源 | 目标不存在、无关资源不变 |

## 统一环境事实模型

Planner 在生成计划前会获得结构化、脱敏的环境模型：

- 项目候选目录、源码包目录、平台运行根目录、入口来源目录、配置文件和可运行状态。
- Nginx 二进制、配置目录、配置文件和已启用站点。
- Redis 二进制、配置来源、端口、bind 地址，以及“是否配置鉴权”的元数据。
- MySQL、RabbitMQ 的配置来源、端口和“是否配置凭据”的元数据。
- Python 解释器候选。
- Docker、libvirt、OVS、screen、`/dev/kvm` 等宿主能力。

密码、token、私钥不会进入模型；环境事实只记录“是否已配置”和配置来源。环境模型带指纹，便于判断重新规划时环境是否真的发生变化。

## Planner 与执行边界

- Planner 只输出目标、依据、依赖、预期影响和成功标准；它看不到 Action 名称，
  不能输出命令，也没有确定性 happy-path fallback。
- Execution Agent 独占完整 Action Catalog，先尝试参数有界的注册 Action；Action
  的确定性风险是下限，模型不能降低。
- 注册 Action 在整体计划确认后自动执行；Shell Artifact 额外逐步确认。

## 安全边界

- 不向 Planner 注册“任意 Ubuntu shell”。Execution Agent 无法映射 Action 时才可
  生成 Shell Artifact，并固定脚本、cwd、run_as、环境、超时、哈希、nonce、环境
  指纹和有效期。
- Shell Artifact 使用 `bashlex` AST 与 `bash -n` 双重校验，禁止动态命令替换、
  嵌套解释器、后台执行、网络外传、凭据读取/内嵌、修改 Agent 安全边界和无边界删除；
  执行使用 `shell=False`，且只能使用一次。
- 删除容器网络、镜像、libvirt domain、OVS 或宿主链路时必须携带归属确认。
- Git 更新默认使用 fast-forward；hard reset、revert、restore、push 等高影响操作需要逐步确认。
- 归档解压拒绝路径穿越、链接和设备文件。
- 本地维护脚本必须固定路径、参数和 SHA-256。
- JSON 修改会先备份、深度合并并重新解析；敏感字段不能由计划写入。
- 不允许只凭退出码把关键状态判定为成功；Execution Agent 为已知动作补充确定性
  checker，证据不足时 Verifier 可自动执行最多两轮注册只读探测。

## 能力扩展规则

未来出现新的高频运维方式时，优先补 Runbook 和通用事实能力，不要求每条路线都
注册成固定动作。完整扩展可以包含：

1. 在 RAG Runbook 中描述经验、适用证据与验收方法。
2. 增加或复用只读 probe。
3. 对高频、稳定、可复用变更增加参数有界的 action；长尾能力由 Shell Artifact
   人工在环承接。
4. 增加能证明目标状态的 checker。
5. 增加 Action 绑定、Shell 拒绝、失败回传、重规划和迁移测试。

这样新增知识才会从“模型知道”变成“系统能够安全地做并确认做对”。
