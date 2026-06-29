---
title: Klonet 当前服务器启动路径修正
status: current_runbook
priority: P0
domains: operations, runtime, troubleshooting
intent_tags: platform_start, platform_restart, path_selection
last_verified: 2026-06-29
---

# Klonet 当前服务器启动路径修正

## 适用范围

本条目用于回答 adminis 服务器上 Klonet 平台启动、重启、screen 命名和 Python 路径选择问题。

当前服务器的后端 Master、Celery、Web Terminal 和 Worker 统一使用 `/usr/local/python3/bin/` 下的 Python 工具。不要把历史文档里的 `/usr/local/bin/gunicorn` 或 `/usr/local/bin/celery` 当作当前服务器默认启动命令。

`/usr/local/bin/redis-server` 只是 Redis 独立服务的历史常见路径，不能用它推断 Klonet 后端 Gunicorn/Celery 的 Python 环境。

## 103 平台启动顺序

```bash
cd /home/adminis/lht/103_project/mains

# Master
screen -S 103_m
sudo /usr/local/python3/bin/gunicorn -c gun.py master_main:flask_app

# Celery
screen -S 103_c
sudo /usr/local/python3/bin/celery -A celery_worker.celery worker --loglevel=info

# Web Terminal
screen -S 103_web
sudo /usr/local/python3/bin/python3.8 web_terminal_main.py

# Worker
screen -S 103_w
sudo /usr/local/python3/bin/gunicorn -c worker_gun.py worker_main:flask_app
```

## 回答规则

- 用户提到 `103_project`、`103_` screen 前缀或当前 adminis 服务器时，优先使用本条目的命令。
- 如果旧知识库片段同时出现 `/usr/local/bin/` 和 `/usr/local/python3/bin/`，应明确说明旧片段混有历史路径，并以本条目为准。
- 如果要在其他服务器或未知环境上操作，先用 `command -v gunicorn`、`command -v celery`、`command -v python3.8` 或运行态 screen 记录确认，再给命令。
