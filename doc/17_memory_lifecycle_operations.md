# 记忆系统生命周期运维（阶段 7）

本文件覆盖记忆库（PostgreSQL + pgvector）**上线之后**的运维面：联合评测门槛、
权威切换与回滚、备份与恢复、数据删除权、日常巡检与故障速查。
安装步骤见 [`15_memory_postgres_deployment.md`](15_memory_postgres_deployment.md)；
设计与迁移计划见 [`../通用功能升级计划/02-记忆系统升级计划.md`](../通用功能升级计划/02-记忆系统升级计划.md)。

---

## 1. 上线前必须配好的开关

| 环境变量 | 位置 | 说明 |
| :--- | :--- | :--- |
| `KLONET_AGENT_MEMORY_DSN` | 应用进程 | 业务侧连接串，用 `klonet_app` 角色（读写） |
| `KLONET_AGENT_TEST_PG_DSN` | 运维/CI | **仅评测与测试用**，会建/删临时库，必须指向可随意破坏的库 |
| `KLONET_AGENT_MEMORY_AUTHORITY` | 应用进程 | 见第 3 节。默认 `cutover`（2026-10-10 起）：数据库优先，库不可用自动降级 Markdown |
| `KLONET_AGENT_MEMORY_WRITE_PIPELINE` / `..._MEMORY_PACK` | 应用进程 | 默认随 `cutover` 打开；也可单独设值灰度 |

> **降级语义（cutover 默认值）**：`KLONET_AGENT_MEMORY_AUTHORITY=cutover` 下，
> 数据库召回链路不可用（未配 DSN / 连不上库）时，orchestrator 自动降级回
> Markdown 注入与文件写入，打印一次告警并在 trace 记
> `memory_pack_markdown_fallback`。**"召回结果为空"不触发降级**——那只是
> 这条问题没有相关记忆，不代表库坏了。降级是可用性手段，不改变权威归属；
> 降级期间写入走旧文件工具，库恢复后（重启进程）自动回到数据库路径。

> **两个 DSN 刻意不共用。** 测试/评测会 `CREATE DATABASE`、`DROP DATABASE`，
> 复用业务 DSN 等于给一次误配留了一颗能删库的雷。

---

## 2. 联合评测门槛（cutover 的前置条件）

评测资产：

| 文件 | 作用 |
| :--- | :--- |
| `evals/memory_cases.jsonl` | 9 个用例，覆盖单跳、精确标识符、时态、过期、冲突、多跳、偏好、跨租户隔离、语义改写 |
| `evals/run_memory_eval.py` | 运行器：三条路径（Markdown 全量 / 数据库关键词 / 数据库混合）+ 指标聚合 + 阈值核验 |
| `evals/memory_eval_thresholds.json` | **冻结的验收阈值**（改它等于重新冻结基线） |
| `evals/memory_summary.md` | 每次运行的实测结果与逐条明细 |
| `tests/test_memory_eval_harness.py` | 离线契约测试：锁用例形状、阈值覆盖、聚合与判定逻辑本身 |

运行：

```bash
PYTHONPATH=<仓库父目录> KLONET_AGENT_TEST_PG_DSN=postgresql://... \
    python -m evals.run_memory_eval
# 或
python scripts/memory_cutover.py --check
```

指标口径：

- `recall@k` / `precision@k`：命中里相关项的占比；**precision 在零命中时记 0**
  （空结果既不贡献召回也不贡献精度，不能被"没答对反而得高分"钻空子）。
- `过期误召回率`：已过 `valid_to`、或已被替代/删除的记忆出现在当前视图的比例。
  Markdown 全量注入天然为 1（旧值也在文件里），这正是要修的问题。
- `跨租户泄漏率`：命中里属于其它用户/项目的比例，必须为 **0**。
- `平均注入 token`：被注入上下文的正文 token 估算。
- `冲突标注率`：存在 `contradicts` 关系的用例里，命中确实被标注的比例。

当前冻结的阈值与实测（2026-10-08）：

| 检查项 | 阈值 | 实测 |
| :--- | :--- | :--- |
| `hybrid_recall_at_k_min` | ≥ 0.9 | 1.00 |
| `hybrid_precision_at_k_min` | ≥ 0.6 | 0.75 |
| `keyword_recall_at_k_min` | ≥ 0.6 | 0.80 |
| `expired_false_recall_max` | = 0 | 0 |
| `cross_tenant_leakage_max` | = 0 | 0 |
| `conflict_flag_rate_min` | ≥ 1.0 | 1.00 |
| `hybrid_token_ratio_vs_markdown_max` | ≤ 1.0 | 0.8736 |

另有两项布尔检查：混合检索的 recall 不得低于关键词检索；带 `hybrid_only` 的
语义用例必须由混合检索赢下。

