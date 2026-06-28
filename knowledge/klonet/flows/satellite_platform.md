---
title: 卫星平台概览
domains: satellite, topology, runtime
priority: P0
status: current
quality: reviewed
sensitivity: public
last_verified: 2026-06-28
intent_tags: satellite_platform, satellite_topology, platform_overview
---

# 卫星平台概览

## 核心结论

Klonet 里的卫星平台是面向天地一体化网络实验的业务扩展模块。它不是一个独立于 Klonet 的新系统，而是在 Klonet 的拓扑、容器、链路、Worker 执行和事件调度能力之上，增加卫星节点、地面站、星间链路、星地链路和时变拓扑更新。

回答“卫星平台是什么”“接管卫星平台要了解什么”时，应先使用本文作为概览，再按需要读取源码或机器索引确认细节。

## 主要能力

- 表达卫星、地面站和相关网络节点。
- 根据卫星轨道和可见性关系生成星间、星地链路。
- 支持链路随时间变化，进行链路创建、删除和迁移。
- 结合 Docker/OVS/veth/VXLAN 等底层能力把动态链路落到 Worker 节点。
- 支持路由协议和节点网络配置，用于天地一体化网络实验。
- 通过 Celery 或后台任务持续计算、发布和执行卫星事件。

## 关键源码入口

- `vemu_uestc/Function_layer/satellite.py`：卫星业务编排和功能层入口。
- `vemu_uestc/satellite/master_evt_generate.py`：Master 侧卫星事件生成。
- `vemu_uestc/satellite/master_eventset.py`：Master 侧链路事件处理。
- `vemu_uestc/satellite/worker_eventset.py`：Worker 侧卫星事件执行。
- `vemu_uestc/satellite/satool.py`：卫星相关工具函数。
- `vemu_uestc/webserver/tasks/satellite/tasks.py`：卫星相关后台任务。
- `vemu_uestc/webserver/api/satellite/`：卫星相关 API 入口。

## 接管时优先看什么

1. 先理解普通 Klonet 拓扑创建、链路创建和 Worker 分发流程。
2. 再看卫星模块如何在普通拓扑之上增加时变事件。
3. 重点确认 Master 侧事件生成、Worker 侧事件执行和 Redis/Celery 状态流转。
4. 如果问题涉及具体函数、端口、接口或报错，必须继续用 `search_code` 和 `read_source_file` 读取真实源码确认。

## 回答边界

本文只提供稳定概览。精确接口字段、函数行为和当前版本实现，仍以 `klonet_knowledge/02_vemu_uestc_code` 中的真实源码为准。
