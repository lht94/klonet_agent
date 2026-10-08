# 15 记忆库（PostgreSQL + pgvector）部署说明

本文对应 `通用功能升级计划/02-记忆系统升级计划.md` 阶段 1。记忆系统的权威存储是
**PostgreSQL + pgvector**，它同时承担结构化事实、版本、时态、来源、权限、全文与语义检索。

## 0. 两种环境，一套代码

| | 本机（开发/验收） | 远程服务器（真实执行环境） |
| --- | --- | --- |
| 形态 | Docker 容器 | **原生安装** |
| 用途 | 跑迁移、跑集成测试、验证 RLS | 承载真实记忆数据 |
| 数据 | 可随时丢弃重建 | 必须备份 |

代码里**没有任何 Docker 依赖**：`memory/database.py` 只读 DSN 环境变量，
两边跑的是同一套迁移文件。区别只在 DSN、`pg_hba.conf` 和角色密码。

> 为什么远程不装 Docker：该服务器（Ubuntu 20.04.6 LTS）上没有 Docker，
> 且它是真实执行环境，多一层容器运行时只会增加故障面。

## 1. 本机：Docker 起一个一次性 pgvector

```bash
# 需要先手动启动 Docker Desktop（守护进程不启动时 docker 命令会报
# "failed to connect to the docker API ... dockerDesktopLinuxEngine"）
./scripts/pg_local.sh up
```

脚本会打印测试用的 DSN。然后：

```bash
export KLONET_AGENT_TEST_PG_DSN="postgresql://postgres:klonet@127.0.0.1:15432/postgres"
python -m pytest tests/test_memory_postgres_repository.py -q
```

容器里的 `postgres` 是超级用户，集成测试需要临时建库/删库和 `SET LOCAL ROLE`，
所以它同时也是最方便的验收账号。

用完销毁：

```bash
./scripts/pg_local.sh down          # 删容器，保留数据卷
./scripts/pg_local.sh nuke          # 连数据卷一起删
```

## 2. 远程服务器：原生安装

目标环境事实（来自 `doc/对话记录3.md` 的实测记录）：

- 操作系统 **Ubuntu 20.04.6 LTS (Focal Fossa)**，x86_64
- Python **3.11.15**，位于 Miniconda 环境 `/home/lzl/miniconda3/envs/klonet_agent/bin/python3`
  （README 里 `python3.8` 那条命令已经失效，服务器上没有该解释器）
- **未安装 Docker**

### 2.1 安装 PostgreSQL 16 + pgvector

Ubuntu 20.04 默认源里的 PostgreSQL 是 12，且不带 pgvector 包。走 PGDG 官方源：

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates gnupg lsb-release

# 导入 PGDG 签名密钥
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc

# 添加 apt 源（focal-pgdg）
. /etc/os-release
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list

sudo apt-get update
sudo apt-get install -y postgresql-16 postgresql-16-pgvector
```

确认扩展文件已就位：

```bash
ls /usr/share/postgresql/16/extension/vector.control
```

**如果 PGDG 里没有该发行版对应的 `postgresql-16-pgvector`**（例如内网源被裁剪），
从源码编译 pgvector：

```bash
sudo apt-get install -y build-essential git postgresql-server-dev-16
git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git
cd pgvector && make && sudo make install && cd ..
```

服务与自启：

```bash
sudo systemctl enable --now postgresql
sudo -u postgres psql -c "SELECT version();"
```

### 2.2 建库

先用 `postgres` 超级用户建库（peer 认证，不需要密码）：

```bash
sudo -u postgres psql -c "CREATE DATABASE klonet_memory WITH ENCODING 'UTF8';"
```

`klonet_app` / `klonet_ops` / `klonet_migrator` 三个角色**由迁移脚本自己创建**
（`migrations/0002_roles_and_grants.sql`），不需要手工建。

### 2.3 跑迁移

用 `postgres` 账号跑一次迁移（这样 0002 才有 CREATEROLE 权限把三个角色建出来）。

```bash
cd ~/klonet_agent
sudo -u postgres env \
  KLONET_AGENT_MEMORY_DSN="postgresql:///klonet_memory?host=/var/run/postgresql" \
  PYTHONPATH="$(dirname "$PWD")" \
  /home/lzl/miniconda3/envs/klonet_agent/bin/python -c "
