---
title: Klonet 环境部署
status: current_runbook
priority: P0
domains: operations, deployment, dependencies
intent_tags: environment_setup, dependency_install
last_verified: 2026-06-24
---

# Klonet 环境部署

## 适用场景

用于在一台新的服务器或服务器内虚拟机上首次部署 Klonet 基础环境。当前标准做法是获取 `vemu_install_new_gen` 安装包并运行环境脚本；Python、Docker、OVS、Redis、MySQL、RabbitMQ 等组件不再逐项手工安装。

本文不保存真实服务器地址、账号、凭据、DNS 或内部镜像仓库地址。执行前将尖括号占位符替换为当前环境值。

## 标准部署结论

标准流程只有四个阶段：

1. 把环境安装包传到目标服务器。
2. 解压后执行 `base_requ_setup.sh NORMAL`。
3. 执行 `docker_service.sh` 启动基础容器服务。
4. 配置 Docker 镜像仓库并完成验收。

`base_requ_setup.sh DPDK` 不是当前 Klonet 的标准部署路径，不要使用 DPDK 参数。安装脚本可以重复执行，但重复执行前仍应检查其版本和已有服务状态。

## 部署前确认

- 目标服务器是全新环境，或已经确认脚本不会覆盖其他平台实例。
- 当前账号可使用 `sudo`，且目标磁盘空间充足。
- 已获得当前使用的 `<install_bundle>.tar`，解压目录预期为 `vemu_install_new_gen`。
- 已确定 Master 对外可达地址 `<master_ip>` 和镜像仓库端口 `<registry_port>`。
- 修改 Docker 配置和重启 Docker 前，确认服务器上没有不能中断的其他容器业务。

## 第一步：传输安装包

优先把安装包传到普通用户家目录，再移动到 `/root`，不需要为了直接写入 `/root` 而放宽目录权限。

~~~bash
# 在安装包所在机器执行；SSH 使用默认端口时删除 -P <ssh_port>
scp -P <ssh_port> <install_bundle>.tar <user>@<server_ip>:~/

# 在目标服务器执行
sudo mv ~/<install_bundle>.tar /root/
sudo -i
cd /root
~~~

如果安装包已经位于目标服务器，直接确认文件校验值和来源后进入下一步。

## 第二步：运行基础环境脚本

~~~bash
sudo -i
cd /root
apt-get update
tar -xvf <install_bundle>.tar
cd /root/vemu_install_new_gen

# 当前标准模式；脚本可在确认状态后重复运行
bash base_requ_setup.sh NORMAL

# 启动或创建 Klonet 所需的基础容器服务
bash docker_service.sh
~~~

不要执行：

~~~bash
# 非当前标准路径
bash base_requ_setup.sh DPDK
~~~

旧环境中可能只有 `docker_master.sh` 和 `docker_worker.sh`。它们属于历史替代路径，仅当目标安装包没有 `docker_service.sh`，且已确认脚本内容与服务器角色时才使用。

安装过程中如果报错，先保存完整脚本输出和失败命令，不要立即改成手工安装所有组件。

## 第三步：配置 Docker 镜像仓库

Master 和 Worker 都需要把 `insecure-registries` 指向 Master 上实际运行的镜像仓库。先备份原配置：

~~~bash
sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%Y%m%d%H%M%S)
sudo vim /etc/docker/daemon.json
~~~

最小参数化示例：

~~~json
{
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "insecure-registries": [
    "<master_ip>:<registry_port>"
  ]
}
~~~

若当前服务器已有 `registry-mirrors`、`dns`、存储驱动或其他 runtime 配置，应在原 JSON 上合并，不能直接覆盖。`insecure-registries` 只适用于受控内网；具备条件时应改用 TLS 和认证。

确认 JSON 有效并评估容器中断影响后执行：

~~~bash
python3 -m json.tool /etc/docker/daemon.json
sudo systemctl daemon-reload
sudo systemctl restart docker
sudo docker info
~~~

