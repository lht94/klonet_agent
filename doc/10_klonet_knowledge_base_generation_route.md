# Klonet Agent 知识库生成路线

## 目标

Klonet Agent 的知识库不应只是把源码和文档全部塞进检索系统，而应沉淀为一套可维护、可追溯、可扩展的知识资产。

本路线采用四层结构：

```text
1. 人工知识层：稳定、可维护、给人读的 Markdown
2. 机器索引层：自动生成、给检索和定位用的结构化索引
3. 原始证据层：源码、原始文档、PDF/DOCX/PPTX、实验材料
4. 运行经验层：问答、排错、项目日志、真实维护案例
```

其中，Markdown 主要承担解释、教学和长期维护；JSONL/SQLite 等结构化文件主要承担路由、定位和索引；源码和原始文档承担最终证据；运行经验层负责沉淀团队真实开发和运维经验。

## 一、人工知识层

### 定位

人工知识层是 Klonet Agent 的“教材”和“项目手册”。它面向人和 Agent 同时可读，适合长期维护和版本管理。

建议目录：

```text
knowledge/klonet/
  00_project_overview.md
  01_architecture.md
  02_runtime_roles.md
  03_domain_terms.md

  flows/
    topology_deploy.md
    topology_delete.md
    worker_register_heartbeat.md
    link_config_and_delay.md
    traffic_generation.md
    monitor_and_data_server.md
    image_registry.md
    kvm_and_vm_networking.md
    satellite_experiment.md

  dev/
    backend_api_development.md
    flask_route_and_view_pattern.md
    schema_validation.md
    git_workflow.md
    coding_style.md
    testing_and_postman.md

  ops/
    environment_setup.md
    startup_shutdown.md
    nginx_and_file_download.md
    frontend_backend_mapping.md
    virtual_sim_ops.md
    common_troubleshooting.md

  user_api/
    vemu_api_usage.md
    topology_script_examples.md
```

### 内容来源

- 项目总览：`Klonet介绍.docx`、`Klonet.pdf`、功能介绍类材料。
- 架构知识：`vemu_uestc/doc/平台重构/*`、`平台重构/rebuild_blueprint.md`。
- 拓扑流程：源码、重构文档、`webserver/tasks/topo/tasks.py`。
- 流量功能：`实时流量发生与实时查询流量端信息.docx`。
- 链路延迟：`链路实时延迟查询.docx`。
- 后端 API 开发：`平台代码修改.docx`。
- Git 流程：`git/服务器直接Git开发操作.md`。
- 环境部署：`服务器基础环境部署、依赖服务检查、平台服务启动2024_3_15.md`。
- Nginx 和文件下载：`文件上传下载.md`、平台启动/运维文档。
- 虚仿运维：`平台运维内容.docx`、`虚仿平台运维手册.docx`。
- KVM/虚机组网：`虚拟机相关/*`、`虚机terminal相关文档/*`。

### 文档模板

每篇人工知识文档建议使用统一结构：

```text
# 标题

## 适用场景
这篇文档解决什么问题。

## 核心结论
给 Agent 优先使用的短结论。

## 关键流程
步骤、调用链、状态变化。

## 关键文件
列源码路径，不复制大段源码。

## 常见问题
错误表现、可能原因、排查顺序。

## 证据来源
原始文档路径、源码路径、更新时间。
```

## 二、机器索引层

### 定位

机器索引层是 Agent 的“地图”。它主要服务代码定位和检索路由，不负责解释设计原因。

它应由脚本从源码和文档中自动生成，避免手写后过期。

建议目录：

```text
knowledge/klonet_index/
  files.jsonl
  symbols.jsonl
  routes.jsonl
  celery_tasks.jsonl
  config_items.jsonl
  data_models.jsonl
  domain_map.jsonl
```

### 典型索引

`routes.jsonl` 示例：

```json
{
  "route": "/master/topo/",
  "methods": ["POST", "DELETE", "GET", "PUT"],
  "view_class": "TopoDeployAPI",
  "registered_in": "webserver/app_factory.py",
  "implementation": "webserver/api/topo/master_topo.py",
  "domain": "topology",
  "side": "master"
}
```

`symbols.jsonl` 示例：

