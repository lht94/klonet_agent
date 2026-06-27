---
title: Klonet 启动、停止与重启
status: current_runbook
priority: P0
domains: operations, runtime, troubleshooting
intent_tags: platform_start, platform_stop, platform_restart
last_verified: 2026-06-24
---

# Klonet 启动、停止与重启

## 适用场景

用于已经完成环境部署的 Klonet 服务器。日常启动不需要重新安装所有依赖；通常先确认配置，再检查因断电或重启而停止的 Redis，然后启动后端 screen、Nginx 和前端。

本文使用以下占位符：

- `<project_root>`：同时包含 `mains/` 与 `vemu_uestc/` 的项目目录。
- `<instance>`：当前平台实例的简短名称，用于区分 screen。
- `<master_ip>`：浏览器和 Worker 可以访问的 Master 宿主机地址。
- `<master_port>`、`<worker_port>`、`<public_port>`、`<terminal_port>`：当前实例配置的端口。
- `<frontend_path>`：前端目录的绝对路径，Nginx alias 中末尾必须保留 `/`。

## 服务器角色与启动顺序

Master 服务器通常启动：

1. Klonet Redis。
2. Master Gunicorn。
3. Celery。
4. Web Terminal。
5. 本机也承担 Worker 时，再启动 Worker Gunicorn。
6. Nginx 与前端。

扩展 Worker 服务器通常只启动 Redis 检查所需服务和 Worker Gunicorn；Celery 一般只在 Master 侧启动一次。所有 Worker 使用的后端配置必须与 Master 的地址和约定端口保持一致。

## 第一步：修改并核对后端配置

编辑当前实际使用的配置类：

~~~bash
cd <project_root>
sudo vim vemu_uestc/vemu_config/config.py
~~~

至少核对：

- `master_ip`：Master 宿主机对 Worker 可达的地址。
- `master_port` 与 `worker_port`：Master、Worker 和 Nginx 使用同一组约定。
- `public_port`：Nginx 对外监听端口。
- `web_terminal_port`：前端连接 Web Terminal 的端口。
- Redis、MySQL、RabbitMQ 和镜像仓库地址是否属于当前实例。
- 文件末尾的 `PROJ_CONFIG` 是否实例化了刚刚修改的配置类。

当前版本的 Web Terminal 监听端口还可能直接写在 `mains/web_terminal_main.py` 中。启动前确认它与 `web_terminal_port` 一致：

~~~bash
grep -n "WSGIServer|web_terminal_port"   <project_root>/mains/web_terminal_main.py   <project_root>/vemu_uestc/vemu_config/config.py
~~~

多实例服务器必须使用各自独立端口和 Redis 数据范围，不能只修改 `master_ip`。

## 第二步：检查并启动 Redis

机器断电或重启后，Klonet 运行态 Redis 可能没有自动恢复。先检查，不要无条件重复启动：

~~~bash
ps aux | grep '[r]edis'
~~~

如果当前服务器使用统一环境脚本：

~~~bash
cd /root/vemu_install_new_gen
sudo bash ./service_begin_both/begin_redis.sh
ps aux | grep '[r]edis'
~~~

部分服务器或服务器内虚拟机使用独立 `redis.conf`：

~~~bash
cd /root/vemu_install_new_gen/install_redis
sudo /usr/local/bin/redis-server redis.conf &
ps aux | grep '[r]edis'
~~~

另有历史环境把脚本放在用户目录，例如 `<environment_tools>/service_begin_both/begin_redis.sh`。应使用该服务器现有的已验证路径，不要复制其他服务器的绝对路径。

只有后端出现 Broker、MySQL 或基础容器连接错误时，才继续检查：

~~~bash
sudo docker ps -a
sudo docker logs --tail 100 <container_name>
~~~

不要在每次正常启动时重建 MySQL、RabbitMQ、Celery Redis 或镜像仓库容器。

## 第三步：进入正确运行目录

以下后端命令均从 `mains` 目录执行：

~~~bash
cd <project_root>/mains
pwd
test -f gun.py
test -f master_main.py
test -f celery_worker.py
test -f worker_gun.py
test -f worker_main.py
~~~

常见运行时有两种：

| 环境 | Gunicorn/Celery 目录 | Python 示例 |
| --- | --- | --- |
| 服务器 | `/usr/local/bin/` | `/usr/local/bin/python3.8` |
| 服务器内虚拟机 | `/usr/local/python3/bin/` | `/usr/local/python3/bin/python3.8` |

先确认目标文件存在：

~~~bash
ls -l /usr/local/bin/gunicorn /usr/local/bin/celery
ls -l /usr/local/python3/bin/gunicorn /usr/local/python3/bin/celery
~~~

这些“服务器路径/服务器内虚拟机路径”标签只是历史环境中的常见变体，不是判断当前机器类型的依据。不同批次服务器、宿主机或虚拟机可能互换路径；最终以当前目标机器上 `command -v gunicorn`、`command -v celery`、`command -v python3.8` 或 `ls -l` 的结果为准。只执行当前机器实际存在且属于同一套 Python 环境的一套命令，不要混用 `/usr/local/bin/` 和 `/usr/local/python3/bin/`。

