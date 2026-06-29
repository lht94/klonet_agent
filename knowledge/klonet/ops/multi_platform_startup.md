---
title: Klonet 多平台启动与冲突检查
status: current_runbook
priority: P0
domains: operations, runtime, troubleshooting
intent_tags: platform_start, platform_restart, port_conflict, nginx, frontend
last_verified: 2026-06-30
---

# Klonet 多平台启动与冲突检查

## 适用范围

本条目用于已经完成基础环境安装后的 Klonet 平台新增、启动和重启。平台名不是固定值，`<instance>` 可以是任意不冲突的实例名，例如课程号、用户缩写或临时编号。

在同一台服务器上新增平台前，必须先确认已有平台占用的 screen 名称、后端端口、Web Terminal 端口、Nginx 对外端口和前端 alias。不要复用已有 screen 名称；不要复用已有后端端口、Web Terminal 端口、Nginx 对外端口或前端 alias。

## 第一步：查看已有平台占用

先收集当前服务器上所有可能冲突的运行态信息：

```bash
screen -ls
ps aux | grep -E '[g]unicorn|[c]elery|web_terminal_main'
sudo ss -lntp
sudo nginx -T
grep -R "proxy_pass\|listen\|alias" /etc/nginx
```

需要记录：

- 已有 screen 名称，例如 `<old_instance>_m`、`<old_instance>_c`、`<old_instance>_web`、`<old_instance>_w`。
- Master Gunicorn 端口：`<master_port>`。
- Worker Gunicorn 端口：`<worker_port>`。
- Web Terminal 端口：`<terminal_port>`。
- Nginx 对外端口：`<public_port>`。
- 前端 alias 或 location：`<frontend_alias>`。
- 已有项目目录：`<project_root>`，避免误把 workspace 副本当作运行目录。

## 第二步：选择新平台参数

为新平台选择一组没有冲突的参数：

```text
<instance>       新平台实例名
<project_root>   新平台项目目录，目录下应有 mains/ 和 vemu_uestc/
<master_port>    新 Master 后端端口
<worker_port>    新 Worker 后端端口
<terminal_port>  新 Web Terminal 端口
<public_port>    新 Nginx 对外端口
<frontend_alias> 新前端 location/alias，例如 /VEMU2-new/
<frontend_path>  新前端目录
```

修改 `<project_root>/vemu_uestc/vemu_config/config.py`、`<project_root>/mains/web_terminal_main.py`、前端 `scripts/config.js` 和 Nginx 配置时，必须使用同一组参数。

## 第三步：进入运行目录

所有后端命令都从 `<project_root>/mains` 执行：

```bash
cd <project_root>/mains
pwd
test -f gun.py
test -f master_main.py
test -f celery_worker.py
test -f web_terminal_main.py
test -f worker_gun.py
test -f worker_main.py
```

## 第四步：按顺序启动四个后端 screen

当前 Klonet 后端统一使用 `/usr/local/python3/bin/` 下的 Python 工具。旧资料中可能出现 `/usr/local/bin/gunicorn` 或 `/usr/local/bin/celery`，那是历史环境写法；在未知服务器上操作前用 `command -v gunicorn`、`command -v celery`、`command -v python3.8` 和现有 screen 输出确认实际环境。

`/usr/local/bin/redis-server` 只是 Redis 独立服务的历史常见路径，不能用它推断 Klonet 后端 Gunicorn/Celery 的 Python 环境。

```bash
cd <project_root>/mains

# Master
screen -S <instance>_m
sudo /usr/local/python3/bin/gunicorn -c gun.py master_main:flask_app

# Celery
screen -S <instance>_c
sudo /usr/local/python3/bin/celery -A celery_worker.celery worker --loglevel=info

# Web Terminal
screen -S <instance>_web
sudo /usr/local/python3/bin/python3.8 web_terminal_main.py

# Worker
screen -S <instance>_w
sudo /usr/local/python3/bin/gunicorn -c worker_gun.py worker_main:flask_app
```

每个 screen 看到服务正常启动后，用 `Ctrl+A` 再按 `D` 离开，不要关闭进程。

## 第五步：配置新平台前端与 Nginx

为新平台增加不冲突的 Nginx 对外端口和前端 alias。示例模板：

```nginx
server {
    listen <public_port>;
    server_name <server_name>;

    location / {
        include proxy_params;
        proxy_pass http://127.0.0.1:<master_port>;
    }

    location <frontend_alias> {
        alias <frontend_path>/;
    }
}
```

保存后先检查再重载：

```bash
sudo nginx -t
sudo nginx -s reload
```

## 第六步：启动后验证

```bash
screen -ls
ps aux | grep -E '[g]unicorn|[c]elery|web_terminal_main'
sudo ss -lntp | grep -E '<master_port>|<worker_port>|<terminal_port>|<public_port>'
curl http://127.0.0.1:<worker_port>/server_health/
```

如果遇到端口冲突、screen 名称冲突或 Nginx alias 冲突，先回到第一步重新核对已有平台，不要直接 kill 全部 gunicorn、celery 或 screen。
