# Klonet Operations Runbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Klonet 环境部署与启停文档改写成命令明确、条件清晰、可被 Agent 直接检索和执行的标准 Runbook。

**Architecture:** 以两份 P0 Markdown 为操作入口，安装文档只描述脚本化标准部署，启停文档覆盖已部署环境的完整生命周期。通过文档契约测试固定关键命令和安全边界，最后重建 JSONL 索引并验证检索排序。

**Tech Stack:** Markdown、pytest、现有 `KnowledgeIndexer` 与 `KnowledgeRetriever`

---

### Task 1: 固定 Runbook 文档契约

**Files:**
- Create: `tests/test_knowledge_runbooks.py`

- [ ] **Step 1: 编写失败测试**

测试读取两份 Markdown，并断言：

- 环境部署包含 `base_requ_setup.sh NORMAL`、`docker_service.sh`、`/etc/docker/daemon.json`、`systemctl daemon-reload` 和 `systemctl restart docker`。
- 环境部署明确 DPDK 不是标准路径，且真实 IPv4 地址未出现。
- 启停文档同时包含 `/usr/local/bin/gunicorn`、`/usr/local/python3/bin/gunicorn`、两种 Redis 启动方式、screen、Ctrl+C、前端 `config.js`。
- Nginx 模板包含六个 location、`nginx -t` 和 reload。
- `libvirt_config.sh` 附近包含“按需”或“KVM”条件，且文档不再包含 `<python_env>`。

- [ ] **Step 2: 验证测试失败**

Run: `python -m pytest tests/test_knowledge_runbooks.py -q`

Expected: FAIL，指出旧文档缺少标准脚本或具体命令。

### Task 2: 重写环境部署 Runbook

**Files:**
- Modify: `knowledge/klonet/ops/environment_setup.md`

- [ ] **Step 1: 将标准路径移到正文最前**

按“准备安装包 -> 解压 -> `apt-get update` -> `base_requ_setup.sh NORMAL` -> `docker_service.sh`”给出完整命令块。安装包文件名使用 `<install_bundle>.tar`，目录使用 `/root/vemu_install_new_gen`。

- [ ] **Step 2: 参数化 Docker daemon 配置**

给出不含真实 IP、DNS、镜像源和凭据的 JSON 模板，解释 `<master_ip>:<registry_port>`，并在重启 Docker 前提示确认共享实例影响。

- [ ] **Step 3: 增加部署后验收和失败分支**

具体列出 `docker ps`、Redis 检查、脚本日志及组件命令。将手工依赖安装明确降级为脚本失败后的排障，不写成标准操作。

- [ ] **Step 4: 运行契约测试**

Run: `python -m pytest tests/test_knowledge_runbooks.py -q`

Expected: 环境部署相关断言通过，启停相关断言仍可能失败。

### Task 3: 重写启动、停止和重启 Runbook

**Files:**
- Modify: `knowledge/klonet/ops/startup_shutdown.md`

- [ ] **Step 1: 写入配置与 Redis 准备命令**

明确修改 `vemu_uestc/vemu_config/config.py`、Web Terminal 端口与前端配置。Redis 先用 `ps aux | grep '[r]edis'` 检查，未运行时给出 `begin_redis.sh` 与 `redis-server redis.conf &` 两种环境路径。

- [ ] **Step 2: 写入两套后端启动命令**

分别给出服务器 `/usr/local/bin/` 与虚拟机 `/usr/local/python3/bin/` 下的 Master、Celery、Web Terminal 和 Worker 命令。说明 screen 创建、`Ctrl+A D` 离开和 `screen -r` 返回。

- [ ] **Step 3: 将 KVM 初始化改为条件步骤**

仅当首次启用 KVM、libvirt/tap/bridge 初始化丢失或脚本内容确认需要重放时，才运行 `libvirt_config.sh`。

- [ ] **Step 4: 写入完整 Nginx 与前端配置**

收录参数化 server 模板，包含下载、上传、Worker 下载、静态下载、Master 代理与 `/VEMU2/`。明确执行 `sudo vim /etc/nginx/sites-available/default`、`sudo nginx -t`、`sudo nginx -s reload`，然后修改 `VEMU2/scripts/config.js` 中宿主机 IP、public 端口和 Terminal 端口。

- [ ] **Step 5: 写入停止和重启操作**

使用 `screen -ls`、`screen -r <name>`、Ctrl+C 停止，再执行相同启动命令。精确 kill 仅作为 screen 无法恢复时的异常分支。

- [ ] **Step 6: 运行契约测试**

Run: `python -m pytest tests/test_knowledge_runbooks.py -q`

Expected: PASS。

### Task 4: 审计其他规范文档

**Files:**
- Inspect: `knowledge/klonet/**/*.md`
- Modify only when required: `knowledge/klonet/ops/common_troubleshooting.md`、`knowledge/klonet/flows/kvm_and_vm_networking.md`

- [ ] **Step 1: 扫描宽泛操作词**

搜索“检查、确认、核对、按需”和抽象占位符，判断每个命中是否已有命令、观察对象、成功标准或到 Runbook 的明确链接。

- [ ] **Step 2: 修复真正缺少执行入口的段落**

在常见排障中补最小状态命令或链接；在 KVM 文档中统一 `libvirt_config.sh` 的条件口径。架构、术语、开发设计文档不做无关改写。

- [ ] **Step 3: 运行知识库测试**

Run: `python -m pytest tests/test_knowledge.py tests/test_knowledge_runbooks.py -q`

Expected: PASS。

### Task 5: 重建索引并验收检索

**Files:**
- Regenerate: `knowledge/index.jsonl`

- [ ] **Step 1: 重建主索引**

Run: `python -c "from klonet_agent.knowledge.indexer import KnowledgeIndexer; print(KnowledgeIndexer().build())"`

Expected: 输出大于 0 的片段数量。

- [ ] **Step 2: 验证敏感信息与结构**

检查两份 Runbook 不含真实 IPv4、口令或 `<python_env>`，并确认六个 Nginx location、两类 Python 路径和 Redis 分支都存在。

- [ ] **Step 3: 验证检索**

用 `KnowledgeRetriever` 查询“全新服务器部署 Klonet”“启动 Klonet 后端”“修改前端 config.js IP 和端口”，期望前三条包含新版 `environment_setup.md` 或 `startup_shutdown.md`。

- [ ] **Step 4: 运行全量测试**

Run: `python -m pytest -q`

Expected: 所有测试通过。