**评测用的是确定性哈希词袋嵌入**（离线可重放），量的是**管线**（三通道 + RRF +
冲突标注能不能把语义命中用起来），不是某个供应商的向量质量。真实语义增益需要
在线上用真模型另测一轮，结论写进评测报告，但**不改变这里的阈值口径**。

---

## 3. 权威切换与回滚

### 3.1 四态状态机

| 阶段 | Markdown 的角色 | 数据库的角色 |
| :--- | :--- | :--- |
| `legacy` | 唯一权威（回答读它） | 未使用 |
| `shadow` | 权威 | 影子写入，只验证"落得下来" |
| `compare` | 权威 | 双读对比，差异必须能解释 |
| `cutover` | 仅迁移/导出来源 | **唯一权威** |

只允许相邻推进（`shadow→compare→cutover`），每一步都可退回；唯一例外是
`cutover` 只退回 `compare`——回到 `shadow`/`legacy` 等于"重新读 Markdown 回答"，
那要先在 `compare` 里确认库里数据完整，不能一步跳回去。

迁移状态机由 `scripts/migrate_markdown_memory.py --phase <目标>` 推进；
进入 `cutover` **必须**提供只读快照（`--snapshot <导出文件>`）：

```bash
python scripts/migrate_markdown_memory.py --phase shadow
python scripts/migrate_markdown_memory.py --phase compare
python scripts/migrate_markdown_memory.py --phase cutover --snapshot exports/2026-10-08.json
```

### 3.2 运行期权威

状态机管"数据完整性"，运行期还有一个开关决定"回答时读谁"：

```bash
# 切换（评测达标后才允许）
KLONET_AGENT_TEST_PG_DSN=postgresql://... \
    python scripts/memory_cutover.py --apply /etc/klonet-agent/klonet-agent.env
# 回滚
python scripts/memory_cutover.py --rollback /etc/klonet-agent/klonet-agent.env
```

`--apply` 会先跑一遍评测，**不达标直接拒绝**——判定门槛不能被绕过（没有
`--skip-eval` 这类开关）。切到 `cutover` 时，`KLONET_AGENT_MEMORY_AUTHORITY`
一次打开写入管线与记忆包注入，`MEMORY.md` / `USER.md` 不再常驻注入。

> **2026-10-10 起 `cutover` 已是默认值**，`--apply` 主要用于把部署环境文件显式
> 固化（以及从 `legacy` 显式迁移的老部署）；`--rollback` 仍用于显式回退到
> `legacy`。运行期的可用性降级（库不可用退回 Markdown）与这里的**权威回滚**
> 是两回事：前者自动发生、库恢复即回；后者是迁移语义，回到最近一次只读快照。

**回滚语义**：回滚 = 回到最近一次只读快照，**不恢复双写**（计划 §8 第 8 条）。
快照文件路径记在迁移状态文件里。

---

## 4. 备份与恢复

### 4.1 备份

记忆库是普通 PostgreSQL 库，用标准工具即可；但有三处**不在 pg_dump 里**：

| 对象 | 位置 | 为什么容易漏 |
| :--- | :--- | :--- |
| 迁移状态文件 | `<memory_dir>/.migration_state.json` | 决定"哪些源文件迁过"和快照路径 |
| 只读快照导出 | `exports/*.json` | cutover 回滚的唯一依据 |
| Markdown 源 | `sessions/`、`users/`、`shared/ops/` | 迁移期间**不要删**，它同时是导出源 |

```bash
pg_dump --format=custom --no-owner \
        --dbname="$(echo "$KLONET_AGENT_MEMORY_DSN" | sed 's#.*/##')" \
        --file=klonet_memory_$(date +%F).dump
# 迁移状态与快照一起打包
tar czf klonet_memory_meta_$(date +%F).tar.gz .migration_state.json exports/
```

### 4.2 恢复

```bash
pg_restore --clean --if-exists --no-owner --dbname=klonet_memory klonet_memory_2026-10-08.dump
# 迁移与角色授予是幂等的，重跑一遍即可
python -m klonet_agent.memory.database   # 或由应用启动时的 run_migrations 完成
```

恢复后必做的三项核对（缺一项就可能"看起来好了"）：

1. `assert_ready()`：六表 `rls=true force=true`、三角色齐全、触发器就位；
2. `applied_migrations()` 的 checksum 与仓库内迁移文件一致（改过迁移文件会立刻暴露）；
3. 记忆库测试三件套 + 管理/鲁棒性测试：`tests/test_memory_*.py` 全绿。

**outbox 不需要单独备份**：向量是可以重算的派生数据。恢复后若有版本缺向量，
把对应版本重新登记进 outbox 再跑 worker 即可；全文与精确通道在补向量前仍然可用。

### 4.3 应用不可用时的降级

