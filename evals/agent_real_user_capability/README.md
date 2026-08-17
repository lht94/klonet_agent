# Agent 真实用户需求能力评测

`cases.json` 与 `docs/ops/agent_real_user_capability_test_checklist.md` 的 35 项场景一一对应。

评测分为两层：

1. **隔离合同验收**：使用隔离文件、进程和端口夹具运行真实生产代码，覆盖不能安全注入真实服务器的故障与变更。
2. **真实会话验收**：对只读查询和停在审批边界的场景，启动真实 `ops-privilege` CLI、RAG、LLM 和服务器探针，保存原始回答。

运行全部隔离合同：

```bash
python3.8 evals/agent_real_user_capability/run_eval.py \
  --output-dir /tmp/agent-real-capability
```

同时运行安全的真实会话：

```bash
python3.8 evals/agent_real_user_capability/run_eval.py \
  --live \
  --output-dir /tmp/agent-real-capability
```

评测器不会发送 `confirm-priv-plan`，因此真实会话不执行变更。
