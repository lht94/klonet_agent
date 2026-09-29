# Klonet 上下文管理专项评测

- cases: 8
- passed: 8/8
- budget: window=32768, max_output=2048, safety=1024

## Token 与行为基线

| case | mode | 旧路径 token | 新路径 token | 节省 | 压缩触发(前) | checkpoint | 覆盖已扣减 | 当前输入保留 | 硬拒绝 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ctx_mentor_long_tool_result | mentor | 22818 | 2291 | 90.0% | 是 | 是 | 是 | 是 | 否 |
| ctx_mentor_multi_tool_calls | mentor | 22814 | 2289 | 90.0% | 是 | 是 | 是 | 是 | 否 |
| ctx_interrupted_tool_chain | mentor | 22818 | 2288 | 90.0% | 是 | 是 | 是 | 是 | 否 |
| ctx_coding_long_single_input | coding | 22205 | 1841 | 91.7% | 是 | 是 | 是 | 是 | 否 |
| ctx_ops_runtime_dialogue | ops | 19754 | 2277 | 88.5% | 是 | 是 | 是 | 是 | 否 |
| ctx_hard_overflow | mentor | 42219 | 0 | - | 是 | 否 | - | - | 是 |
| ctx_trivial_history_no_compaction | mentor | 2226 | 2226 | 0.0% | 否 | 否 | 是 | 是 | 否 |
| ctx_restart_recovery_after_checkpoint | mentor | 22824 | 2288 | 90.0% | 是 | 是 | 是 | 是 | 否 |

## 逐条明细

### ctx_mentor_long_tool_result
- 说明：单个工具结果非常长：验证完整消息组不被拆开，超出部分由 checkpoint 覆盖
- 结果：PASS
- hard/soft limit：27702 / 19391
- 分区 token：{"system": 2189, "checkpoint": 85, "transient": 0, "current_input": 17, "evidence": 0, "recent_history": 0, "tools": 1994}
- checkpoint 区间：rows-0..rows-3
- 是否调用供应商：是
- 进入请求的消息数：11
- event_id 是否泄漏进请求：否
- 重启恢复事件数：None

### ctx_mentor_multi_tool_calls
- 说明：一条 assistant 消息带两个 tool calls：验证两个 tool 结果与调用方同组保留
- 结果：PASS
- hard/soft limit：27702 / 19391
- 分区 token：{"system": 2189, "checkpoint": 85, "transient": 0, "current_input": 15, "evidence": 0, "recent_history": 0, "tools": 1994}
- checkpoint 区间：rows-0..rows-4
- 是否调用供应商：是
- 进入请求的消息数：11
- event_id 是否泄漏进请求：否
- 重启恢复事件数：None

### ctx_interrupted_tool_chain
- 说明：助手发起了工具调用但没有结果（中断）：验证残片被清洗且不产生孤立 tool 消息
- 结果：PASS
- hard/soft limit：27702 / 19391
- 分区 token：{"system": 2189, "checkpoint": 85, "transient": 0, "current_input": 14, "evidence": 0, "recent_history": 0, "tools": 1994}
- checkpoint 区间：rows-0..rows-4
- 是否调用供应商：是
- 进入请求的消息数：11
- event_id 是否泄漏进请求：否
- 重启恢复事件数：None

### ctx_coding_long_single_input
- 说明：单轮用户输入极长：验证必需区不被裁剪、当前输入 100% 保留
- 结果：PASS
- hard/soft limit：26863 / 18804
- 分区 token：{"system": 1706, "checkpoint": 85, "transient": 0, "current_input": 50, "evidence": 0, "recent_history": 0, "tools": 2833}
- checkpoint 区间：rows-0..rows-2
- 是否调用供应商：是
- 进入请求的消息数：11
- event_id 是否泄漏进请求：否
- 重启恢复事件数：None

### ctx_ops_runtime_dialogue
- 说明：Ops 模式中等长度运行态对话：验证工具循环历史可与 checkpoint 共存
- 结果：PASS
- hard/soft limit：24055 / 16838
- 分区 token：{"system": 2175, "checkpoint": 85, "transient": 0, "current_input": 17, "evidence": 0, "recent_history": 0, "tools": 5641}
- checkpoint 区间：rows-0..rows-4
- 是否调用供应商：是
- 进入请求的消息数：11
- event_id 是否泄漏进请求：否
- 重启恢复事件数：None

### ctx_hard_overflow
- 说明：必需区（系统规则 + 当前输入）超过硬预算：必须本地拒绝且不调用供应商
- 结果：PASS
- hard/soft limit：27702 / 19391
- 分区 token：{"system": 42200, "checkpoint": 0, "transient": 0, "current_input": 19, "evidence": 0, "recent_history": 0, "tools": 1994}
- checkpoint 区间：None
- 是否调用供应商：否
- 进入请求的消息数：0
- event_id 是否泄漏进请求：否
- 重启恢复事件数：None

### ctx_trivial_history_no_compaction
- 说明：历史很短且未超过软阈值：不产生 checkpoint，避免无意义压缩调用
- 结果：PASS
- hard/soft limit：27702 / 19391
- 分区 token：{"system": 2189, "checkpoint": 0, "transient": 0, "current_input": 16, "evidence": 0, "recent_history": 21, "tools": 1994}
- checkpoint 区间：None
- 是否调用供应商：是
- 进入请求的消息数：12
- event_id 是否泄漏进请求：否
- 重启恢复事件数：None

### ctx_restart_recovery_after_checkpoint
- 说明：重启恢复：checkpoint 之后的事件全部恢复（未覆盖的最新用户输入 + 追加的 80 条 = 81），不再被固定 20 条截断
- 结果：PASS
- hard/soft limit：27702 / 19391
- 分区 token：{"system": 2189, "checkpoint": 85, "transient": 0, "current_input": 14, "evidence": 0, "recent_history": 0, "tools": 1994}
- checkpoint 区间：rows-0..rows-2
- 是否调用供应商：是
- 进入请求的消息数：11
- event_id 是否泄漏进请求：否
- 重启恢复事件数：81