from klonet_agent.memory.database import MemoryDatabase
db = MemoryDatabase.from_env()
db.open()
print('applied:', db.run_migrations())
print('ready  :', db.assert_ready())
"
```

预期输出 `applied: ['0001_init', '0002_roles_and_grants']`；再跑一次应当输出
`applied: []`（幂等）。`assert_ready()` 会校验 pgvector 扩展、六张表和已应用迁移，
任何一项缺失都会直接报错，而不是等到第一次检索才失败。

### 2.4 设置角色密码

密码**不写进版本库**，部署时单独设置：

```bash
sudo -u postgres psql -d klonet_memory <<'SQL'
ALTER ROLE klonet_app PASSWORD '用密码管理器生成一个';
ALTER ROLE klonet_ops PASSWORD '另一个不同的密码';
SQL
```

确认应用角色**没有** RLS 绕过权限（两个都必须是 `f`）：

```bash
sudo -u postgres psql -d klonet_memory -c \
  "SELECT rolname, rolsuper, rolbypassrls, rolcreatedb
     FROM pg_roles WHERE rolname LIKE 'klonet\_%' ORDER BY rolname;"
```

### 2.5 配置 DSN

服务通过 `EnvironmentFile` 读环境变量（见 `scripts/klonet-agent.service.in`），
默认文件是 `/etc/klonet-agent/klonet-agent.env`：

```bash
sudo tee -a /etc/klonet-agent/klonet-agent.env >/dev/null <<'EOF'

# 记忆库（PostgreSQL + pgvector）
KLONET_AGENT_MEMORY_DSN=postgresql://klonet_app:上面设置的密码@127.0.0.1:5432/klonet_memory
EOF
sudo chmod 600 /etc/klonet-agent/klonet-agent.env
sudo chown root:root /etc/klonet-agent/klonet-agent.env

sudo systemctl restart klonet-agent
sudo journalctl -u klonet-agent -n 50 --no-pager
```

需要 Ops 共享记忆时，Ops 服务实例用 `klonet_ops` 角色连接（**不要**把
`klonet_app` 提升成 ops 角色，那等于让所有请求都能读共享运维记忆）。

`pg_hba.conf` 一般不用改：PostgreSQL 16 的默认配置已经允许
`host all all 127.0.0.1/32 scram-sha-256`，够本机服务使用。
只有跨主机连接才需要显式加行并 `sudo systemctl reload postgresql`。

### 2.6 验收（在服务器上跑集成测试）

`KLONET_AGENT_TEST_PG_DSN` 需要一个**能建库删库**的账号（测试会建临时库）：

```bash
cd ~/klonet_agent
KLONET_AGENT_TEST_PG_DSN="postgresql://postgres@/postgres?host=/var/run/postgresql" \
  python -m pytest tests/test_memory_postgres_repository.py -q
```

RLS 相关用例需要能 `SET LOCAL ROLE klonet_app`；用 `postgres` 账号即可。
如果换成一个普通角色，那几条会带着原因 skip——**skip 不等于通过**。

临时用命令行手工验证跨用户隔离：

```bash
PGPASSWORD='klonet_app 的密码' psql \
  "postgresql://klonet_app@127.0.0.1:5432/klonet_memory" <<'SQL'
