---
title: Klonet 常见错误导航
status: diagnostic_index
priority: P0
last_verified: 2026-06-23
---

# Klonet 常见错误导航

| 现象 | 首查层 | 案例 |
|---|---|---|
| 拓扑请求已接受但进度不再变化 | Celery、进度表、Worker 分发 | [拓扑进度卡住](cases/topology_progress_stuck.md) |
| Worker 进程存在但平台不可见 | 双向连通、注册、心跳、Redis | [Worker 注册失败](cases/worker_registration_failed.md) |
| ONOS 容器启动但控制能力不可用 | 容器、网络、端口、应用 | [ONOS 启动失败](cases/onos_startup_failed.md) |
| 虚机终端打开后立即关闭 | domain、console、libvirt stream、WebSocket | [虚机终端登录失败](cases/vm_terminal_login_failed.md) |
| KVM 节点存在但链路不通 | tap、bridge/OVS、路由、MTU | [KVM 组网异常](cases/kvm_networking_failed.md) |
| 前端按钮或页面行为找不到后端实现 | 浏览器请求、路由索引、MethodView | [前后端 API 定位](cases/frontend_backend_api_trace.md) |

## 通用原则

- “接口返回成功”只证明请求被接受，不证明异步任务或真实资源成功。
- Redis 记录和 Docker、OVS、libvirt 真实资源必须双向核对。
- 先记录请求、响应、任务 ID、日志时间点，再修改状态。
- 清库、批量杀进程、删除虚机或网络资源不是首轮排查动作。

## 证据来源

- knowledge/klonet/ops/common_troubleshooting.md
- knowledge/staging/platform_operation_notes_lihetian_curated.md
- knowledge/klonet_index/routes.jsonl
