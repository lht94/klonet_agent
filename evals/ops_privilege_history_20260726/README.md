# Ops-Privilege 历史场景评测

本目录保存从两套服务器会话历史中归纳出的脱敏评测集，用于验证
Ops-Privilege 自适应 PEV 工作流的路由、风险分级、确认边界和真实 Planner
表现。

## 数据来源

- `lzl@192.168.1.33:/home/lzl/klonet_agent/memory/sessions/**/history.jsonl`
- `klonet-agent@192.168.1.33:/home/klonet-agent/klonet_agent/memory/sessions/**/history.jsonl`

仓库中不保存原始会话。`cases.jsonl` 只保留归纳、脱敏后的意图，IP、用户目录、
仓库地址、密码和 token 均不从原始记录复制。

## 评测层次

1. `deterministic`：全部用例都检查实际路由和规则风险分级。
2. `live`：标记了 `live: true` 的安全子集调用真实 Planner/Verifier。
3. 变更命令只运行到计划确认边界；评测不会自动发送 `confirm-priv`。
4. 只读命令允许真实执行。若错误地尝试执行变更命令，安全执行器会拦截并记录。

## 运行

从仓库根目录的父目录运行：

```bash
PYTHONPATH=/home/klonet-agent \
python3 -X utf8 klonet_agent/evals/ops_privilege_history_20260726/run_eval.py \
  --cases klonet_agent/evals/ops_privilege_history_20260726/cases.jsonl \
  --output /tmp/ops_privilege_eval_results.jsonl
```

只运行确定性检查：

```bash
PYTHONPATH=/home/klonet-agent \
python3 -X utf8 klonet_agent/evals/ops_privilege_history_20260726/run_eval.py \
  --deterministic-only \
  --cases klonet_agent/evals/ops_privilege_history_20260726/cases.jsonl \
  --output /tmp/ops_privilege_eval_results.jsonl
```
