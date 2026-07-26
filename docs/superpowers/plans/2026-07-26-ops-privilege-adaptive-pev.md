# Ops-Privilege 自适应 PEV 实施计划

1. 定义 `PrivilegedPlan`、`PrivilegedStep`、`ExecutionEvidence`、
   `CheckResult` 和 `VerificationDecision`，固化状态机及内容哈希。
2. 实现会话隔离、原子写入的计划存储；重启时把活动步骤恢复为
   `execution_unknown`。
3. 实现确定性风险分类、硬拒绝规则和分级审批策略。
4. 实现非模型可见 Executor，继承 stdin、实时输出、有界捕获，超时不重试。
5. 实现 Checker Registry、已知命令族检查推断和前后置条件校验。
6. 实现无工具 Planner 与无工具 Verifier，并用确定性结果门控模型结论。
7. 实现 Supervisor 的提交、确认、逐步执行、重规划、恢复、中止、查询与审计。
8. 从 profile、工具 schema 和 ToolExecutor 中移除原始高权限 Shell 逃逸路径，
   在 Orchestrator 通用工具循环前接管高权限目标。
9. 更新提示词、README、设计说明和简历表述。
10. 运行功能测试、Python 3.8 兼容检查、既有定向回归和完整测试套件；新增失败不得
    超出实施前记录的七项基线失败。
