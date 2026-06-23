---
title: Klonet 开发案例索引
status: diagnostic_index
priority: P0
last_verified: 2026-06-23
---

# Klonet 开发案例索引

## 前后端 API 定位

使用 [前后端 API 定位](cases/frontend_backend_api_trace.md) 从浏览器实际请求反查 `knowledge/klonet_index/routes.jsonl`，再进入规范源码树中的 MethodView 实现。不要仅凭按钮文本、旧接口文档或 `register_api` 允许的方法集合判断真实语义。

## 变更后的最小验证

1. 浏览器 Network 中请求 URL、method、payload 与预期一致。
2. 路由索引能唯一定位到 `vemu_uestc/webserver/...` 实现。
3. Master 到 Worker 的二次请求有对应日志。
4. 异步操作同时核对 Celery 状态、Redis 进度和真实资源。
5. 新增路由后重新生成 `knowledge/klonet_index/routes.jsonl`。
