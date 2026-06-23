---
title: Klonet 项目总览
status: current_verified
priority: P0
domains: project, architecture, operations, development
last_verified: 2026-06-22
---

# Klonet 项目总览

## 适用场景

本文帮助新成员和 Agent 快速回答：

- Klonet 是什么。
- 平台由哪些运行角色组成。
- 一次拓扑部署大致经过哪些组件。
- 当前系统与重构蓝图有什么区别。
- 遇到具体问题应继续查哪类知识。

## 核心结论

Klonet 是面向网络虚拟化、网络仿真和教学实验的分布式平台。当前实现以 Master 为控制与编排入口，以 Worker 为资源执行节点，通过 Flask API、Celery、Redis、MySQL 和底层 Docker/OVS/tc/KVM 能力完成拓扑创建、链路配置、流量、监控、镜像、虚机和卫星实验等功能。

当前代码是一个具体历史版本，不代表永久最新状态。回答实现问题时必须引用源码路径和验证日期。

## 核心能力

- 创建、删除和查询网络拓扑。
- 将拓扑切分并分发到一个或多个 Worker。
- 创建容器、OVS、链路、VXLAN 和 KVM 虚机节点。
- 配置静态链路、时延、丢包和毫米波链路。
- 下发节点服务和批量命令。
- 生成流量并查询流量状态。
- 部署监控、采集实验数据和展示结果。
- 管理容器镜像、实验镜像和 KVM 镜像。
- 提供 SSH、Web Terminal 和端口映射能力。
- 支持卫星拓扑和天地一体化实验扩展。

## 运行角色

| 角色 | 当前职责 | 关键入口 |
| --- | --- | --- |
| Frontend | 用户操作、拓扑编辑、状态展示、调用 Master API | 前端版本目录及 scripts/config.js |
| Nginx | 静态资源和 API 反向代理 | 部署环境中的 Nginx 配置 |
| Master | API、鉴权、全局状态、资源编排、Worker 调度 | mains/master_main.py |
| Celery Worker | 执行拓扑等长耗时异步任务 | mains/celery_worker.py |
| Worker | 在本机执行节点、链路、流量、监控和虚机操作 | mains/worker_main.py |
| Web Terminal | 提供终端相关服务 | mains/web_terminal_main.py |
| Data Server | 实验数据处理和图表服务 | mains/data_server_main.py |
| Redis | 运行态、用户 DB 映射、拓扑和任务状态 | Service_layer/redisAPI.py |
| MySQL | 用户、权限、镜像和长期元数据 | Service_layer/mysql_models.py |

## 核心数据流

~~~text
Frontend
-> Nginx
-> Master API
-> Celery / Function_layer
-> Redis 状态与资源规划
-> Worker API
-> Service_layer
-> Implement_layer
-> Docker / OVS / tc / KVM
~~~

MySQL 保存用户、权限和部分长期元数据；Redis 主要承担运行态和任务状态。具体表项必须结合当前代码确认。

## 代码分层

| 层 | 职责 | 代表目录 |
| --- | --- | --- |
| 入口与 API | 创建 Flask 应用、注册路由、接收请求 | mains、webserver |
| 任务与编排 | 长任务、拓扑预处理、切分和资源调度 | webserver/tasks、Function_layer |
| 服务封装 | 数据访问、资源、节点、链路和镜像服务 | Service_layer |
| 底层执行 | Docker、OVS、tc、shell 等系统操作 | Implement_layer |
| 配置与基础设施 | 地址、端口、功能开关和存储配置 | vemu_config |

分层是理解代码的导航，不代表依赖边界完全严格。现有项目仍存在跨层调用和集中注册问题。

## 当前实现与重构蓝图

当前事实：

- Master 和 Worker 通过 Flask API 协作。
- app_factory.py 集中注册大量路由。
- Celery 承担长耗时任务。
- Redis 保存大量运行态和部分核心状态。
- Worker 暴露较多底层执行 API。

重构规划：

- Control API 尽量无状态。
- Orchestrator/Scheduler 独立承担编排。
- Worker Agent 接收意图并执行本地动作。
- 关系型数据库保存系统事实。
- Message Bus 传递命令和事件。

重构蓝图尚未落地，不能用规划行为解释当前故障。

## 关键文件

- klonet_knowledge/02_vemu_uestc_code/mains/master_main.py
- klonet_knowledge/02_vemu_uestc_code/mains/worker_main.py
- klonet_knowledge/02_vemu_uestc_code/mains/celery_worker.py
- klonet_knowledge/02_vemu_uestc_code/webserver/app_factory.py
- klonet_knowledge/02_vemu_uestc_code/webserver/tasks/topo/tasks.py
- klonet_knowledge/02_vemu_uestc_code/Service_layer/redisAPI.py
- klonet_knowledge/02_vemu_uestc_code/Service_layer/NEManager.py
- klonet_knowledge/02_vemu_uestc_code/Implement_layer/LinkManager/link_operate.py

## 常见问题

### Klonet 只是容器仿真平台吗

不是。平台以 Docker 容器为重要执行对象，同时已经扩展 KVM 虚机、Web Terminal、SSH、卫星实验和多类网络能力。

### Master 是否直接创建所有资源

通常不是。Master 负责校验、规划、切分和调度，Worker 在资源所在主机执行实际操作。

### 文档和代码冲突时信谁

当前行为以目标部署版本的源码和运行日志为准。文档用于解释和经验导航，旧文档和个人记录必须标注时效性。

## 证据来源

- klonet_knowledge/01_ai_materials/
- klonet_knowledge/02_vemu_uestc_code/
- knowledge/staging/platform_operation_notes_lihetian_curated.md
- klonet_knowledge/05_platform_refactor_blueprint/rebuild_blueprint.md
- klonet_knowledge/06_quick_start_docs/