## 第四步：检查基础服务

### 基础容器

~~~bash
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
~~~

安装脚本通常会准备 MySQL、Celery Redis、RabbitMQ 和镜像仓库等基础容器。具体容器名以当前 `docker_service.sh` 为准；历史环境常见名称包括 `mysql-vemu`、`redis-celery`、`rabbitmq-server` 和 `registry`。

如果容器已经存在但处于停止状态，先查看日志，再定向启动，不要重新创建同名容器：

~~~bash
sudo docker ps -a
sudo docker logs --tail 100 <container_name>
sudo docker start <container_name>
~~~

### Klonet Redis

Klonet 运行态 Redis 可能是宿主机上的独立实例，机器断电或重启后经常需要重新启动。先检查：

~~~bash
ps aux | grep '[r]edis'
~~~

若未运行，使用当前服务器已有的标准启动脚本：

~~~bash
cd /root/vemu_install_new_gen
sudo bash ./service_begin_both/begin_redis.sh
ps aux | grep '[r]edis'
~~~

部分环境没有该入口脚本，需要从配置目录启动：

~~~bash
cd /root/vemu_install_new_gen/install_redis
sudo /usr/local/bin/redis-server redis.conf &
ps aux | grep '[r]edis'
~~~

端口和鉴权方式必须从当前 `redis.conf` 与 Klonet 配置读取，不使用历史文档中的端口或密码。Klonet 运行态 Redis 的历史常见配置是 `RedisConfig.redis_port = 8368`，但仍应以当前运行项目的 `vemu_config/config.py`/`PROJ_CONFIG` 为准；排查 `worker_list`、用户 DB、进度表或拓扑状态时，`redis-cli` 必须显式带 `-p <redis_port>`，不能默认连接 `6379`。

## 部署验收

依次确认：

~~~bash
# 安装目录与关键脚本
test -d /root/vemu_install_new_gen
test -f /root/vemu_install_new_gen/base_requ_setup.sh
test -f /root/vemu_install_new_gen/docker_service.sh

# 关键命令
python3 --version
docker --version
sudo docker info
ovs-vsctl --version
screen --version

# 服务状态
sudo docker ps
ps aux | grep '[r]edis'
~~~

验收标准：

- `base_requ_setup.sh NORMAL` 和 `docker_service.sh` 没有未处理错误。
- Docker daemon 正常，基础容器处于预期状态。
- Klonet Redis 进程存在，且配置端口与后端配置一致。
- Master 与 Worker 均能访问 `<master_ip>:<registry_port>`。
- 当前服务器角色需要 KVM 时，CPU 虚拟化、libvirt 和镜像目录另按 KVM 文档验证。
- 环境验收完成后，再按照 [启动与停止](startup_shutdown.md) 启动 Klonet。

## 脚本失败时如何处理

标准处理顺序：

1. 保存失败脚本、完整输出、操作系统版本和执行目录。
2. 找到脚本中的首个失败命令，而不是只看最后一行。
3. 检查 apt 源、网络、磁盘、已有包版本和服务端口。
4. 阅读当前安装包内脚本，确认它实际安装了哪些组件。
5. 只修复已确认失败的组件，再重跑标准脚本。

不要把“看到一个 import 错误就全局安装一个最新包”作为部署方法。历史 Klonet 依赖版本较旧，临时升级 Flask、SQLAlchemy、Celery 或 eventlet 可能让其他模块失效。

## 证据来源

- klonet_knowledge/10_platform_operation_notes_lihetian/平台操作步骤-李鹤天.docx
- klonet_knowledge/06_quick_start_docs/服务器基础环境部署、依赖服务检查、平台服务启动2024_3_15.md
- klonet_knowledge/02_vemu_uestc_code/doc/平台安装与部署/
- knowledge/staging/platform_operation_notes_lihetian_curated.md
