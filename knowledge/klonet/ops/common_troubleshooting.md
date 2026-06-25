---
title: Klonet 常见排障
status: current_experience_with_source_validation
priority: P0
domains: operations, troubleshooting, topology, vm
last_verified: 2026-06-24
---

# Klonet 常见排障

## 适用场景

用于平台不可访问、服务启动失败、Worker 注册失败、拓扑卡住、资源遗留和虚机终端异常等问题。

## 核心结论

排障顺序应沿调用链进行：

~~~text
用户现象
-> Frontend/Nginx
-> Master API
-> Celery
-> Redis/MySQL
-> Worker API
-> Docker/OVS/KVM 真实资源
~~~

先收集证据，再执行修复。Redis 状态和真实资源可能不一致，不能只看一侧。

## 通用信息收集

- 发生时间、用户、拓扑和操作。
- Master、Celery、Worker、Terminal 日志。
- 请求 URL、method、payload 和响应。
- screen、进程、PID 和端口。
- Redis 目标 DB 与关键表。
- Docker、OVS、libvirt 真实资源。
- 磁盘、内存和 CPU。
- 最近配置、代码或环境变更。

敏感数据在记录中脱敏。

第一轮先执行只读命令，形成同一时间点的状态快照：

~~~bash
date
screen -ls
ps aux | grep '[g]unicorn'
ps aux | grep '[c]elery'
ps aux | grep '[r]edis'
sudo ss -lntp
sudo docker ps -a
sudo ovs-vsctl show
sudo virsh list --all
df -h
free -h
~~~

不使用 KVM 的服务器可以跳过 `virsh`；不使用 OVS 的场景可以跳过 `ovs-vsctl`。命令失败本身也是证据，应连同完整错误输出保留。

服务启动、停止和 screen 操作的完整命令见 [Klonet 启动、停止与重启](startup_shutdown.md)。

## 前端可访问但无法登录

1. 浏览器 Network 查看实际请求。
2. 确认前端 base URL 和 API 前缀。
3. 绕过 Nginx 直接请求 Master。
4. 检查 Nginx location 与 proxy_pass。
5. 检查 Master 和数据库。
6. 检查 Cookie、会话和跨域。

如果静态页面正常，问题通常不在前端文件服务本身。

## Master 启动失败

常见原因：

- 配置类导入错误。
- MySQL/Redis/RabbitMQ 不可达。
- 端口占用。
- Python 环境或依赖不兼容。
- 上传目录或镜像目录权限错误。
- bootstrap 初始化异常。
- 磁盘空间不足。

先阅读完整 traceback，不使用“缺什么就全局安装什么”替代根因分析。用以下命令确认运行目录、解释器和端口，再对照 [启动 Runbook](startup_shutdown.md) 中的两套 Python 路径：

~~~bash
pwd
command -v python3
command -v gunicorn
sudo ss -lntp
~~~


## Worker 注册失败

1. 确认 Worker 进程和端口。
2. 从 Worker 请求 Master。
3. 从 Master 请求 Worker 健康接口。
4. 核对 Master/Worker 配置。
5. 检查防火墙和路由。
6. 检查 Worker 注册、心跳和 Redis worker_list。
7. 检查同一 Worker 是否以旧地址残留。

## 拓扑部署进度卡住

检查：

- Master 接口是否成功发布任务。
- Celery 是否收到并启动任务。
- Redis 进度表是否更新。
- 资源评估和拓扑切分是否完成。
- subtopo2worker 映射。
- Worker /worker/topo/ 响应。
- Docker、OVS、KVM 创建日志。
- 前端轮询是否请求了正确任务。

禁止把“删除进度表”作为第一处理动作。

## 拓扑删除后仍有资源

当前删除任务可能因 Worker 失效而跳过远端清理。核对：

- topo2subtopo 和 subtopo2worker。
- Worker 是否可达。
- Docker 容器、OVS bridge/port、VXLAN、libvirt domain。
- Redis 拓扑和资源表。
- 监控、流量和日志清理结果。

先输出差异清单，再执行定向清理。

## 资源不足或 worker_resource 异常

- 对比平台记录与宿主机真实 CPU/内存/容器。
- 检查异常拓扑销毁和 Worker 重启。
- 确认是否有运行任务。
- 备份 Redis key。
- 只修复确认错误的字段。
- 修复后创建并删除最小拓扑验证。

直接删除整个 worker_resource 属于危险恢复手段。

## Web Terminal/SSH 异常

1. 检查目标容器或虚机运行状态。
2. 检查 Terminal 服务和 Worker API。
3. 检查 WebSocket、SSH 和端口映射。
4. KVM 场景检查 libvirt 和 console 占用。
5. 检查浏览器代理链路。
6. 从宿主机本地连接目标，区分平台问题与目标节点问题。

## 虚机无法联网

- 检查 libvirt 网络和网关。
- 检查虚机网卡名称和地址。
- 检查默认路由和 DNS。
- 检查宿主机转发、防火墙和 NAT。
- 多网卡时避免错误默认路由。
- 以当前网络配置为准，不复制历史网段。

## 前端按钮无响应

- 硬刷新并确认脚本加载。
- 检查 Console 错误。
- 检查事件绑定。
- 检查 Network 请求。
- 检查前端镜像列表、属性列表与后端数据数量。
- 检查 Redis 中前端预期字段。

## 危险操作清单

以下操作必须有确认、备份和回滚：

- pkill -f。
- 批量结束 screen。
- 批量删除容器或 OVS。
- virsh destroy/undefine。
- 修改 qcow2 和分区。
- 删除 Redis 资源记录。
- 重启 Docker。
- Git reset --hard 和远端强制推送。

## Case 记录模板

每次确认根因后记录：

- 现象。
- 环境。
- 排查路径。
- 根因。
- 解决方案。
- 相关源码。
- 原始文档。
- 可复用结论。

## 证据来源

- knowledge/staging/platform_operation_notes_lihetian_curated.md
- klonet_knowledge/04_platform_teaching_ops/平台运维内容.docx
- klonet_knowledge/04_platform_teaching_ops/虚仿平台运维手册.docx
- klonet_knowledge/08_vm_terminal_docs/
- klonet_knowledge/02_vemu_uestc_code/Service_layer/deploy_error.py
- klonet_knowledge/02_vemu_uestc_code/webserver/tasks/topo/tasks.py
