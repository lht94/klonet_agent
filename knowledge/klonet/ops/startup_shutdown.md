---
title: Klonet 启动与停止
status: current_with_environment_validation
priority: P0
domains: operations, runtime, troubleshooting
last_verified: 2026-06-22
---

# Klonet 启动与停止

## 适用场景

用于启动、验证、停止和重启当前 Master-Worker 版本的 Klonet。

## 核心结论

推荐顺序是“依赖服务 -> Master -> Celery -> Web Terminal -> Worker -> Nginx/Frontend -> 最小业务验证”。停止时反向进行，并优先优雅终止，避免跨实例 pkill。

## 启动前清单

- 确认目标配置类和实例名称。
- 核对 Master、Worker、public、Terminal、Redis 和数据库端口。
- 核对前端 config 和 Nginx。
- 检查磁盘、内存和端口占用。
- 检查旧 PID、screen 和进程。
- 检查 Redis、MySQL、RabbitMQ、Docker、OVS。
- 使用 KVM 时检查 libvirt 和初始化脚本。
- 多实例环境禁止使用模糊进程清理命令。

## 启动顺序

### 1. 依赖服务

验证 Redis、MySQL、RabbitMQ、Docker 和按需依赖。不要只检查进程，至少完成一次真实连接或健康请求。

### 2. Master

~~~bash
cd <project_parent>
screen -S <instance>_master
sudo <python_env>/gunicorn -c mains/gun.py mains.master_main:flask_app
~~~

实际导入路径取决于启动目录和安装方式，应先依据现有部署命令确认。

### 3. Celery

~~~bash
screen -S <instance>_celery
sudo <python_env>/celery -A mains.celery_worker.celery worker --loglevel=info
~~~

检查 Broker、Redis 和任务模块加载是否成功。

### 4. Web Terminal

~~~bash
screen -S <instance>_terminal
sudo <python_env>/python mains/web_terminal_main.py
~~~

确认普通 HTTP 与 WebSocket/终端端口。

### 5. Worker

~~~bash
sudo ./libvirt_config.sh
screen -S <instance>_worker
sudo <python_env>/gunicorn -c mains/worker_gun.py mains.worker_main:flask_app
~~~

不使用 KVM 时仍需核对脚本是否包含其他必要网络初始化。

### 6. Nginx 与前端

~~~bash
sudo nginx -t
sudo nginx -s reload
~~~

验证静态文件、API 前缀、上传下载和 Terminal 转发。

## 运行验证

~~~bash
screen -ls
ps aux | grep gunicorn
ps aux | grep celery
sudo lsof -i :<master_port>
sudo lsof -i :<worker_port>
curl http://127.0.0.1:<master_port>/<known_route>
curl http://127.0.0.1:<worker_port>/server_health/
~~~

再进行：

- Worker 注册检查。
- 登录接口检查。
- 最小拓扑创建。
- 进度状态检查。
- 最小拓扑删除。
- 真实资源清理检查。

## 正常停止

1. 停止接收新任务。
2. 等待或取消正在运行的拓扑任务。
3. 进入 Worker screen 并 Ctrl+C。
4. 停止 Web Terminal。
5. 停止 Celery。
6. 停止 Master。
7. 按需停止 Nginx 或依赖服务。
8. 检查端口、PID、screen 和资源。

停止共享 Redis、MySQL、RabbitMQ 或 Docker 前必须确认没有其他实例使用。

## 异常停止

先收集：

- screen -ls
- 进程完整命令行
- 端口占用
- PID 文件
- Celery 任务状态
- 当前拓扑和资源状态

仅在无法优雅停止时使用精确 kill。pkill -f gunicorn、批量退出 screen 和批量删除容器都属于危险操作。

## 重启原则

- 配置修改后重启受影响进程，不机械重启全部服务。
- systemd unit 修改后执行 daemon-reload。
- Nginx 修改先运行 nginx -t。
- Worker 重启后验证注册和镜像同步。
- Celery 重启前评估运行中的任务。
- Docker 重启后检查 OVS、Swarm 和基础容器。

## 常见问题

### 端口占用

定位完整进程命令，确认实例归属后停止。不要只根据 PID 文件判断。

### 前端可打开但无法登录

直接请求 Master API，再检查 Nginx API 转发和前端配置。

### Worker 未出现

检查 Worker 启动日志、Master 地址、端口、注册接口和 Redis 状态。

### 进度条不动

检查 Celery、任务状态、Worker 响应和进度表，不要先清库。

## 关键文件

- mains/master_main.py
- mains/worker_main.py
- mains/celery_worker.py
- mains/gun.py
- mains/worker_gun.py
- mains/web_terminal_main.py
- webserver/app_factory.py
- vemu_config/config.py

## 证据来源

- klonet_knowledge/02_vemu_uestc_code/mains/
- klonet_knowledge/02_vemu_uestc_code/doc/平台安装与部署/VEMU程序启动.md
- klonet_knowledge/06_quick_start_docs/平台重启步骤.docx
- klonet_knowledge/09_onos_startup/ONOS启动.docx
- knowledge/staging/platform_operation_notes_lihetian_curated.md
