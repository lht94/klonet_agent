---
title: Klonet 环境部署
status: current_with_environment_validation
priority: P0
domains: operations, deployment, dependencies
last_verified: 2026-06-22
---

# Klonet 环境部署

## 适用场景

用于准备 Master、Worker 和相关依赖环境。本文给出核对框架，不提供任何真实服务器地址、凭据或内部镜像仓库。

## 核心结论

Klonet 部署不是单一 Python 服务安装。完整环境通常包含 Python、Gunicorn、Celery、Redis、MySQL、RabbitMQ、Docker、OVS、tc、Nginx，以及按需启用的 KVM/libvirt、Docker Swarm、ONOS、监控和 Data Server。

历史安装脚本和依赖版本可能过期，必须先检查当前源码和目标系统。

## 部署前检查

- 明确目标角色：Master、Worker、Web Terminal、Data Server 或组合部署。
- 记录操作系统、内核、Python、Docker、OVS 和 libvirt 版本。
- 确认宿主机是否已有其他平台实例或容器。
- 确认端口、Redis DB、数据库名称和镜像目录不会冲突。
- 检查磁盘、内存、CPU、虚拟化支持和网络。
- 备份 Docker daemon、Nginx、systemd 和平台配置。
- 禁止在共享宿主机上未经确认重启 Docker 或批量清理资源。

## 基础组件

| 组件 | 作用 | 验证方式 |
| --- | --- | --- |
| Python | 运行 Flask、Celery 和工具脚本 | python --version |
| Gunicorn | 托管 Master/Worker Flask 应用 | 检查版本与进程 |
| Celery | 执行异步任务 | 检查 Worker 启动日志 |
| Redis | 运行态、任务状态和用户 DB | redis-cli 或健康检查 |
| MySQL | 用户、权限和长期元数据 | SQL 连接与模型初始化 |
| RabbitMQ | Celery 或消息依赖 | 管理命令和端口 |
| Docker | 容器节点和基础服务 | docker info、docker ps |
| OVS | 虚拟交换与端口 | ovs-vsctl show |
| tc/iproute2 | 链路参数和网络配置 | tc、ip 命令 |
| Nginx | 静态资源和 API 代理 | nginx -t、curl |
| libvirt/KVM | 虚机节点 | virsh list --all |
| Poppler/Tesseract | 知识文档 PDF/OCR 工具，不是平台运行依赖 | 工具版本 |

## 推荐部署顺序

1. 安装系统工具和 Python 环境。
2. 安装并验证 Docker、OVS 和网络工具。
3. 启动 Redis、MySQL、RabbitMQ 等依赖。
4. 初始化 MySQL 数据库和 Redis 配置。
5. 配置项目参数。
6. 配置容器镜像仓库。
7. 按需安装 libvirt/KVM 和执行初始化脚本。
8. 按需初始化 Docker Swarm/Overlay。
9. 配置 Nginx 和前端。
10. 启动 Master、Celery、Web Terminal、Worker。
11. 执行最小功能验证。

## Python 与依赖

历史资料曾依赖 Python 3.8 和一组较旧的 Flask 生态版本。不能直接在新环境中执行“缺什么就 sudo pip install”。

推荐：

- 从当前 requirements 或部署脚本建立独立环境。
- 记录依赖锁定文件。
- 保证 Gunicorn、Celery 和手工执行脚本使用同一 Python 环境。
- 对旧版本依赖先做兼容测试。
- 不在生产宿主机上临时升级核心库。

## Docker 与镜像仓库

Worker 需要能访问配置中的镜像仓库。修改 daemon.json 后：

1. 运行配置校验。
2. 记录当前运行容器。
3. 确认允许重启 Docker。
4. 重载并重启服务。
5. 验证 docker info、基础容器和镜像拉取。

insecure registry 仅用于受控内网，正式环境应使用 TLS 和认证。

## Redis 与 MySQL

Redis：

- 核对端口、DB 数量、鉴权和持久化。
- 多实例部署时分离端口和 DB。
- 检查 Worker 注册和用户 DB 映射。

MySQL：

- 核对数据库、字符集、用户权限和模型初始化。
- 不使用文档中的默认密码。
- 连接信息应从安全配置读取。

## KVM 前置条件

- CPU 和 BIOS 支持虚拟化。
- KVM 内核模块正常。
- libvirtd 正常运行。
- 默认或自定义网络可用。
- qcow2 镜像目录权限正确。
- Worker 进程有执行 virsh、virt-install 和网络操作的权限。
- libvirt_config.sh 已审查并在目标环境执行。

## Docker Swarm 与 ONOS

只有需要 Overlay 或 SDN 控制器时才启用：

- 明确 advertise address。
- 检查 Swarm 现有状态。
- 创建 attachable overlay 网络。
- 确认 ONOS 镜像、应用和端口。
- 不在正式知识中保存默认凭据。

## 部署验收

- 所有基础服务健康。
- Master、Celery、Worker 和 Terminal 进程正常。
- Worker 注册成功。
- 前端和 API 可达。
- 创建并删除一个最小拓扑。
- Docker/OVS/libvirt 中无遗留测试资源。
- 日志中没有持续重试或连接错误。

## 常见问题

### Gunicorn 能启动但接口不可用

检查绑定地址、端口、Nginx、应用导入路径和数据库初始化。

### Worker 启动但未注册

检查 Master 地址、Worker 端口、网络、防火墙、Redis 和注册日志。

### Docker 重启后平台异常

检查基础容器、镜像仓库、OVS、Swarm 网络和平台进程，避免直接重建数据。

## 证据来源

- klonet_knowledge/06_quick_start_docs/服务器基础环境部署、依赖服务检查、平台服务启动2024_3_15.md
- klonet_knowledge/02_vemu_uestc_code/doc/平台安装与部署/
- knowledge/staging/platform_operation_notes_lihetian_curated.md
- klonet_knowledge/09_onos_startup/ONOS启动.docx
- klonet_knowledge/07_vm_related/
