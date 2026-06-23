---
title: Klonet 运维案例索引
status: diagnostic_index
priority: P0
last_verified: 2026-06-23
---

# Klonet 运维案例索引

| 案例 | 适用范围 | 预期产出 |
|---|---|---|
| [拓扑进度卡住](cases/topology_progress_stuck.md) | Master、Celery、Redis、Worker | 最后成功阶段和首个失败边界 |
| [Worker 注册失败](cases/worker_registration_failed.md) | Master-Worker 控制面 | 双向连通、注册与心跳状态 |
| [ONOS 启动失败](cases/onos_startup_failed.md) | SDN 控制器节点 | 容器、Overlay、端口、应用检查表 |
| [虚机终端登录失败](cases/vm_terminal_login_failed.md) | KVM console/WebSocket | 终端链路的故障层 |
| [KVM 组网异常](cases/kvm_networking_failed.md) | KVM-KVM、KVM-Docker | 接口、桥接、路由与 MTU 差异清单 |

这些条目是诊断手册，不替代当前环境配置。所有命令中的地址、端口和实例名均应从部署配置读取。
