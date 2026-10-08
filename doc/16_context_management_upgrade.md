# 16. 上下文管理升级说明

> 状态：已实施并部署（2026-09-29 完成，10-08 整理成文）
> 关联提交：`7017017`（主体）、`b62541c`（reasoning_effort 可配置）、`86a8666`（请求转储调试开关）
> 关联计划：`docs/superpowers/plans/2026-09-29-context-management-upgrade.md`

## 1. 背景与问题

升级前，上下文管理存在四个结构性问题：

1. **静态 token 上限**：全局 `MAX_TOKEN = 500000` 不区分模型，未知模型可能把必超限的请求发出去，靠供应商报错后"事后补救"。
2. **按条数裁剪**：`HISTORY_MAX_MESSAGES = 20` 按"最近 20 条"截断，会把 assistant 的 tool_calls 和对应 tool result 拆开，产生 OpenAI 协议不合法的残缺交换（依赖 `sanitize_openai_tool_history` 兜底丢消息）。
3. **压缩即自由文本**：旧 `compress_memory` 用"记忆复盘"式自然语言总结，格式不受约束，关键结论可能静默丢失，且不可校验、不可回退。
4. **恢复不可靠**：重启后按 `compact_event` 标记 + 最近 N 条恢复，检查点与原始事件之间没有可靠的覆盖边界。

## 2. 设计原则

- **调用前编译，而不是失败后补救**：每次模型调用前按当前模型的预算组装上下文，硬预算溢出在本地确定性拒绝（`ContextOverflowError`），不发必败请求。
- **原始事件不可变**：`history.jsonl` 持续追加；编译和压缩都只产生派生视图，不改写、不删除原始事件。
- **裁剪单位是语义完整组**：user 回合、tool_calls 与全部 tool results 整组保留或整组淘汰，绝不产生协议残片。
- **结构化检查点**：压缩产物是固定 schema 的 `TaskCheckpoint`，可校验、可版本化、损坏可回退。
- **旧路径可回退**：旧 `compress_memory` 路径保留，一个环境变量即可回退，观察一个版本周期后再删。

## 3. 新增模块与架构

```text
原始事件持续写入 history.jsonl（不可变）
    │
    ▼  每次模型调用前
┌─────────────────────────────────────────────────┐
│ context/budget.py      模型感知预算              │
│   ModelContextProfile / ContextBudget            │
│   已知模型: gemini-3.7-flash, glm-5.2            │
│   未知模型回退保守默认(65536)，env 可覆盖         │
│                                                  │
│ context/message_groups.py  完整消息组             │
│   按回合/工具交换分组，从新到旧选择               │
│                                                  │
│ context/compiler.py    ContextCompiler           │
│   必需区(system+checkpoint+transient+当前输入)    │
│   → 证据区(RAG/摘要，独立预算 25%)                │
│   → 最近历史区(剩余预算内的完整组)                │
│   硬限溢出: 本地拒绝; 软限溢出: 请求压缩          │
└─────────────────────────────────────────────────┘
    │ compiled.messages（带 areas/included/omitted 元数据）
    ▼
LLMClient.complete(...)   ← trace: context_compile 事件

超过软阈值时（每次请求最多一次）:
    history ──► memory/compactor.py ──► TaskCheckpoint（17 字段 schema，
              LLM 输出 JSON → 校验 → 最多一次修复 → 失败回退旧 checkpoint）
              └─► memory/checkpoint_store.py（checkpoints/checkpoint-vN.json，
                  临时文件 + os.replace 原子写，损坏自动回退上一版）
              └─► 携带 checkpoint 的 system 消息重新编译后发送

重启恢复:
    init_history → _load_recovered_history()
      checkpoint.source_event_end="rows-N" → MemoryStore.load_history_after(N)
      checkpoint 缺失/损坏 → 回退旧 compact_event 标记逻辑
```

新增文件：

| 文件 | 职责 |
| --- | --- |
| `context/tokens.py` | CJK 启发式 token 估算（多供应商无公开 tokenizer，估算保守） |
| `context/budget.py` | `ModelContextProfile` / `ContextBudget` / `build_context_budget` |
| `context/message_groups.py` | `parse_message_groups` / `select_recent_groups` |
| `context/compiler.py` | `ContextCompiler` / `ContextRequest` / `CompiledContext` / `ContextOverflowError` |
| `memory/models.py` | `TaskCheckpoint`（schema、校验、system 消息渲染） |
| `memory/checkpoint_store.py` | 版本化原子存储 |
| `memory/compactor.py` | `MemoryCompactor`（结构化压缩 + 一次修复） |

修改的既有文件：`orchestrator.py`（编译接入、软压缩触发、恢复逻辑）、`memory/store.py`（`count_history_rows` / `load_history_after`）、`tracing/logger.py`（`record_context_compile`）、`config.py`（开关与 reasoning_effort 配置）、`tests/test_python38_compat.py`（守卫扩展到 `context/` 包）。

## 4. 行为变化（对使用者可见的）

1. **请求必不超限**：编译后估算 token 不会超过该模型的 hard input limit；必需区本身超限时本地报错，不再打到供应商。
2. **工具链不再残缺**：tool_calls 与 tool results 以整组为单位被保留或淘汰。
3. **压缩变成检查点**：超过软阈值时生成结构化检查点并在 console 打印 `已生成任务检查点 vN`；检查点同时写入 `memory/checkpoints/`。
4. **重启恢复更准**：从 checkpoint 覆盖边界之后的事件恢复，而不是笼统的"最近 N 条"。
5. **可观测性**：`trace.jsonl` 每次调用新增 `context_compile` 事件，含估算 token、hard/soft 限、各区域占用（system/checkpoint/evidence/recent_history/tools）、淘汰组数。

