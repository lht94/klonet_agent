---
title: Klonet 当前架构
status: current_verified_with_planning_notes
priority: P0
domains: architecture, runtime, data, topology
last_verified: 2026-06-22
---

# Klonet 当前架构

## 适用场景

用于理解运行角色、代码层次、请求流、状态存储和当前架构风险。新增功能或排障前应先确定问题处于哪一层。

## 核心结论

当前 Klonet 是 Master-Worker 分布式控制系统。Master 同时承担 API、鉴权、初始化、编排和状态聚合；Worker 承担资源所在主机的底层执行；Celery 处理长耗时任务；Redis 与 MySQL 分别承担运行态和长期元数据。

## 运行拓扑

~~~mermaid
flowchart LR
    FE[Frontend] --> NX[Nginx]
    NX --> MA[Master Flask API]
    MA --> CE[Celery]
    MA <--> RD[(Redis)]
    MA <--> MY[(MySQL)]
    CE --> OR[拓扑编排与资源规划]
    OR --> WA[Worker API]
    WA --> SL[Service_layer]
    SL --> IL[Implement_layer]
    IL --> INF[Docker OVS tc KVM]
    MA --> WT[Web Terminal]
    MA --> DS[Data Server]
~~~

图中省略了部分监控、消息队列和卫星组件，具体部署以配置为准。

## 入口与应用工厂

Master：

- mains/master_main.py 创建 Master Flask 应用。
- 调用 webserver.app_factory.create_master_app。
- 初始化登录、上传、MySQL、运行时 bootstrap 和 Master API。

Worker：

- mains/worker_main.py 创建 Worker Flask 应用。
- 调用 create_worker_app。
- 注册拓扑、链路、资源、流量、监控、镜像、SSH、KVM 和卫星相关 API。

app_factory.py 的 register_api 当前统一允许 POST、DELETE、GET、PUT。实际支持的方法由 MethodView 类实现，调用方不能仅根据注册表推断 method。

## 代码层次

### API 与入口层

职责：

- 创建应用和扩展。
- 注册路由。
- 接收与解析请求。
- 调用任务层、功能层或服务层。

代表路径：mains/、webserver/api/、webserver/app_factory.py。

### 任务与功能编排层

职责：

- 兼容和校验拓扑 JSON。
- 创建用户数据库映射。
- 资源评估与 Worker 选择。
- 拓扑切分。
- 分发 Worker 请求。
- 管理进度和失败返回。

代表路径：webserver/tasks/topo/tasks.py、Function_layer/topo_preprocess.py、Function_layer/resource_manager.py。

### 服务层

职责：

- Redis/MySQL 数据访问。
- 节点、链路、镜像和异步任务封装。
- 领域异常和状态管理。

代表路径：Service_layer/redisAPI.py、Service_layer/NEManager.py、Service_layer/AsyncTopoManager.py。

### 实现层

职责：

- 执行 Docker、OVS、tc、ip、bridge、virsh 和 shell 操作。
- 创建或删除具体网络资源。

代表路径：Implement_layer/LinkManager/link_operate.py、Implement_layer/ContainerManager/。

## 状态与存储

### Redis

DB0 负责全局映射和 Worker 相关状态，UserMapRedis 将用户映射到独立 DB，UserDB 操作用户拓扑和节点数据。

已确认的关键概念：

- worker_list
- worker_resource
- 用户到 DB 映射
- topo2subtopo
- subtopo2worker
- 进度条表
- 节点和链路表项

键名和字段结构以 Service_layer/redisAPI.py 和配置为准。

### MySQL

当前应用工厂初始化 SQLAlchemy，模型包含用户、权限、镜像和其他长期元数据。禁止在知识文档中保留连接口令。

## 典型拓扑部署流

1. 前端请求 Master 拓扑接口。
2. Master API 发布 Celery 任务。
3. 任务兼容旧 JSON 并检查用户、拓扑和数据表。
4. 资源管理器检查资源。
5. 拓扑预处理并切分为子拓扑。
6. Redis 保存拓扑、子拓扑和 Worker 映射。
7. Master 请求各 Worker 的 /worker/topo/。
8. Worker 创建节点和链路并部署服务。
9. Master 汇总响应，更新日志和进度状态。
10. 前端轮询任务和进度接口。

任何一步都可能留下部分状态，排障时必须同时检查任务、Redis 和真实资源。

## 部署与网络边界

- Master 与 Worker 可以跨主机。
- Worker 端口必须与 Master 使用的配置一致。
- Nginx 负责公共入口和静态资源。
- Web Terminal 可能有独立端口和 WebSocket 链路。
- KVM 节点通常涉及 libvirt、qcow2、tap/bridge 和端口映射。
- 跨 Worker 链路可能涉及 VXLAN。

## 当前架构风险

- app_factory.py 路由注册集中，规模大。
- Master 职责较重，是控制面热点。
- Redis 承担大量核心运行状态，异常修复复杂。
- HTTP、Celery、线程和 shell 调用混合，错误传播不统一。
- 底层命令具有高权限，幂等和回滚要求高。
- 配置和历史文档中存在硬编码敏感信息风险。
- 部分代码和文档存在重复版本，引用时必须写明路径。

## 重构方向

规划态架构建议拆分 Control API、Orchestrator、Worker Agent、State DB 和 Message Bus，并以任务状态机管理创建、失败和回滚。

该方向用于设计讨论，不描述当前运行行为。

## 常见问题

### 为什么接口返回成功但资源不完整

可能是异步任务或 Worker 子步骤部分失败。检查 Celery 状态、各 Worker 响应、Redis 映射和真实资源。

### 为什么删除成功但宿主机还有资源

当前删除逻辑可能跳过失效 Worker，代码中也有遗留资源风险注释。需要核对 Worker 可达性和真实资源。

## 证据来源

- klonet_knowledge/02_vemu_uestc_code/mains/
- klonet_knowledge/02_vemu_uestc_code/webserver/app_factory.py
- klonet_knowledge/02_vemu_uestc_code/webserver/tasks/topo/tasks.py
- klonet_knowledge/02_vemu_uestc_code/Service_layer/redisAPI.py
- klonet_knowledge/02_vemu_uestc_code/doc/平台重构/项目框架与设计分析总结.md
- klonet_knowledge/05_platform_refactor_blueprint/rebuild_blueprint.md