记忆库连不上时，召回失败一律降级为"这一轮不注入记忆"，**不抛异常、不伪造结果**；
但 `42xxx`（schema/pgvector 缺失）类错误会直接抛 `MemoryRetrievalError` ——
那是部署坏了，继续跑只会给出更差的答案。

---

## 5. 数据删除权

删除一条记忆必须**同时**做到三件事，缺一条就会出现"删了但还能召回"：

1. **逻辑删除**：记录状态转 `deleted`，当前视图立刻不再返回它；
2. **结束有效期**：当前版本写 `valid_to`，`as_of` 视图也不再把它当成"当时有效"；
3. **清向量与 outbox**：只删正文而留着向量，语义通道仍然能召回它。

三件事在 `PostgresMemoryRepository.delete_memory()` 的**同一个事务**里完成；
`memory/admin.py` 的 `MemoryAdmin` 负责调度、门槛与审计，不重复实现原子性。

```python
from klonet_agent.memory.admin import MemoryAdmin, RetentionPolicy

admin = MemoryAdmin(repo, tracer=tracer)
admin.explain(memory_id)                                  # 记录/版本/来源/关系全视图
admin.forget(memory_id, reason="用户要求删除")              # 逻辑删除 + 清向量 + 审计
admin.list_memories(include_deleted=True)                 # 删除是可审计的
admin.recall_check(memory_id)                             # 删除后还能不能被召回
admin.purge(policy=RetentionPolicy(soft_delete_retention_days=30))  # 物理清理
admin.archive_expired()                                   # 防御性归档
```

保留期不变量：

- **物理清理只碰"已被明确删除、且过了保留期"的记录**，绝不自动删除用户没删过的
  记忆；归档 ≠ 删除。
- `purge_deleted()` 的保留期门槛由调用方显式给出，**没有默认值**——不可逆操作
  不接受"猜一个"。
- 单批清理有上限（默认 1000），避免一个定时任务把整张表锁住。

运维侧要定期验证的三条（`tests/test_memory_admin.py` /
`tests/test_memory_resilience.py` 已经把它们固定住）：

- 删除后**换一个新进程**再查，仍然查不到（`test_deleted_memory_stays_gone_after_a_restart`）；
- 删除后 worker 再跑一轮，**不会**给已删除版本补向量
  （`test_embedding_worker_does_not_resurrect_a_deleted_memory`）；
- 删除中途失败**整体回滚**，不留"状态已删、向量还在"的半成品
  （`test_a_failed_deletion_rolls_back_completely`）。

---

## 6. 日常巡检

| 看什么 | 怎么看 | 期望 |
| :--- | :--- | :--- |
| outbox 覆盖率 | `EmbeddingWorker.stats()` 的 `coverage` | 长期应接近 1；持续偏低说明嵌入服务不稳 |
| 租约堆积 | `processing` 且 `next_attempt_at` 早于当前时间 | 有堆积说明 worker 进程反复崩溃 |
| 永久失败 | outbox `failed` 计数 | 每条都应有对应的 `last_error`，没有就是分类逻辑出错 |
| 过期未归档 | `MemoryAdmin.archive_expired()` 的返回值 | 常态应为 0；非 0 说明有写入路径绕过 `mark_expired` |
| 跨租户 | 记忆库测试里的隔离用例 | 必须为 0 |

**HNSW 索引暂时不建**：80 条记忆下 exact 向量扫描 < 2s
（`test_vector_scan_measurement_justifies_no_hnsw_yet`）。按计划 §12"先测量再决定"，
等真实数据量让扫描成为瓶颈时再建，建之前还要关注多租户过滤后候选不足的问题。

---

## 7. 故障速查

| 症状 | 根因 | 处理 |
| :--- | :--- | :--- |
| `password authentication failed for user "klonet"` 且真库用例大批 ERROR | 把**库内业务角色**当成登录角色用了 | 本地容器/测试库的登录角色是 `postgres`，写成 `postgres:...@127.0.0.1:<port>/postgres` |
| 测试出现 `16 passed, 24 errors` | 同上：DSN 写错导致真库用例全部连接失败 | 改对 DSN，不是环境坏了 |
| `MemoryRetrievalError`（SQLSTATE 42704/42883） | 服务端缺 pgvector 扩展 | 装 `postgresql-16-pgvector` 并 `CREATE EXTENSION vector;` |
| 迁移报 checksum 不符 | 已应用的迁移文件被改过 | 不要改历史迁移；新增一个迁移文件 |
| `bind: An attempt was made to access a socket in a way forbidden` | 端口落在 Windows 保留段 | 换端口（本地验证用 15432） |
| 记忆注入一直为空包 | 召回失败被降级，或预算太小 | 看 trace 的 `memory_pack_recall_failed` / `memory_pack_empty`，再查 DSN 与 token 预算 |
