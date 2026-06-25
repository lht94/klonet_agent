# Klonet 平台启动知识集合

collection_id: klonet_runtime_startup

## 适用范围

该集合用于已经完成基础环境安装后的 Klonet 平台服务启动、停止、重启和启动后验证。

## 支持的结构化意图

- task_type: deployment_guidance, operation_guide, troubleshooting
- operation: platform_start, platform_stop, platform_restart
- target: klonet_platform, redis, gunicorn, celery, web_terminal, nginx, worker, master

## 排除范围

- 不用于首次安装基础环境。
- 不用于 `base_requ_setup.sh NORMAL`、`docker_service.sh`、镜像仓库和系统依赖安装。

## 当前文档

- ../startup_shutdown.md

## 机器可读 Manifest

- manifest.json

