# 18 — Agent 运行治理层（03 计划阶段 0–5 实施记录）

> 对应计划：[`通用功能升级计划/03-Agent运行治理升级计划.md`](../通用功能升级计划/03-Agent运行治理升级计划.md)
> 本文记录 2026-10-09 落地部分：阶段 0（基线与契约）、阶段 1（运行标识与事件底座 +
> 最小隐私网关）、阶段 2（持久化任务状态）、阶段 3（通用失败协议）、
> 阶段 4（证据与 provenance）、阶段 5 的确定性路由核心（shadow-only）。
> 阶段 6–8（完整隐私、统一评估、切换清理）为后续迭代。

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

## 6. 阶段 4：证据与 provenance（2026-10-09 第二批）

- 新增 `runtime/governance/provenance.py`：`EvidenceRecord`（观察）/ `ClaimRecord`（主张）
  严格分离，`claim_evidence` 多对多（supports/contradicts/uncertain）；
  `content_hash` 内容寻址、`refresh_freshness`（源哈希变化或超 TTL → stale）、
  `detect_conflict`（同主体同类型 fresh 观察不同 → `EvidenceConflict`）；
  `claim_evidence_gate` 是记忆候选的最小证据门槛（≥1 条 supports）；
  `ops_evidence_to_governance` 兼容 adapter（Ops 权威不动，只映射）。
- 新增迁移 `0005_governance_provenance.sql`：`governance.evidence / claims /
  claim_evidence / route_decisions`，RLS 与延迟外键同 0004 约定。
- `evidence_postgres.py` 以 mixin 挂进 `PostgresGovernanceRepository`；事件与
  投影仍同事务，幂等命中不重复应用投影。
- 服务层：`record_evidence`（冲突自动转 `claims.status='contradicted'` 并挂双证据）、
  `create_claim`、`mark_evidence_stale`、`evidence_sufficient`。
- orchestrator：`_execute_tool` 成功结果登记为 evidence（哈希 + 观察预览）。

## 7. 阶段 5：能力注册与确定性路由（shadow-only）

- `capabilities.py`：`ModelCapability`（固定版本声明：窗口/能力/价格/延迟/健康度/
  数据驻留上限）、`ModelCapabilityRegistry`、`default_registry()`（来自部署配置，
  去重同模型多入口）。
- `routing_policy.py`：`TaskRequirements` 硬约束过滤（capability/context/cost/
  latency/privacy 各有 reason code）→ 确定性评分 quality 0.40 + health 0.25 +
  cost 0.20 + latency 0.15（quality 用"有无冻结 eval 基线"作保守代理：有 1.0 / 无 0.5）。
- **只做 shadow**：`chat_with_llm` 记录「策略选择 vs 实际模型」到
  `governance.route_decisions`，不控制生产流量；学习型路由器暂不实现。
- 隐私约束进入硬过滤：`privacy_max_class` 超出模型 `max_privacy_class` 的直接剔除；
  secret 永远不进模型（隐私网关拒绝）。

## 8. 后续（未在本轮范围）

- 阶段 5 收尾：shadow 对比报告与 canary、eval 基线回填注册表；
- 阶段 6：完整隐私（保留期、删除与派生清理、访问审计）；
- 阶段 7：统一 EvaluationRecord 与发布门禁；
- 阶段 8：checkpoint 改从任务投影生成、JSONL 读取路径删除、旧链路清理。
