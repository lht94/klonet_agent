# 记忆生命周期 Worker 运维手册（04 计划阶段 7）

对应代码：`memory/maintenance/{service,cli,health}.py`、`memory/maintenance/jobs/*`。
部署样例：`deploy/systemd/`（systemd）与 `deploy/docker-compose.memory-maintenance.yml`（仅示例）。

## 日常命令

```bash
# 常驻运行（生产入口；systemd unit 的 ExecStart 就是这一行）
python -m klonet_agent.memory.maintenance run

# 单次把所有到期的 Job 各跑一轮（crontab / 手动）
python -m klonet_agent.memory.maintenance once

# 只跑某个 Job（规范名见下），--force 无视 next_run_at
python -m klonet_agent.memory.maintenance once --job embedding --force
python -m klonet_agent.memory.maintenance once --job purge --dry-run --force   # 只报数不删

# 健康快照（--json 供 CI / 监控脚本读取；字段名 = Prometheus 名）
python -m klonet_agent.memory.maintenance status --json
```

规范 Job 名（写进 `memory_maintenance_jobs.job_name`，是外部契约）：
`embedding_outbox` / `expiration` / `purge` / `consolidation` / `reembedding` / `health_report`。

退出码：`0` 成功；`1` 运行故障（DB 不可用等）；`2` 用法错误（未知 Job 名等）；`3` 配置错误（非法周期/租约、无 DSN）。

## 暂停 / 恢复

- **暂停单个 Job**（保留配置）：
  `UPDATE memory_maintenance.memory_maintenance_jobs SET enabled = false WHERE job_name = 'purge';`
- **恢复**：同上置 `enabled = true`（`next_run_at` 已过会立即被领走）。
- **优雅停机**：`systemctl stop klonet-memory-maintenance`（SIGTERM）——Worker 只置停止事件，
  当前 Job 跑完才退出；`TimeoutStopSec=90` 要大于最大 Job 周期。

## dry-run / 清理

- `purge --dry-run` 返回待清理规模（条数 / 最老删除时间 / 预计释放字节），不删任何东西。
- 真清理：先人工核对最老样本（阶段 8 的 canary 要求），再 `once --job purge --force`。
- 删除三件套由 `MemoryAdmin` 保证：记录转 deleted + 结束版本有效期 + 清向量与 outbox。

## embedding 模型迁移（reembedding）

```python
from klonet_agent.memory.maintenance.reembedding import EmbeddingMigrationManager

mgr = EmbeddingMigrationManager(database)
mgr.create(migration_id="v2-2026q4", target_profile_id="p2", target_model="...", target_model_version="...")
mgr.start("v2-2026q4")                      # 播种 + backfilling；Worker 的 ReembeddingJob 接管
# ... 队列清空后 Job 自动推进 validating ...
mgr.validate("v2-2026q4", search_fn=..., probes=[...])   # 不达标 → continue_backfill 回炉
mgr.promote("v2-2026q4")                    # 门禁过了才允许；原子切 active profile
mgr.rollback("v2-2026q4")                   # 出问题一个操作切回 default（一个事务）
```

## 健康与告警

- 指标唯一权威通道：`runtime.governance.service.record_run_metric` → `governance.runtime_events`
  （event_type=`run_metric`）。**没有** `memory_maintenance_health_metrics` 表——谁建谁违规。
- 告警判定（`health.collect_snapshot`）：任一 Job `consecutive_failures >= 3`；
  embedding 最老 pending ≥ 900 秒；DSN 配置了但库打不开。
- `scripts/memory_cutover.py --check` 已接通健康门禁：`healthy == false` 非零退出。

## 回滚

删除 Worker 是**重建 schema**，不是原地改：

1. `systemctl disable --now klonet-memory-maintenance`；
2. 删除 `memory/maintenance/` 与迁移 `0006/0007/0008` 对应的三张系统表
   （`memory_maintenance` schema 整体 DROP、`public.memory_embeddings`）；
3. 清 `schema_migrations` 里对应行后重跑 `run_migrations()`（按需保留 0004/0005 治理层）。

**禁止** `ALTER COLUMN vector(N)` 原地改维度——维度变化必须新建 profile + 新表。