BEGIN;
SELECT set_config('app.user_id', 'someone-else', true) AS bound_user;
SELECT count(*) AS visible_rows FROM memory_records;   -- 必须是 0
COMMIT;
SELECT current_setting('app.user_id', true) AS after_commit;  -- 必须是空
SQL
```

第二条 `count(*)` 只要是 0，就说明"忘记绑定租户"或"绑定错租户"读不到数据；
`after_commit` 为空说明租户变量随事务清除，没有残留在连接池会话里。

## 3. 安全模型要点

- **双层过滤**。应用层每条 SQL 都带 `user_id`（项目记忆再加 `project_id`）条件，
  数据库 RLS 是第二道闸。只靠 RLS 的话，任何一次用超级用户或 `BYPASSRLS`
  维护角色跑在线查询都会静默跨用户。
- **租户变量只在本事务内有效**。`SET LOCAL`/`set_config(..., is_local=true)`，
  事务结束后 PostgreSQL 自动清除。测试里有专门的泄漏探针。
- **`shared_ops` 只对 `klonet_ops` 角色放行**，且租户策略显式排除它——
  否则只要共享记忆的 `user_id` 撞上某个普通租户 id，那个租户就能读到全部共享记忆。
- **放行挂在角色上，不挂在 GUC 上**。会话自己能 `set_config`，用可自设的变量当权限
  等于没有权限。
- **不允许入库**：密码/API Key/cookie/Authorization、私钥或 `.env` 正文、
  未脱敏工具输出、他人未授权信息、模型隐藏推理内容。

## 4. 运维要点

> 备份、恢复、删除权与 cutover 的完整流程见
> [`17_memory_lifecycle_operations.md`](17_memory_lifecycle_operations.md)。
> 本节的"备份"只给最小可用的 pg_dump 形态。

### 备份

```bash
sudo -u postgres pg_dump -Fc klonet_memory > klonet_memory_$(date +%F).dump
```

迁移期间的状态机是 `legacy → shadow → compare → cutover`（见计划 §8），
cutover 回滚只能回到最近一次数据库导出的只读快照，**不恢复双写**。

### 迁移纪律

`migrations/*.sql` 一经执行即不可修改。执行器会记录 sha256，文件被改动后再跑
`run_migrations()` 会直接报 `MigrationError`。要改结构就新增 `NNNN_xxx.sql`。

### 向量维度变更

`memory_versions.embedding` 是 `vector(1024)`，对应 `text-embedding-v4` 的默认维度。
计划 §7.3 要求"不原地改变已有 vector 维度"：换模型或换维度时新增
`memory_embedding_profiles` + `memory_embeddings` 表并后台重嵌入，
而不是 `ALTER COLUMN`。

## 5. 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `未安装 PostgreSQL 驱动` | 没装 psycopg | `pip install 'psycopg[binary,pool]'` |
| `记忆库缺少 pgvector 扩展` | 用了不带 pgvector 的 PG 包 | 装 `postgresql-NN-pgvector`，或在库内 `CREATE EXTENSION vector;` |
| `记忆库 schema 不完整，缺少表` | 迁移没跑 | 跑 `MemoryDatabase.run_migrations()` |
| `迁移 ... 已应用但内容被改动` | 改了已执行的迁移文件 | 回退该文件，改成新增迁移 |
| `迁移 ... 执行失败：必须拥有...` | DSN 账号无建表权限 | 用 `klonet_migrator` 或 `postgres` 跑迁移 |
| `[0002] 当前账号缺少 CREATEROLE 权限` | 迁移账号不能建角色 | 用 `postgres` 重跑 0002，`shared_ops` 在那之前读不到（fail-closed） |
| Docker 报 `failed to connect to the docker API` | Docker Desktop 守护进程没起 | 手动启动 Docker Desktop |
| Docker 报 `ports are not available: ... bind: An attempt was made to access a socket in a way forbidden by its access permissions` | 端口落在 Windows 保留的动态端口段里 | 换端口：`KLONET_PG_PORT=15432 ./scripts/pg_local.sh up`。用 `netsh int ipv4 show excludedportrange protocol=tcp` 查排除段 |
