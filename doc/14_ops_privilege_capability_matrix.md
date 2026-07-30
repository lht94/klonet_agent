# Ops-Privilege 知识能力矩阵

## 目的

Ops-Privilege 不把知识库中的命令原样交给模型执行，而是把高频运维需求映射为四类可审计能力：

1. `workflow`：说明一个领域任务应按什么阶段完成。
2. `probe`：执行有边界的只读事实采集。
3. `action`：执行用户已经确认的结构化变更。
4. `checker`：独立检查变更后的真实状态。

失败重规划使用同一套能力目录：

```text
步骤失败
  → 停止后续变更
  → 确定性探测 + 模型选择的补充只读探测
  → 结合失败证据、统一环境事实和 Klonet RAG 确认根因
  → 只允许用已注册 action 生成修复步骤
  → 拒绝“没有修复便原样重试”的候选计划
  → 展示新的自然语言修复计划
  → 用户重新确认后执行
  → checker 验收
```

当前注册规模为 23 个领域工作流、43 个只读探测器、39 个直接执行动作和 35 个结果检查器。注册表一致性由测试保证：工作流引用的动作必须真实存在，直接动作必须有实现，常见知识领域必须同时具备探测和验证能力。

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

## 安全边界

- 不注册“任意 Ubuntu shell”。模型只能选择结构化动作，`run_ops_command` 也只能使用确定性策略允许的 `program + argv`。
- 删除容器网络、镜像、libvirt domain、OVS 或宿主链路时必须携带归属确认。
- Git 更新默认使用 fast-forward；hard reset、revert、restore、push 等高影响操作需要逐步确认。
- 归档解压拒绝路径穿越、链接和设备文件。
- 本地维护脚本必须固定路径、参数和 SHA-256。
- JSON 修改会先备份、深度合并并重新解析；敏感字段不能由计划写入。
- 不允许只凭退出码把关键状态判定为成功；Planner 未声明后置条件时会为已知动作自动补充确定性 checker。

## 能力扩展规则

未来知识库出现新的高频运维方式时，不应只在提示词里增加一条命令。完整扩展至少包含：

1. 在工作流中描述使用阶段与所需事实。
2. 增加或复用只读 probe。
3. 增加参数有界的 action，并定义风险、前置条件、影响和确认范围。
4. 增加能证明目标状态的 checker。
5. 增加正常、拒绝、失败恢复和安全边界测试。

这样新增知识才会从“模型知道”变成“系统能够安全地做并确认做对”。
