---
title: 拓扑部署进度卡住
status: diagnostic_playbook
priority: P0
domains: topology, celery, redis, worker
intent_tags: topology_deploy
last_verified: 2026-06-23
---

# 拓扑部署进度卡住

## 现象

Master 接受拓扑部署请求，但前端进度长期不变，或进度与实际容器、OVS、KVM 资源不一致。

## 环境

适用于当前 Master API -> Celery `master_deploy_topo` -> Redis 进度表 -> Worker `/worker/topo/` 的异步链路。

## 排查路径

1. 保存部署请求、响应、任务 ID、用户与拓扑标识。
2. 确认 Celery 已注册并开始执行 `master_deploy_topo`。
3. 按日志时间线定位预处理、资源评估、切分、`subtopo2worker` 和 Worker 分发的最后成功阶段。
4. 查询进度表，确认键与前端轮询参数一致。
5. 对每个目标 Worker 验证 `/server_health/` 和 `/worker/topo/`。
6. 将 Redis 状态与 Docker、OVS、libvirt 实际资源对照。

## 根因候选与确认标准

- Celery 未消费：任务无开始记录，Worker 日志无对应任务。
- Worker 不可达：Master 请求失败，目标 Worker 本地健康检查正常或异常可被独立复现。
- 进度键不一致：后端更新的键与前端查询键不同。
- 底层创建阻塞：任务已进入 Worker，最后日志停在具体 Docker、OVS 或 KVM 操作。
- 状态提前清理：进度曾更新，随后在任务结束前消失。

## 解决方案

只修复已确认的故障层；重试前清点残留资源。禁止把删除进度表或清空 Redis 作为首要修复。完成后验证最小拓扑创建、进度完成、资源可用和拓扑删除。

## 相关源码

- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/webserver/tasks/topo/tasks.py
- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/Function_layer/deploy_process_bar.py
- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/webserver/api/topo/worker_topo.py
- knowledge/klonet_index/celery_tasks.jsonl

## 相关文档

- knowledge/klonet/flows/topology_deploy.md
- knowledge/klonet/ops/common_troubleshooting.md

## 可复用结论

用“最后成功阶段 + 首个失败边界”描述问题，比“进度卡住”更适合 Agent 继续检索和执行。
