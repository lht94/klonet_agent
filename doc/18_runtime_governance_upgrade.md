# 18 — Agent 运行治理层（03 计划阶段 0–3 实施记录）

> 对应计划：[`通用功能升级计划/03-Agent运行治理升级计划.md`](../通用功能升级计划/03-Agent运行治理升级计划.md)
> 本文记录 2026-10-09 落地的 P0 部分：阶段 0（基线与契约）、阶段 1（运行标识与事件底座 +
> 最小隐私网关）、阶段 2（持久化任务状态）、阶段 3（通用失败协议）。
> 阶段 4–8（证据链、能力路由、完整隐私、统一评估、切换清理）为后续迭代。

## 1. 模块布局

```text
runtime/governance/
  models.py      # DTO、状态机转换表、reason code、SCHEMA_VERSION=1
  privacy.py     # 最小隐私网关：四级分类、secret 拒绝、脱敏、RedactionRecord
  repository.py  # GovernanceRepository 协议 + InMemory 实现（测试/降级缓冲）
  postgres.py    # PostgreSQL 实现：事件 + 投影同一事务
  service.py     # RuntimeGovernance：事务边界、状态转换、fail-closed 降级
  exporters.py   # JsonlEventExporter：JSONL 只是导出，不是权威
  bootstrap.py   # 从环境装配；开关关闭/无 DSN 时返回 None
migrations/
  0004_governance.sql  # governance schema：9 张表、RLS、索引、延迟外键
```

依赖方向（计划 §7）：orchestrator / LLMClient / ToolExecutor → RuntimeGovernance →
PostgreSQL。ContextCompiler 与记忆管线不写治理状态。

## 2. 关键决策

1. **可选部署件**：`KLONET_AGENT_RUNTIME_GOVERNANCE=1` 才启用；与记忆库共用
   `KLONET_AGENT_MEMORY_DSN`（governance 是同一数据库里的独立 schema）。没有 DSN
   时构造返回 `None`，主链路逐字不变——与 `MEMORY_AUTHORITY` 同一哲学。
2. **降级规则（阶段 1 完成标准）**：任务状态改变 / 失败关闭 fail closed
   （`GovernanceUnavailableError`，内存 todos 不会被修改）；模型/工具调用的
   telemetry 事件进缓冲（上限 200 条），`flush_telemetry()` 按幂等键补投。
3. **事件与投影同事务**：`runtime_events` INSERT 与 tasks/steps/failures UPSERT
   在同一个 `tenant_session` 事务里；相关外键 `DEFERRABLE INITIALLY DEFERRED`，
   事件行先写、投影行后写，COMMIT 时一起校验。
4. **幂等**：`event_id` 主键 + `idempotency_key` 唯一；幂等命中返回 False 且
   **不重复应用投影**。todos 的幂等键是 `{run_id}:todo:{id}`，重复提交零事件。
5. **状态机**：Task `pending→running→blocked→running→completed/cancelled`，
   Step `pending→running→succeeded|failed|skipped`，非法转换在服务层拒绝
   （`InvalidStatusTransitionError`），数据库 CHECK 兜底。tasks.version 乐观锁。
6. **失败协议**：活跃失败按 `(scope, error_class)` 复用并累计 attempt_count；
   resolved 必须带解决动作，verified 还要验证证据；verified 生成
   `FailureLessonCandidate`（只挂在内存属性上，交记忆管线审核，不直接进记忆）。
7. **隐私（最小网关，与事件底座同步交付）**：secret（sk- key、AKIA、ghp_、私钥块、
   `password=`、Bearer）→ `SecretRejectedError` fail closed，事件降级为不含原文的
   tombstone；sensitive（IPv4、email）→ 就地脱敏 + RedactionRecord（只存规则、
   类别、内容哈希）。投影字段（failure.message、task.content、tool args preview）
   在服务层统一脱敏。
8. **JSONL 降级为导出**：治理事件同步追加到 `tracing/governance.jsonl`
   （`GOVERNANCE_TRACE_FILE`）；导出失败被吞掉，绝不反向影响权威链路。

## 3. Orchestrator 接入点

| 位置 | 行为 | 级别 |
| --- | --- | --- |
| `single_chat` 开始 | `start_turn()`（首次调用时 `start_run`） | telemetry |
| `chat_with_llm` | `record_model_call(model, tokens, duration)` | telemetry |
| `_execute_tool`（新包装） | `record_tool_call`；异常时 `outcome_unknown` + `record_failure` | telemetry + failure |
| `AgentSession.update_todos` | `session.on_todos_updated` 观察者 → `apply_todos`（变更前执行，异常拒绝更新） | **fail closed** |

`AgentSession.on_todos_updated` 默认 `None`：治理关闭时 `update_todos` 行为逐字不变。

## 4. 验证

- 单元测试 `tests/test_governance_models.py`（28 项）：状态机、隐私、幂等、
  乐观锁、telemetry 缓冲、fail-closed、JSONL 导出。
- 集成测试 `tests/test_governance_postgres.py`（11 项，`KLONET_AGENT_TEST_PG_DSN`
  门控）：9 张表齐建、迁移幂等、事件+投影同事务、重复提交幂等、乐观锁冲突、
  活跃失败唯一、secret 无明文、**RLS 跨租户读为零**、数据库宕机 fail closed。
- 契约（阶段 0）：所有模型调用仍只走 `LLMClient → ProviderRouter`，
  治理层只记录不发起调用。

## 5. 启用方式

```bash
# .env
KLONET_AGENT_RUNTIME_GOVERNANCE=1
KLONET_AGENT_MEMORY_DSN=postgresql://...   # 治理层共用
```

首次启用前执行迁移：
`python -c "from klonet_agent.memory.database import MemoryDatabase; MemoryDatabase.from_env().open().run_migrations()"`

## 6. 后续（未在本轮范围）

- 阶段 4：通用 EvidenceRecord / ClaimRecord / provenance（source adapter、哈希、freshness）；
- 阶段 5：ModelCapabilityRegistry + RoutingPolicy（shadow 先行）；
- 阶段 6：完整隐私（保留期、删除与派生清理、访问审计）；
- 阶段 7：统一 EvaluationRecord 与发布门禁；
- 阶段 8：checkpoint 改从任务投影生成、JSONL 读取路径删除、旧链路清理。
