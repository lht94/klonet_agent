---
title: Worker 注册失败
status: diagnostic_playbook
priority: P0
domains: runtime, worker, redis, monitor
last_verified: 2026-06-23
---

# Worker 注册失败

## 现象

Worker 进程和监听端口存在，但 Master 或前端看不到 Worker；或注册后很快被判定失效。

## 环境

当前注册路由为 `POST /master/worker/<worker_ip>/`，健康路由为 `GET /server_health/`，心跳路由为 `POST /master/heartbeat/`。

## 排查路径

1. 核对 Worker 启动日志、实际监听地址和端口。
2. 从 Worker 主机访问 Master 注册地址。
3. 从 Master 主机访问 Worker `/server_health/`。
4. 对照 Master 与 Worker 配置中的地址和端口，避免注册地址、NAT 地址和监听地址混用。
5. 检查注册请求响应、Master 日志、`worker_list` 和心跳表。
6. 检查旧地址残留及同一 Worker 的重复记录。

## 根因候选与确认标准

- 单向网络或防火墙：一个方向连接成功、反向健康检查失败。
- 地址发布错误：注册值不是 Master 可达的 Worker 地址。
- 注册成功但心跳失败：`worker_list` 短暂出现，随后被健康守护逻辑移除。
- Redis 状态残留：同一实例存在旧地址或资源记录，且与当前配置不一致。

## 解决方案

修正可达地址或端口后重启受影响 Worker，并观察注册与至少一个心跳周期。仅删除已经确认过期的记录；之后执行健康检查和最小拓扑调度验证。

## 相关源码

- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/webserver/api/worker_register/worker_register.py
- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/webserver/api/health_check/heartbeat.py
- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/Function_layer/server_health_master.py
- knowledge/klonet_index/routes.jsonl

## 相关文档

- knowledge/klonet/ops/startup_shutdown.md
- knowledge/klonet/ops/common_troubleshooting.md

## 可复用结论

“进程存在”不等于“可调度”；注册、反向健康检查、心跳和 Redis 状态必须形成闭环。