## 第四步：启动 Master

### 服务器路径

~~~bash
cd <project_root>/mains
screen -S <instance>_master
sudo /usr/local/bin/gunicorn -c gun.py master_main:flask_app
~~~

### 服务器内虚拟机路径

~~~bash
cd <project_root>/mains
screen -S <instance>_master
sudo /usr/local/python3/bin/gunicorn -c gun.py master_main:flask_app
~~~

看到 Gunicorn 成功绑定 `<master_port>` 且没有持续 traceback 后，按 `Ctrl+A`，再按 `D`，离开但不终止 screen。

## 第五步：启动 Celery

### 服务器路径

~~~bash
cd <project_root>/mains
screen -S <instance>_celery
sudo /usr/local/bin/celery -A celery_worker.celery worker --loglevel=info
~~~

### 服务器内虚拟机路径

~~~bash
cd <project_root>/mains
screen -S <instance>_celery
sudo /usr/local/python3/bin/celery -A celery_worker.celery worker --loglevel=info
~~~

确认启动日志列出了 Klonet 任务且没有持续连接错误，再用 `Ctrl+A`、`D` 离开。多 Worker 跨主机部署时，Celery 通常不需要在每台 Worker 服务器重复启动。

## 第六步：启动 Web Terminal

### 服务器路径

~~~bash
cd <project_root>/mains
screen -S <instance>_web_terminal
sudo /usr/local/bin/python3.8 web_terminal_main.py
~~~

### 服务器内虚拟机路径

~~~bash
cd <project_root>/mains
screen -S <instance>_web_terminal
sudo /usr/local/python3/bin/python3.8 web_terminal_main.py
~~~

如果当前安装的解释器名称不是 `python3.8`，先用 `command -v python3.8` 或 `command -v python3` 确认与 Gunicorn 使用的是同一套依赖，再替换命令。看到 `Started!` 后用 `Ctrl+A`、`D` 离开。

## 第七步：按需执行 KVM 初始化

`libvirt_config.sh` 不是每次启动 Worker 的必选步骤。仅当满足下列情况之一，并且已经审查脚本内容时才按需执行：

- 当前服务器首次启用 Klonet KVM 节点。
- libvirt、tap、bridge 或相关网络初始化已经丢失。
- 当前部署说明明确要求在本次重启后重新执行。

~~~bash
cd <libvirt_script_directory>
sudo chmod u+x libvirt_config.sh
sudo ./libvirt_config.sh
~~~

纯 Docker Worker、普通代码重启或已有 KVM 网络状态正常时，直接启动 Worker，不执行该脚本。

## 第八步：启动 Worker

### 服务器路径

~~~bash
cd <project_root>/mains
screen -S <instance>_worker
sudo /usr/local/bin/gunicorn -c worker_gun.py worker_main:flask_app
~~~

### 服务器内虚拟机路径

~~~bash
cd <project_root>/mains
screen -S <instance>_worker
sudo /usr/local/python3/bin/gunicorn -c worker_gun.py worker_main:flask_app
~~~

看到 Worker 成功绑定 `<worker_port>` 后，用 `Ctrl+A`、`D` 离开。随后从 Master 验证 Worker 健康接口和注册状态。

## 第九步：配置并重载 Nginx

编辑默认站点：

~~~bash
sudo vim /etc/nginx/sites-available/default
~~~

当前平台使用的标准参数化配置如下：

~~~nginx
server {
    listen <public_port>;
    server_name <server_name>;
    index index.html index.htm index.nginx-debian.html;

    location /file/dload/ {
        proxy_pass http://127.0.0.1:<master_port>/file/dload/;
    }

    location /file/uload/ {
        proxy_pass http://127.0.0.1:<master_port>/file/uload/;
    }

    location /reallyload/ {
        set $workerip $arg_workerip;
        set $filename $arg_filename;
        proxy_pass http://${workerip}:<master_port>/download/${filename};
        index index.html index.htm index.jsp;
    }

    location /download/ {
        alias /root/vemu_static/;
    }

    location / {
        include proxy_params;
        proxy_pass http://127.0.0.1:<master_port>;
    }

    location /VEMU2/ {
        alias <frontend_path>/;
    }
}
~~~

注意：

- `<public_port>` 是浏览器访问的端口，`<master_port>` 是 Master Gunicorn 端口。
- 当前历史部署要求 Worker 下载服务使用与 Master 约定一致的服务端口，因此 `/reallyload/` 模板沿用 `<master_port>`。若当前配置已将 `worker_port` 分离，应以 Worker 实际下载接口端口为准。
- `<frontend_path>/` 末尾的 `/` 不能省略。
- 不要把真实 IP、用户名或个人目录写入公共知识文档。

保存后先检查语法，成功后再重载：

~~~bash
sudo nginx -t
sudo nginx -s reload
~~~

## 第十步：修改并访问前端

编辑当前实际部署的前端版本：

