---
title: Klonet 领域术语
status: current_verified
priority: P0
domains: terminology, architecture, topology, operations
last_verified: 2026-06-22
---

# Klonet 领域术语

## 适用场景

用于统一 Agent、开发者和运维人员的表达。术语含义以当前代码为主，重构规划中的新概念会明确标注。

## 核心结论

同名概念必须区分运行角色、数据角色和规划角色。尤其不要混淆平台 Worker 与 Celery Worker、宿主机与拓扑节点、Redis 状态与真实资源，以及当前架构与重构蓝图。

## 核心术语

| 术语 | 含义 | 主要证据 |
| --- | --- | --- |
| Klonet | 网络虚拟化、网络仿真和教学实验平台 | 项目材料与源码 |
| Master | 控制和编排入口，提供 Master API 并调度 Worker | mains/master_main.py |
| Worker | 资源所在主机的执行服务 | mains/worker_main.py |
| Celery Worker | 消费异步任务的进程，不等同于平台 Worker | mains/celery_worker.py |
| Frontend | 提供拓扑编辑、用户操作和状态展示的 Web 前端 | 前端版本目录 |
| Nginx | 公共入口、反向代理和静态文件服务 | 运维资料 |
| Data Server | 实验数据计算和图表服务 | mains/data_server_main.py |
| Web Terminal | 连接容器或 KVM 控制台的终端服务 | mains/web_terminal_main.py |
| Topology | 用户定义和部署的一组节点、链路与服务 | topo API 与任务 |
| Subtopology | 拓扑切分后分配给某个 Worker 的执行单元 | topo2subtopo |
| NE | Network Element，平台中的网络节点抽象 | Service_layer/NEManager.py |
| Host | 主机类节点，通常用于业务或流量端点 | 拓扑模型 |
| Switch | 交换类节点，可能由 OVS 等实现 | 拓扑模型 |
| Router | 路由类节点 | 拓扑模型 |
| Controller | SDN 控制器节点，如 ONOS | 拓扑模型 |
| Service | 节点创建后部署的二层或业务服务 | /worker/service/ |
| Link | 节点之间的连接及其网络参数 | LinkManager |
| Veth pair | Linux 虚拟以太网对，常用于同机节点连接 | link_operate.py |
| VXLAN | 跨主机二层覆盖网络能力 | link_operate.py |
| OVS | Open vSwitch，提供虚拟交换和端口管理 | Implement_layer |
| tc/netem | Linux 流量控制与时延、丢包模拟工具 | 链路配置实现 |
| nsenter/netns | 进入目标网络命名空间执行操作的机制 | 链路实现 |
| KVM | 基于内核虚拟化的虚机节点能力 | NEManager 与 KVM 文档 |
| qcow2 | KVM 常见磁盘镜像格式 | KVM 镜像实现 |
| libvirt/virsh | 管理 KVM 虚机的服务与命令接口 | Web Terminal 与运维资料 |
| tap | 将虚机网卡连接到宿主机网络的虚拟设备 | KVM 链路实现 |
| Redis DB0 | 保存用户映射和 Worker 等全局运行状态 | redisAPI.py |
| User DB | 为具体用户分配的 Redis DB | UserMapRedis、UserDB |
| worker_list | 已注册或可用 Worker 集合相关表 | WorkerRedis |
| worker_resource | Worker 资源状态相关表 | redisAPI.py |
| topo2subtopo | 拓扑到子拓扑列表的映射 | topo 任务 |
| subtopo2worker | 子拓扑到 Worker 的映射 | topo 任务 |
| Process Bar | 拓扑部署、删除或文件任务进度状态 | deploy_process_bar.py |
| Image Registry | 容器镜像或实验镜像仓库能力 | image_registry API |
| KVM Image | 可上传、同步和实例化的虚机镜像 | kvm_image API |
| Traffic | 流量定义、下发和实时查询能力 | traffic API |
| Monitor | 节点、平台、链路和实验数据监控 | monitor API |
| Satellite | 卫星拓扑生成、时变链路和天地一体化实验扩展 | satellite 模块 |
| Heartbeat | Worker 或服务健康状态检测机制 | health_check 与 heartbeat API |
| Control API | 重构蓝图中的无状态 API 入口，当前尚未完全落地 | 重构蓝图 |
| Orchestrator | 重构蓝图中的独立编排器，当前职责仍主要在 Master | 重构蓝图 |
| Worker Agent | 重构蓝图中的意图执行单元，不等同于当前 Worker API 集合 | 重构蓝图 |

## 容易混淆的概念

### Worker 与 Celery Worker

平台 Worker 是远端执行节点；Celery Worker 是消费消息任务的进程。两者都叫 Worker，但职责完全不同。

### 拓扑节点与宿主机

拓扑节点是平台创建的容器、虚机或网络设备；宿主机是运行 Worker 和真实资源的服务器。

### 当前架构与重构蓝图

当前系统由 Master Flask API、Celery 和 Worker API 协作。Control API、独立 Orchestrator、Worker Agent 是规划目标。

### KVM 虚机与平台 KVM 节点

宿主机上手工创建的维护虚机不一定是平台管理的 KVM 节点。只有进入平台拓扑、数据库和生命周期管理的实例才是平台节点。

### Redis 状态与真实资源

Redis 保存平台认知的状态；Docker、OVS、libvirt 中保存真实资源。异常时两者可能不一致。

## 命名和引用规则

- 文档首次出现缩写时给出全称。
- API 使用完整路径，例如 /master/topo/。
- 源码引用使用相对证据路径。
- 环境地址使用 <master_ip>、<worker_ip>、<port> 占位符。
- 规划概念必须标记为规划态。
- 未验证的历史术语不得写成稳定事实。

## 证据来源

- klonet_knowledge/02_vemu_uestc_code/webserver/app_factory.py
- klonet_knowledge/02_vemu_uestc_code/Service_layer/redisAPI.py
- klonet_knowledge/02_vemu_uestc_code/Service_layer/NEManager.py
- klonet_knowledge/02_vemu_uestc_code/Implement_layer/LinkManager/link_operate.py
- knowledge/staging/platform_operation_notes_lihetian_curated.md
- klonet_knowledge/05_platform_refactor_blueprint/rebuild_blueprint.md
