# Klonet 基础环境安装知识集合

collection_id: klonet_environment_setup

## 适用范围

该集合用于首次准备 Klonet 基础环境、安装系统依赖、启动基础容器服务和做环境验收。

## 支持的结构化意图

- task_type: deployment_preparation, operation_guide, troubleshooting
- operation: environment_setup, dependency_install, acceptance_check
- target: klonet_environment, docker, docker_service, base_requ_setup, registry

## 排除范围

- 不用于日常启动已经安装好的 Klonet 平台服务。
- 不用于 Master Gunicorn、Celery、Web Terminal、Worker、Nginx 的标准启动顺序。

## 当前文档

- ../environment_setup.md

## 机器可读 Manifest

- manifest.json