```json
{
  "path": "Service_layer/redisAPI.py",
  "symbol": "UserDB",
  "kind": "class",
  "domain": "data",
  "summary": "用户维度 Redis 拓扑运行态数据访问类"
}
```

`domain_map.jsonl` 示例：

```json
{
  "domain": "traffic",
  "paths": [
    "webserver/api/traffic/",
    "Service_layer/TrafficManager.py",
    "tools/vemu_api/traffic.py"
  ],
  "notes": "流量定义、下发、状态保存、实时查询相关代码"
}
```

### 作用

机器索引层回答的问题包括：

- 某个 API 路由在哪里注册？
- 某个类或函数在哪个文件？
- 某个业务域涉及哪些目录？
- 某个 Celery 任务在哪定义？
- 某个配置项属于哪个组件？

## 三、原始证据层

### 定位

原始证据层保存未经改写的事实来源。它是 Agent 回答和人工审查时的最终依据。

包括：

```text
Klonet/vemu_uestc/                 # 源码
Klonet/vemu_uestc/doc/             # 旧项目文档
Klonet/平台教学运维/                # 运维经验
Klonet/快速入门文档/                # 入门和部署
Klonet/虚拟机相关/                  # KVM/虚机组网
Klonet/虚机terminal相关文档/         # SSH/Web terminal
Klonet/平台重构/                    # 重构思路
Klonet/前端/                        # 前端和接口配置
```

### 处理方式

不要把原始文件全部改写成 Markdown 后丢弃原文。正确方式是：

```text
原始文件保留
+ 抽取一份 text/md 中间件
+ 中间件记录 source_path
```

当前脚本：

```bash
python scripts/extract_raw_docs.py
```

如果要处理 DOCX/PPTX 中的嵌入图片，或处理扫描版 PDF，需要先检查 OCR 环境：

```bash
python scripts/check_ocr_env.py
```

OCR 依赖分两层：

- Python 包：`Pillow`、`pytesseract`、`pdf2image`，由 `requirements.txt` 管理。
- 系统工具：Tesseract OCR、Poppler。Windows 上需要安装后把 `tesseract.exe`、`pdftoppm.exe` 或 `pdftocairo.exe` 加入 `PATH`。

如果 Tesseract 已安装但没有加入 `PATH`，可以设置环境变量：

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

脚本也会自动尝试常见 Windows 安装路径：

```text
C:\Program Files\Tesseract-OCR\tesseract.exe
C:\Program Files (x86)\Tesseract-OCR\tesseract.exe
```

确认环境可用后，再显式启用 OCR：

```bash
python scripts/extract_raw_docs.py --ocr --ocr-lang chi_sim+eng
```

默认不启用 OCR，避免普通文本抽取依赖本机必须安装 Tesseract/Poppler。没有 OCR 环境时，脚本应在抽取结果的 `Extraction Notes` 和 `_extraction_summary.json` 中明确记录原因，而不是伪造识别结果。

建议增加：

```text
knowledge/raw_manifest.jsonl
knowledge/extracted_docs/
  platform_ops_raw.md
  vm_terminal_ssh_raw.md
  realtime_traffic_raw.md
```

`raw_manifest.jsonl` 示例：

```json
{
  "source_path": "C:/Users/LHT/OneDrive/课设/Klonet/平台教学运维/虚仿平台运维手册.docx",
  "type": "docx",
  "category": "ops",
  "derived_text": "knowledge/extracted_docs/virtual_sim_ops_raw.md",
  "sensitivity": "contains_credentials",
  "action": "redact_before_public_index"
}
```

### 脱敏要求

部分运维文档包含账号、密码、服务器地址、Postman 账号等敏感信息。

这些内容可以保留在原始证据层，但进入人工知识层和公共索引前必须脱敏。

应保留的是排查方法，而不是明文凭据。例如：

```text
有学校 vlab 域名映射、教研室域名映射、内网服务三层链路；
排查时按 内网 IP -> 教研室域名 -> vlab 域名 的顺序定位。
```

不应在公共知识中写入真实密码、token、邮箱授权码和私有账号。

## 四、运行经验层

### 定位

运行经验层是 Klonet Agent 最有差异化价值的一层。它来自真实开发、问答、排错和运维过程，会随着团队使用持续增长。

