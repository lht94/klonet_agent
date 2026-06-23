---
title: Klonet 拓扑部署流程
status: current_verified
priority: P0
domains: topology, celery, redis, worker
last_verified: 2026-06-22
---

# Klonet 拓扑部署流程

## 适用场景

用于理解拓扑创建调用链、定位部署卡住位置，以及修改拓扑预处理、资源调度、Worker 分发和进度状态。

## 核心结论

拓扑部署是异步分布式流程，不是单个 HTTP 请求内完成。Master 接收请求后发布 Celery 任务；任务兼容和校验输入、规划资源、切分拓扑、写入 Redis，再调用各 Worker 创建本地子拓扑和服务。

## 关键入口

- Master 路由：/master/topo/
- Worker 拓扑路由：/worker/topo/
- Worker 服务路由：/worker/service/
- Master 任务：master_deploy_topo
- Worker 执行：Worker TopoDeployAPI/TopoServiceAPI
- 任务文件：webserver/tasks/topo/tasks.py

API 路径与 method 必须以 app_factory.py 和 MethodView 实现为准。

## 部署阶段

### 1. 请求接收与任务发布

Master API 接收用户、拓扑和 networks 等数据，调用 deploy_topo，将 master_deploy_topo 发布到 Celery。

接口快速返回不代表资源已经创建完成。

### 2. 兼容处理

compat_json 为旧版本拓扑补充字段，例如 service 和接口名称后缀。兼容逻辑说明当前前端/拓扑 JSON 存在版本差异。

修改拓扑 schema 时必须同步评估：

- 老前端输入。
- vemu_api 脚本。
- 数据库存储。
- Worker API。
- 动态修改功能。

### 3. 用户与拓扑检查

任务通过 UserMapRedis/UserDB 获取用户 DB，检查目标拓扑是否已存在，并初始化相关运行状态。

同名拓扑、残留表或错误用户 DB 映射会导致早期失败。

### 4. 卫星与特殊拓扑处理

卫星拓扑可能先生成或转换网络描述，并在部署完成后发布时变事件任务。普通拓扑排障时也要确认是否误进入卫星分支。

### 5. 资源评估与切分

ResourceManager 检查资源并选择 Worker；Topo_process 对拓扑预处理和切分。

关键输出：

- topo2subtopo
- subtopo2worker
- 子拓扑数据
- Worker 资源分配
- 进度表

资源检查、切分和数据写入不是独立事实，必须保持一致。

### 6. Worker 拓扑创建

Master 根据 subtopo2worker 生成 Worker 请求：

~~~text
POST http://<worker_ip>:<worker_port>/worker/topo/
~~~

每个 Worker 只处理分配给自己的子拓扑。跨主机链路可能需要 VXLAN 或其他远端信息。

### 7. Worker 服务创建

节点和链路创建成功后，Master 请求 Worker 的 /worker/service/ 部署节点服务和链路相关服务。

拓扑存在不代表服务全部启动，排障需分开检查资源阶段和服务阶段。

### 8. 汇总与完成

Master 汇总 Worker 响应：

- 任一必要步骤失败时返回失败信息。
- 成功后写用户日志。
- 卫星拓扑发布后续任务。
- 延迟清理进度表。
- 返回拓扑创建成功。

## 数据变化

部署过程中可能创建或修改：

- 用户拓扑总表。
- 节点和链路表。
- topo2subtopo。
- subtopo2worker。
- worker_resource。
- 进度表。
- 监控、流量和日志相关表。
- KVM/镜像相关元数据。

字段以 redisAPI.py 和当前任务代码为准。

## 进度条

进度条是 Redis 中的阶段状态，不是 Celery 任务本身。进度不动可能意味着：

- Celery 未收到任务。
- 任务卡在资源评估。
- Worker 请求未返回。
- Worker 创建底层资源阻塞。
- 进度表键不一致。
- 前端轮询错误任务或拓扑。
- 进度表被提前清理。

## 失败排查

### 任务未开始

检查 Master 返回、Broker、Celery 进程和任务注册。

### 资源评估失败

检查 Worker 注册、worker_resource、宿主机真实资源和 ResourceManager 日志。

### 切分失败

检查 topology JSON、Topo_process、硬件节点和 split_option。

### Worker 请求失败

检查 subtopo2worker、网络、Worker 端口、健康接口和 Worker 日志。

### 节点成功但链路失败

检查节点 ID、namespace、Veth/VXLAN/OVS、网卡命名和残留资源。

### 服务阶段失败

检查 /worker/service/、镜像、节点状态和具体服务启动日志。

## 幂等与恢复注意

当前流程可能产生部分成功状态。重试前：

1. 确认 Celery 原任务是否仍运行。
2. 对比 Redis 与真实资源。
3. 确认同名拓扑是否存在。
4. 识别已创建子拓扑。
5. 只清理已确认的遗留资源。
6. 再发起部署。

禁止通过直接删除全部 Redis 状态来伪造干净环境。

## 删除流程概览

master_delete_topo：

1. 获取拓扑和子拓扑映射。
2. 请求各 Worker DELETE /worker/topo/。
3. 归还资源。
4. 删除监控、流量和日志。
5. 清理拓扑数据库表。
6. 更新并清理删除进度。

代码中存在失效 Worker 资源可能无法清理的风险，删除成功后仍需核对宿主机。

## 关键文件

- webserver/app_factory.py
- webserver/api/topo/master_topo.py
- webserver/api/topo/worker_topo.py
- webserver/tasks/topo/tasks.py
- Function_layer/topo_preprocess.py
- Function_layer/resource_manager.py
- Service_layer/redisAPI.py
- Service_layer/NEManager.py
- Implement_layer/LinkManager/link_operate.py

## 常见问题

### 为什么前端看到成功但 Worker 没资源

区分 API 接受成功、Celery 任务成功和 Worker 资源成功。核对任务结果和真实资源。

### 为什么删除返回成功但还有容器

Worker 失效、部分响应和清理分支都可能留下资源。检查子拓扑映射和目标 Worker。

## 证据来源

- klonet_knowledge/02_vemu_uestc_code/webserver/app_factory.py
- klonet_knowledge/02_vemu_uestc_code/webserver/tasks/topo/tasks.py
- klonet_knowledge/02_vemu_uestc_code/Function_layer/topo_preprocess.py
- klonet_knowledge/02_vemu_uestc_code/Function_layer/resource_manager.py
- klonet_knowledge/02_vemu_uestc_code/Service_layer/redisAPI.py
- knowledge/staging/platform_operation_notes_lihetian_curated.md
