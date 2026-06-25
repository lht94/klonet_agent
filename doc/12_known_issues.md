# 已有问题记录

本文档集中记录 Klonet Agent 在真实使用、测试和维护过程中已经确认的问题。

记录问题时应区分现象、证据、根因、影响和验收标准，避免只记录某一次回答中的具体错误。后续出现同类问题时，应优先补充到已有问题条目，而不是为每个错误回答单独增加临时规则。

## 状态定义

| 状态 | 含义 |
| --- | --- |
| `confirmed` | 已有可复现证据，确认是系统问题 |
| `in_progress` | 已确定修复方案，正在实现 |
| `mitigated` | 已降低风险，但根因尚未完全消除 |
| `resolved` | 修复完成并通过回归测试 |
| `wont_fix` | 明确接受该限制，不计划修复 |

## 问题模板

新增问题时使用以下结构：

```text
问题编号：KI-XXX
问题名称：
分类：
状态：
严重程度：
发现日期：

现象：
证据：
根因：
影响：
修复方向：
验收标准：
相关文件：
```

---

## KI-001：未验证的模型输出污染后续记忆

- 分类：记忆系统设计 / 回答可信度
- 状态：`confirmed`
- 严重程度：高
- 发现日期：2026-06-24

### 现象

用户询问 Klonet 标准启动流程时，Agent 给出了以下命令：

```bash
screen -S <instance>-redis
```

当前标准启动步骤没有要求使用 screen 启动 Redis。知识文档中确认的 Redis 启动方式是统一脚本或直接启动 `redis-server`：

```bash
cd /root/vemu_install_new_gen
sudo bash ./service_begin_both/begin_redis.sh
```

或：

```bash
cd /root/vemu_install_new_gen/install_redis
sudo /usr/local/bin/redis-server redis.conf &
```

知识文档只明确要求 Master、Celery、Web Terminal 和 Worker 等平台进程使用 screen。模型将“Redis 是启动流程的一部分”和“部分平台进程使用 screen”错误组合，生成了没有直接证据的 Redis screen 命令。

该错误回答随后被写入会话历史。后续恢复同一会话时，模型可能把自己之前生成的内容当作已有事实继续引用，造成错误上下文反复污染回答。

### 证据

标准知识文档：

- `knowledge/klonet/ops/startup_shutdown.md` 中“第二步：检查并启动 Redis”。
- Redis 使用 `begin_redis.sh` 或 `redis-server redis.conf &`。
- 同一文档中 Master、Celery、Web Terminal、Worker 的 screen 名使用 `<instance>_master`、`<instance>_celery` 等格式。
- 文档中没有 `screen -S <instance>-redis`，也没有 `<instance>-redis` 这套连字符命名规则。

受污染的会话记录：

- `memory/sessions/default/default/history.jsonl`
- 其中保存了包含 `screen -S <instance>-redis` 的 assistant 回答。

### 根因

根因不是某一条 Redis 规则缺失，而是当前记忆系统没有严格区分不同信息来源的可信度：

1. 用户陈述、工具结果、知识库证据和模型生成内容都以普通消息形式进入上下文。
2. assistant 回答会被持久化到 `history.jsonl`，后续会话恢复时重新注入。
3. 模型生成的内容没有 `verified`、`unverified`、`hypothesis` 等状态。
4. 长期记忆和情景记忆允许模型根据自然语言总结写入，缺少来源验证和冲突检测。
5. 命令、路径、端口、启动顺序等高风险信息没有强制要求直接证据。

因此，一次普通幻觉可能沿以下链路扩大：

```text
模型生成无证据内容
-> assistant 回答写入 history.jsonl
-> 后续会话恢复历史
-> 旧回答被当成上下文事实
-> 模型再次引用和扩展
-> 错误逐渐固化
```

### 影响

该问题不只影响 Redis 启动方式，还可能产生同类错误：

- 发明不存在的 shell 命令或参数。
- 把一个服务的启动方式套用到其他服务。
- 猜测端口、配置路径、screen 名或进程名。
- 把历史服务器的操作方式套用到当前服务器。
- 把重构规划误认为当前实现。
- 把模型推测写入项目日志或长期记忆，形成持续污染。

对于运维命令，这类错误可能导致服务重复启动、错误进程管理、端口冲突或误操作，因此严重程度定为高。

### 修复方向

#### 1. 区分对话历史与可信知识

`history.jsonl` 仅用于会话连续性和审计，不应被视为事实库。恢复历史时：

- 只加载当前 session 最近的有限轮次。
- 不恢复旧 `reasoning_content`。
- assistant 历史应明确标记为未验证对话内容。
- 不允许历史 assistant 回答覆盖当前知识库和工具证据。

#### 2. 为记忆记录增加来源和状态

建议记忆记录至少包含：

```json
{
  "content": "Redis 使用 begin_redis.sh 启动",
  "type": "fact",
  "source": "knowledge/klonet/ops/startup_shutdown.md",
  "status": "verified",
  "confidence": 1.0
}
```

模型自行推断的内容只能保存为 `hypothesis/unverified`，不能作为确定事实注入后续回答。

#### 3. 长期记忆采用候选写入流程

`append_episode` 和 `write_memory` 不应直接把模型总结升级为长期事实。建议流程：

```text
模型提出记忆候选
-> 校验 source_ref
-> 检查来源是否为用户确认、工具结果或知识库证据
-> 执行冲突检测
-> 写入 verified memory
```

无可靠来源的内容应丢弃，或进入待验证候选区。

#### 4. 高风险事实必须有直接证据

以下信息不得根据相似模式补全：

- shell 命令和参数。
- 文件路径。
- 端口和鉴权配置。
- 服务启动、停止顺序。
- API 路由和配置字段。
- 数据库修改操作。

没有直接证据时，Agent 必须说明证据不足，而不是生成看似合理的占位命令。

#### 5. 回答前进行声明校验

最终回答前提取命令、路径、端口和确定性事实，并检查是否能映射到本轮检索证据。无证据声明应删除、降级为假设，或明确标记为不确定。

#### 6. 支持错误事实失效

记忆记录应支持：

```text
verified
unverified
rejected
superseded
expired
```

发现错误后，不只删除历史文本，还应记录拒绝原因和反证，防止相同错误再次被模型生成。

### 验收标准

完成修复后，至少满足以下测试：

1. 用户询问 Klonet 启动方式时，回答不得出现 `screen -S <instance>-redis`。
2. Redis 启动命令必须来自当前知识文档的直接证据。
3. assistant 生成的无来源命令不能进入 verified memory。
4. 新会话不能把历史 assistant 回答当作高可信事实。
5. 当历史回答与当前知识库冲突时，以当前已验证知识为准，并明确指出冲突。
6. 对不存在直接证据的命令，回答必须说明“当前证据不足”。
7. 上述机制应对任意服务、命令、路径和端口生效，而不是只针对 Redis。

### 相关文件

- `memory/store.py`
- `orchestrator.py`
- `prompts.py`
- `journal/project_journal.py`
- `knowledge/klonet/ops/startup_shutdown.md`
- `memory/sessions/default/default/history.jsonl`
- `tests/test_orchestrator_controls.py`
- 后续新增的记忆可信度和回答证据测试

### 当前处置建议

在完整修复落地前：

1. 不要将历史 assistant 回答视为知识来源。
2. 清理或归档已经包含错误命令的测试会话。
3. 对运维命令要求 Agent 给出知识来源。
4. 发现无证据命令时记录到本文档，并补充通用回归测试。