建议目录：

```text
knowledge/klonet_experience/
  faq.md
  common_errors.md
  ops_cases.md
  dev_cases.md
  review_cases.md
  accepted_solutions.md

knowledge/klonet_experience/cases/
  2026-06-xx-topology-progress-stuck.md
  2026-06-xx-worker-register-failed.md
  2026-06-xx-data-server-multinode-monitor-bug.md
```

### Case 模板

```text
# 问题标题

## 现象
前端、后端或日志里看到什么。

## 环境
Master、Worker、Redis、MySQL、前端、端口信息；敏感内容脱敏。

## 排查路径
先查什么，再查什么。

## 根因
确认后的原因。

## 解决方案
操作步骤或代码修改点。

## 相关源码
路径即可。

## 相关文档
原始文档路径。

## 可复用结论
给后续 Agent 检索时优先使用的总结。
```

### 适合沉淀的经验

- `平台运维内容.docx`：拓扑资源清理、卫星实验排查、前后端定位方法、多机数据分析遗留问题。
- `虚仿平台运维手册.docx`：vlab 域名映射、内网/外网链路排查、Postman 测试路径。
- `虚机web终端登录文档.docx`：web terminal 访问异常、KVMBeatWS 检查问题。
- `虚机ssh登录文档.docx`：KVM SSH 登录、端口映射、iptables 配置。
- `服务器基础环境部署...md`：Docker、Redis、MySQL、OVS、screen 启动经验。
- `平台代码修改.docx`：新同学理解 Flask API 开发的教学经验。

## 五、四层协作方式

### Mentor 问答流程

例如用户问：拓扑部署进度条卡住怎么办？

```text
1. 人工知识层
   查 ops/common_troubleshooting.md、flows/topology_deploy.md

2. 运行经验层
   查有没有“进度条卡死”的历史案例

3. 机器索引层
   定位 process_bar、tasks、master_topo 相关源码

4. 原始证据层
   必要时读取源码或原始运维文档核对
```

### Coding 任务流程

例如用户说：新增一个链路延迟查询接口。

```text
1. 人工知识层
   查 dev/backend_api_development.md、flows/link_config_and_delay.md

2. 机器索引层
   查已有 /master/delay/、link 相关 API、Service_layer/LinkManager

3. 原始证据层
   读具体源码实现

4. 运行经验层
   查历史开发中是否有类似接口、Postman 测试经验、前端协同注意点
```

## 六、整理优先级

### 第一批：项目理解和启动排障

先让 Agent 能解释项目、指导启动和处理基础部署问题。

```text
00_project_overview.md
01_architecture.md
ops/environment_setup.md
ops/startup_shutdown.md
ops/nginx_and_file_download.md
dev/backend_api_development.md
dev/git_workflow.md
```

### 第二批：Klonet 核心业务

覆盖平台最核心链路。

```text
flows/topology_deploy.md
flows/topology_delete.md
flows/worker_register_heartbeat.md
data/redis_model.md
data/mysql_model.md
user_api/vemu_api_usage.md
```

### 第三批：维护经验和教学场景

覆盖真实教学、虚仿、卫星、KVM 和排错经验。

```text
ops/virtual_sim_ops.md
flows/satellite_experiment.md
flows/kvm_and_vm_networking.md
flows/traffic_generation.md
flows/link_config_and_delay.md
experience/common_errors.md
```

### 第四批：自动化索引

建立代码定位能力。

```text
routes.jsonl
symbols.jsonl
domain_map.jsonl
config_items.jsonl
```

## 七、最终落地形态

建议形成如下结构：

```text
knowledge/
  klonet/                    # 人工知识层
  klonet_index/              # 机器索引层
  raw_manifest.jsonl         # 原始证据清单
  extracted_docs/            # 原始文档抽取文本
  klonet_experience/         # 运行经验层
```

关键原则：

```text
Markdown 负责解释和沉淀。
JSONL/SQLite 负责定位和索引。
源码和原始文档负责证据。
经验案例负责让 Agent 越用越懂 Klonet。
```

这套结构不绑定当前检索实现。后续无论检索系统升级为 BM25、向量库、混合检索，还是 SQLite/知识图谱，知识资产本身都可以继续复用。
