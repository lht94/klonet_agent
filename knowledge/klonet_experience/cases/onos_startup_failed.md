---
title: ONOS 启动或控制能力不可用
status: diagnostic_playbook
priority: P0
domains: onos, sdn, networking
last_verified: 2026-06-23
---

# ONOS 启动或控制能力不可用

## 现象

ONOS 容器未运行，或容器运行但 CLI、Web UI、OpenFlow 控制和拓扑连接不可用。

## 环境

ONOS 可能依赖 Docker Swarm 的 attachable Overlay 网络、端口映射和应用启用。镜像、端口、网络名与应用集合必须以当前部署配置为准。

## 排查路径

1. 检查容器状态、重启次数和启动日志。
2. 检查容器是否加入预期 Overlay 网络，以及网络是否 attachable。
3. 核对当前端口映射，不照抄历史地址或端口。
4. 分别验证 CLI/Web UI 与控制平面端口。
5. 检查所需 ONOS 应用是否已激活。
6. 从被控 OVS/节点侧验证控制器地址和连接状态。

## 根因候选与确认标准

- 容器启动失败：容器退出且日志给出镜像、配置或资源错误。
- 网络缺失：容器正常，但不在预期 Overlay 网络或节点不可达。
- 端口映射不一致：容器内服务正常，宿主机入口失败。
- 应用未启用：管理入口可用，但相应控制功能或协议不可用。

## 解决方案

按“容器 -> 网络 -> 端口 -> 应用 -> 被控节点”顺序修复并逐层验证。文档中的默认凭据不得写入知识库，应从安全配置获得。

## 相关源码

- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/Service_layer/NEManager.py

## 相关文档

- klonet_knowledge/09_onos_startup/ONOS启动.docx
- knowledge/staging/platform_operation_notes_lihetian_curated.md

## 可复用结论

容器 Running 只证明进程层存活；ONOS 可用性至少还包括网络、入口、应用和设备连接四层。