~~~bash
sudo vim <frontend_path>/scripts/config.js
# 仓库中的常见相对路径：VEMU2/scripts/config.js
~~~

至少核对：

- 后端 IP 使用 `<master_ip>`，即宿主机对外可达地址，不是 NAT 内部虚拟机地址。
- 普通 API 使用 Nginx 的 `<public_port>`。
- Web Terminal 使用 `<terminal_port>`。
- API 前缀与当前 Nginx location 保持一致。

修改后硬刷新浏览器，访问：

~~~text
http://<master_ip>:<public_port>/VEMU2/views/user.html
~~~

如果静态页面能打开但无法登录，先在浏览器 Network 记录实际请求，再直接请求 Master 接口，区分前端配置、Nginx 转发和后端问题。

## 启动后验证

~~~bash
screen -ls
ps aux | grep '[g]unicorn'
ps aux | grep '[c]elery'
ps aux | grep '[r]edis'

sudo lsof -i :<master_port>
sudo lsof -i :<worker_port>
sudo lsof -i :<public_port>
sudo lsof -i :<terminal_port>

curl http://127.0.0.1:<worker_port>/server_health/
~~~

然后完成一次：

1. Worker 注册检查。
2. 前端登录。
3. 最小拓扑创建与进度检查。
4. 最小拓扑删除。
5. Docker、OVS 或 libvirt 遗留资源检查。

## 正常停止

正常停止不需要先查 PID 或执行 `pkill`。进入对应 screen，使用 `Ctrl+C` 终止前台进程：

~~~bash
screen -ls

screen -r <instance>_worker
# 按 Ctrl+C

screen -r <instance>_web_terminal
# 按 Ctrl+C

screen -r <instance>_celery
# 按 Ctrl+C

screen -r <instance>_master
# 按 Ctrl+C
~~~

如果进程停止后仍停留在 shell，执行 `exit` 关闭该 screen。停止顺序优先使用 Worker -> Web Terminal -> Celery -> Master；停止前先确认没有正在运行的拓扑任务。

Nginx、Docker、Redis、MySQL 和 RabbitMQ 通常是共享或基础服务，单纯停止 Klonet 后端时不要一并停止。

## 正常重启

重启就是：

1. 使用 `screen -r <screen_name>` 返回目标 screen。
2. 按 `Ctrl+C` 停止该进程。
3. 确认端口已经释放。
4. 在同一 `<project_root>/mains` 目录执行该服务原来的完整启动命令。
5. 用 `Ctrl+A`、`D` 离开 screen。
6. 重新执行对应健康检查。

配置修改后只重启受影响服务：

- `config.py`：通常重启 Master、Celery、Web Terminal 和 Worker。
- Nginx：先 `sudo nginx -t`，再 `sudo nginx -s reload`。
- 前端 `config.js`：保存后硬刷新浏览器，通常不需要重启后端。
- Worker 代码：重启目标 Worker，并验证重新注册。
- Celery 任务代码：确认没有运行中任务后重启 Celery。

## screen 无法恢复时

先收集证据：

~~~bash
screen -ls
ps aux | grep '[g]unicorn'
ps aux | grep '[c]elery'
sudo lsof -i :<port>
~~~

只有确认目标 PID 属于当前实例且无法通过 screen 正常停止时，才使用精确 `kill <pid>`。不要在多实例服务器上执行 `pkill -f gunicorn` 或批量退出全部 screen。

## 常见问题

### Master 启动即报 import 错误

确认运行目录为 `<project_root>/mains`，并确认 Gunicorn、Celery 与 Python 使用同一套 `/usr/local/bin` 或 `/usr/local/python3/bin` 环境。不要混用两套依赖。

### Worker 进程存在但平台不可见

从 Worker 到 Master 检查注册地址，从 Master 到 Worker 检查 `/server_health/`，再核对 `worker_list` 和心跳状态。

### 前端能打开但无法登录

浏览器静态资源正常不代表 Master API 正常。检查 `config.js` 的宿主机 IP、public 端口、Nginx `location /` 和 Master 监听端口。

### Web Terminal 无法连接

核对 `web_terminal_main.py` 实际监听端口、`config.py` 的 `web_terminal_port`、前端 Terminal 配置和 WebSocket 代理链路。

## 关键文件

- `vemu_uestc/vemu_config/config.py`
- `mains/master_main.py`
- `mains/celery_worker.py`
- `mains/web_terminal_main.py`
- `mains/worker_main.py`
- `mains/gun.py`
- `mains/worker_gun.py`
- `VEMU2/scripts/config.js`
- `/etc/nginx/sites-available/default`

## 证据来源

- klonet_knowledge/10_platform_operation_notes_lihetian/平台操作步骤-李鹤天.docx
- klonet_knowledge/06_quick_start_docs/平台重启步骤.docx
- klonet_knowledge/06_quick_start_docs/服务器基础环境部署、依赖服务检查、平台服务启动2024_3_15.md
- klonet_knowledge/02_vemu_uestc_code/doc/平台安装与部署/VEMU程序启动.md
- knowledge/staging/platform_operation_notes_lihetian_curated.md
