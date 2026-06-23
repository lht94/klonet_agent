---
title: 前端到后端 API 定位
status: diagnostic_playbook
priority: P0
domains: frontend, api, development
last_verified: 2026-06-23
---

# 前端到后端 API 定位

## 现象

前端按钮无响应、请求报错，或开发者无法确认某个界面操作对应的 Master/Worker 实现。

## 环境

项目存在多个前端版本和历史接口。后端由 `app_factory.py` 集中注册路由，真实 method 与行为由 MethodView 类实现；部分 Master API 还会继续请求 Worker API。

## 排查路径

1. 在浏览器 Network 保存完整 URL、method、query、JSON body、响应码和响应体。
2. 在目标前端版本中按 URL、调用函数和业务字段反查发起位置。
3. 在 `knowledge/klonet_index/routes.jsonl` 按 route 查找 `implementation`、`view_class` 和 `methods`。
4. 阅读规范源码树 `vemu_uestc/...` 中对应 MethodView 方法。
5. 若实现调用 Worker，继续追踪目标 Worker route、请求参数和响应处理。
6. 对异步接口继续检查 Celery 任务、Redis 状态和前端轮询。

## 根因候选与确认标准

- 前端配置错误：实际请求发往错误 base URL、端口或 API 前缀。
- method/payload 不匹配：浏览器请求与 MethodView 实现要求不同。
- 旧前端与新后端不兼容：字段或路径只在某一版本存在。
- Nginx 转发错误：直连 Master 成功，经代理失败。
- Master 到 Worker 失败：Master 收到请求，但二次调用失败。

## 解决方案

以实际请求和路由实现为准修正调用；若改变后端路由，重新生成机器索引并补充兼容策略。验证页面操作时同时观察浏览器、Master 和 Worker 三侧日志。

## 相关源码

- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/webserver/app_factory.py
- klonet_knowledge/02_vemu_uestc_code/vemu_uestc/webserver/api/
- knowledge/klonet_index/routes.jsonl

## 相关文档

- knowledge/klonet/dev/backend_api_development.md
- knowledge/staging/platform_operation_notes_lihetian_curated.md

## 可复用结论

最可靠的定位键是“实际 URL + method”，而不是按钮文字或旧文档中的接口名称。
