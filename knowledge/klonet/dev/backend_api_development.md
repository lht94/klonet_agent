---
title: Klonet 后端 API 开发
status: current_verified
priority: P0
domains: development, api, master, worker
last_verified: 2026-06-22
---

# Klonet 后端 API 开发

## 适用场景

用于新增或修改当前 Flask MethodView API，并保持 Master-Worker、数据访问和错误处理方式一致。

## 核心结论

先确认 API 属于 Master 还是 Worker。Master 负责对外业务语义、全局状态和调度；Worker 负责资源所在主机的底层执行。新增接口应优先复用现有服务层，不在视图中堆积 shell 和数据库细节。

## 当前注册方式

webserver/app_factory.py 中的 register_api 将 MethodView 注册到 Flask。注册层统一声明 POST、DELETE、GET、PUT，但类中实际实现的方法才真正可用。

因此文档、前端和测试必须以 MethodView 实现为准。

## 开发步骤

1. 确定业务域和 Master/Worker 边界。
2. 查找同域现有 API。
3. 明确输入、输出、状态变化和失败语义。
4. 在 webserver/api/<domain>/ 实现或扩展 MethodView。
5. 在 app_factory.py 中导入并注册。
6. 将复杂业务放入 Function_layer 或 Service_layer。
7. 通过 redisAPI/mysql API 访问数据。
8. 底层系统操作放在 Implement_layer。
9. 编写接口测试和失败路径测试。
10. 重启受影响服务并验证前后端协作。

## Master 与 Worker 的选择

使用 Master API：

- 前端直接调用。
- 需要用户、拓扑或全局状态。
- 需要选择 Worker。
- 需要异步任务和结果汇总。

使用 Worker API：

- 操作特定宿主机资源。
- 创建节点、链路、容器、虚机或网络。
- 查询本机状态。
- 由 Master 调用而非前端直接暴露。

复杂功能可能同时需要一对 Master/Worker API。

## 请求参数

优先使用项目已有 schema 或校验工具。不要直接假定 JSON 字段存在。

~~~python
data = request.get_json(silent=True) or {}
user = data.get("user")
topo = data.get("topo")
if not user or not topo:
    return {"code": 0, "msg": "missing required fields"}, 400
~~~

示例仅表达原则，正式实现应匹配项目当前响应约定。

## 响应与错误

当前代码广泛使用 code/msg JSON。新增接口应：

- 保持同域接口一致。
- 区分参数错误、资源不存在、Worker 不可达和内部错误。
- 不向用户返回敏感 traceback。
- 在日志中保留请求、任务、用户和拓扑上下文。
- 对跨 Worker 结果明确部分成功语义。

## 数据访问

Redis：

- UserMapRedis 负责用户 DB 映射。
- UserDB 负责用户拓扑和节点数据。
- WorkerRedis 等类负责全局 Worker 状态。

不要在 API 里直接拼接未经确认的 Redis key。先查 Service_layer/redisAPI.py。

MySQL：

- 使用现有模型和 API。
- 避免在请求中重复初始化连接。
- 不在代码中新增明文凭据。

## 异步任务

长耗时操作使用 Celery，接口返回任务标识或可查询状态。任务需要：

- 明确输入快照。
- 记录阶段。
- 超时和异常处理。
- 幂等或补偿策略。
- 进度状态清理。
- Worker 失败处理。

## 路由与方法核对

开发前检查：

~~~bash
rg "register_api.*<domain>" webserver/app_factory.py
rg "class .*API" webserver/api/<domain>
rg "def (get|post|put|delete)" webserver/api/<domain>
~~~

机器索引层完成后应优先查询 routes.jsonl 和 symbols.jsonl。

## 测试

至少覆盖：

- 正常请求。
- 缺少字段。
- 非法类型或值。
- 用户/拓扑不存在。
- Worker 不可达。
- Redis/MySQL 异常。
- 部分 Worker 失败。
- 重复请求。
- 权限不足。
- 前端实际 payload。

## 常见问题

### 接口注册了但 405

register_api 的 methods 不代表 MethodView 已实现对应方法。检查类方法。

### 修改代码没有生效

确认重启的是正确实例和进程，检查启动目录、Python 环境、Gunicorn worker 和重复代码副本。

### Master 与 Worker 数据不一致

检查请求使用的用户、拓扑、子拓扑、Worker 地址和 Redis DB。

## 关键文件

- webserver/app_factory.py
- webserver/api/
- webserver/tasks/
- Function_layer/
- Service_layer/redisAPI.py
- Service_layer/mysql_models.py
- Implement_layer/

## 证据来源

- klonet_knowledge/02_vemu_uestc_code/webserver/app_factory.py
- klonet_knowledge/02_vemu_uestc_code/webserver/api/
- klonet_knowledge/02_vemu_uestc_code/webserver/tasks/
- knowledge/staging/platform_operation_notes_lihetian_curated.md
- klonet_knowledge/02_vemu_uestc_code/doc/开发流程与规范/
