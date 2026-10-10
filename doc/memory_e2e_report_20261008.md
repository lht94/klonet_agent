# 记忆系统端到端实测报告（2026-10-08）

> 在远端测试服务器 192.168.1.33 上启动真实程序，用真实模型（deepseek-v4-flash，经中转站）
> 模拟用户多轮对话，验证记忆系统（02 计划阶段 6/7）在真实链路上的表现。
> 代码版本：`874c678`（阶段 7 完成提交）。`.env` 未做任何改动，全部配置走 shell 环境变量。

## 1. 测试环境

| 项 | 值 |
| :--- | :--- |
| 程序 | `python agent.py --mode mentor --user-id <U> --project-id <P>` |
| 多轮模拟 | 管道 stdin 每次一轮（`printf '…' \| python agent.py …`），会话状态由 `memory/sessions/<U>/<P>/history.jsonl` 跨进程持久 → 同时验证了**跨进程重启的记忆持久化** |
| 记忆库 | 专用库 `klonet_memory_e2e`（迁移 0001/0002/0003 已应用） |
| 应用角色 | `klonet_app`（**非超管**，RLS 真实生效；密码已重置为 `klonet-e2e-2026`） |
| 开关 | `KLONET_AGENT_MEMORY_DSN` + `KLONET_AGENT_MEMORY_WRITE_PIPELINE=1` + `KLONET_AGENT_MEMORY_PACK=1` |
| 模型 | 聊天 `deepseek-v4-flash`（中转站，直连）；嵌入 `text-embedding-v4`（**403 未购买，见 §4**） |

## 2. 七轮真实对话与结果

| 轮 | 模拟用户动作 | 结果 |
| :--- | :--- | :--- |
| 1 | 告知：平台部署在 192.168.1.33，Web 端口 18080 | **自动写入 2 条 fact**，来源 `rows-0`/`rows-1`，置信度 0.95，outbox 登记 2 条 |
| 2 | 告知：用中文回答、先结论后理由 | 模型尝试旧覆写工具被**正确拒绝**并向用户解释了受控写入机制；管线写入 1 条 `preference` |
| 3 | 告知：值班负责人张工、值班室实验楼 302 | 写入 2 条 fact |
| 4 | **新进程**问"Web 端口是多少" | 正确答出 18080，且自述"本轮检索到的相关记忆里也有对应条目"；trace：`memory_pack_injected`，2 条 / 173 token |
| 5 | **新进程**问"值班负责人是谁" | 正确答出张工 / 302；注入 2 条 / 145 token |
| 6 | **另一个用户**问同一问题 | `memory_pack_empty`（该租户无记忆）→ **跨用户隔离在真实对话中成立**，且没有把 18080 说成"我们平台"的值 |
| 7 | 删除端口记忆后再问（同会话） | **记忆包不再包含已删 id**（只注入部署主机两条）；回答中的 18080 来自会话历史，模型自己说明了来源 |

## 3. 删除权的三层证据

1. **检索层**：`MemoryAdmin.recall_check()` 删除前 `True` → 删除后 `False`；
2. **存储层**：状态 `deleted`、`valid_to` 落值（`2026-10-08T14:00:59Z`）、向量与 outbox 清空；
3. **注入层**：删除后那一轮的 `memory_pack_injected` payload 里只有部署主机两条记忆，**没有**被删的 `80f0a7b1…`。

## 4. 实测暴露的问题（都是真实链路才有的事实）

1. **嵌入服务不可用（需处理）**：远端嵌入端点（阿里云专属实例）返回
   `403 AccessDenied.Unpurchased`，中转站也不支持 embeddings → 语义通道全程降级。
   系统行为符合设计：每轮 trace 记录 `degraded: ["embed_failed:EmbeddingUnavailable"]`，
   全文 + 精确通道照常工作，**没有任何一次失败外溢到对话**。
   → 要开启语义召回，需在专属实例上开通 `text-embedding-v4`，或更换
   `.env` 的 `DEFAULT_EMBEDDING_MODEL / DEFAULT_EMBEDDING_BASE_URL / EMBEDDING_API_KEY`。
2. **rerank 同样降级**：`rerank: "fallback:BadRequestError"`（中转站无 rerank 端点），
   回退到召回顺序，不影响可用性。
3. **重复记忆（改进项）**：同一事实被不同轮次提取成不同 subject——
   `fact:project:klonet:deployment_host` vs `…:deploy_host_ip`、
   `fact:project:运维值班:负责人` vs `fact:user:user:ops_duty_lead`（还跨了作用域）。
   去重目前依赖 subject 精确匹配，**同义不同名的 subject 会各自成条**。
   这是计划 §10「重复记忆率」指标的真实样例，后续可考虑 subject 别名归一或语义去重。
4. **每轮回答都会再进候选提取**：模型复述事实会产生新候选（部分 NOOP 合并，部分成新条）。
   候选接受率 / 重复率需要在真实会话上统计，合成评测用例测不出这一层。

## 5. 结论

- **写入 → 存储 → 跨进程召回 → 注入 → 删除** 的完整闭环在真实模型 + 真实数据库 + 真实 RLS 角色下全部可用；
- 受控写入对旧覆写工具的拦截与用户解释行为正确；
- 跨用户隔离与删除权这两条硬性完成标准在真实对话中成立；
- 语义通道与 rerank 的两条降级路径都被**可见地**触发并兜底，系统未受影响。

## 6. 清理方式（如需）

```bash
python agent.py admin delete-user --user-id e2e-mem-user --yes
python agent.py admin delete-user --user-id e2e-other-user --yes
psql "postgresql://postgres:klonet@127.0.0.1:5432/postgres" -c "DROP DATABASE klonet_memory_e2e;"
```