## 5. 配置项

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `KLONET_AGENT_DISABLE_CONTEXT_COMPILER` | `0`（启用新路径） | 设为 `1` 回退旧的压缩路径 |
| `KLONET_AGENT_CONTEXT_WINDOW` | 按模型 | 覆盖窗口大小 |
| `KLONET_AGENT_MAX_OUTPUT_TOKENS` | 按模型 | 覆盖输出预留 |
| `KLONET_AGENT_SAFETY_MARGIN_TOKENS` | `8192` | 安全余量 |
| `KLONET_AGENT_SOFT_LIMIT_RATIO` | `0.70` | 软阈值比例（hard 的占比） |
| `KLONET_AGENT_REASONING_EFFORT` | `medium` | 设为 `off/none/空` 则完全不发送 `reasoning_effort` 参数（中转站 Gemini 上游不支持该参数，会回 500） |
| `KLONET_DEBUG_DUMP_REQUEST` | 未设置 | 设为文件路径时导出最终请求体，用于离线重放定位供应商问题 |

预算构成：`hard_input_limit = context_window − reserved_output − tool_schema_tokens − safety_margin`；软阈值 = hard × ratio。必需区自身超过 hard 时抛 `ContextOverflowError`。

## 6. 部署记录（vemu25_test3 / lht_ubuntu3）

1. 宿主机 KVM，目标 VM 为 `lht_ubuntu3`（virbr0 `192.128.122.101`，宿主机 socat `1012→.101:22`），SSH `lzl` 登录。
2. `git pull origin master` 到 `86a8666`。
3. `.env`：`PARATERA_BASE_URL/MODEL/API_KEY_1` 改为与 `CHAT_LLM_*` 一致，锁定中转站 api.yyds168.net（绕开官方额度）；`KLONET_AGENT_REASONING_EFFORT=off`。备份在 `.env.bak-20260929-pre-relay`。
4. 中转站国产模型横评（stream+tools）：`deepseek-v4-flash` 5.0s ✅（选用）、`glm-5.3-flash` 10.8s ✅、`qwen3.8-flash` 18.6s ✅。`CHAT_LLM_MODEL`/`PARATERA_MODEL` 均为 `deepseek-v4-flash`。
5. VM 的 `~/.bashrc` 第 7-8 行 export 的 `127.0.0.1:7891` 代理指向未运行的 clash，会导致 openai SDK 连接被拒——**运行 agent 需 unset 五个 proxy 变量，或删除这两行**（待办）。

## 7. 验证结果

- **新增 52 项测试全绿**：`test_context_baseline` / `test_context_budget` / `test_context_message_groups` / `test_context_compiler` / `test_memory_compactor` / `test_checkpoint_store` / `test_context_recovery`，覆盖四类基线轨迹（长工具结果、多工具调用、中断工具链、超长用户输入）。
- **本地全量回归**：开编译器 213 failed / 1306 passed，关编译器 212 failed / 1307 passed——差值恰为依赖编译器的软压缩测试；其余失败均为 privileged/ops/readonly 的 Windows 存量问题（`WinError 193` 等），与升级无关。
- **服务器全量回归**：1515 passed / 4 failed（2 个 `test_llm_provider` 是 `.env` 改动的已知副作用；2 个 `test_orchestrator_controls` 为全量跑测试间污染，单独跑 41 项全过）。
- **端到端实测**：mentor 模式管道输入提问，deepseek-v4-flash 正确回答，0 报错，7646 token；`trace.jsonl` 记录 `context_compile` 事件（est≈2.9k / hard≈957k / areas: system 2094 + tools 1994 + recent_history 813）。

## 8. 已知问题与注意事项

1. **中转站上游间歇性 500**：同请求重试即可成功（客户端有重试机制），属中转站自身不稳定，与本项目无关。
2. **Agent 沙箱跑 pytest 会假报错**：沙箱删除防护在 basetemp 累积超阈值后抛 `SystemExit`，表现为大量 `ERROR at setup`；真实终端不受影响，验证时可加 `--basetemp=<新目录>`。
3. **Python 3.8 兼容约束**：`context/` 包已纳入 `test_python38_compat` 守卫——运行时代码不能写模块级现代类型别名（`X = Callable[[list[dict]], ...]`）、不能用 `str.removeprefix`。
4. **本机 `.git` 曾被 OneDrive 同步损坏**（对象丢失报 `bad object HEAD`）；已通过 `git fetch` 恢复。本机 push 需用 `git -c credential.helper= -c credential.helper=manager push` 绕过挂死的 helper-selector。

## 9. 回滚

```bash
# 服务器上回退到升级前行为（不改代码）：
echo "KLONET_AGENT_DISABLE_CONTEXT_COMPILER=1" >> .env   # 旧压缩路径接管
# 彻底回滚代码：
git checkout 8be0db9 -- .   # 升级前提交
```

## 10. 后续计划

- [ ] 阶段 6 收尾：`evals/run_context_management_eval.py` 新旧路径 A/B 任务恢复评测（需在服务器跑真实模型基线）
- [ ] 观察一个版本周期后删除旧 `compress_memory` 路径与 `MAX_TOKEN` 后置检查
- [ ] 清理 VM `~/.bashrc` 的死代理配置
- [ ] 记忆库（PostgreSQL + pgvector）阶段 2 接入：checkpoint 迁移到 `memory_checkpoints` 表
