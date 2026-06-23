# Klonet 运维文档 Runbook 化设计

## 目标

将 Klonet 环境部署、启动、停止和重启文档改写为 Agent 与运维人员可以直接执行的标准操作手册。正文优先回答“按什么顺序执行哪些命令”，背景原理和异常分支只用于解释条件与风险。

## 信息口径

- 当前标准环境部署路径是获取 `vemu_install_new_gen` 安装包，执行 `base_requ_setup.sh NORMAL` 和 `docker_service.sh`，再完成 Docker 镜像仓库及必要服务配置。
- 不把手工逐项安装 Python、Docker、OVS、Redis、MySQL、RabbitMQ 等组件作为标准部署流程；这些内容只保留为安装脚本失败后的排障参考。
- 服务器常见可执行文件目录为 `/usr/local/bin/`，服务器内虚拟机常见目录为 `/usr/local/python3/bin/`。文档同时给出两套完整命令，不使用抽象的 `<python_env>`。
- 已部署服务器日常启动前重点确认 Redis 是否存活。根据服务器环境，可使用 `begin_redis.sh` 或在 `redis.conf` 所在目录直接启动 `redis-server`。
- `libvirt_config.sh` 是 KVM 条件步骤，不是每次启动 Worker 的必选步骤。
- Nginx 配置使用参数化模板，不保存真实服务器地址、内部域名、账号和凭据。

## 文档改写

### `environment_setup.md`

正文结构调整为：适用前提、准备安装包、解压与执行脚本、配置 Docker 镜像仓库、检查基础容器与 Redis、部署验收、脚本失败排查。删除喧宾夺主的组件百科和手工安装主流程。

### `startup_shutdown.md`

正文结构调整为：修改后端配置、检查或启动 Redis、进入正确项目目录、依次启动 Master/Celery/Web Terminal/Worker、配置 Nginx、修改前端 `config.js`、访问与验证、通过 screen 和 Ctrl+C 停止、使用相同命令重启。

Nginx 模板保留 `/file/dload/`、`/file/uload/`、`/reallyload/`、`/download/`、`/` 和 `/VEMU2/`，并明确 `<public_port>`、`<master_port>`、`<server_name>` 与 `<frontend_path>/` 的含义。修改后先执行 `sudo nginx -t`，成功后执行 `sudo nginx -s reload`。

## 其他文档审计

检查 `knowledge/klonet/` 中承担操作指导职责的文档。判断标准是：若文档要求用户“检查、确认、核对”某项状态，却没有给出可执行入口、观察对象或成功标准，则补充具体命令或链接到本次 Runbook；架构、术语和纯开发原理文档不为追求命令密度而修改。

## 安全与历史差异

- 使用 `<master_ip>`、`<worker_ip>`、`<port>`、`<server_name>` 等占位符。
- 不写入原始资料中的真实 IP、Redis/MySQL 密码、内部仓库地址和个人路径。
- 历史资料中互相冲突的目录、端口或启动方式以“环境变体”标注，不伪装成唯一事实。
- `chmod 777`、修改 Docker daemon、重启 Docker 和批量结束进程均标明影响范围，不作为无条件动作。

## 验收

1. 新服务器部署流程能从安装包开始完整执行，不需要读组件原理才能找到下一条命令。
2. 已部署服务器可以按文档完成 Redis 检查、后端、Nginx 和前端启动。
3. 两种 Python 路径均有完整示例。
4. 停止和重启明确使用 screen、Ctrl+C 和原启动命令。
5. 文档不包含真实凭据和内部地址。
6. 重建主索引后，“全新服务器部署”“启动 Klonet”“修改前端端口”等查询命中新版 Runbook。
