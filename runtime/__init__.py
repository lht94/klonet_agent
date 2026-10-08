"""Agent 运行治理层（通用功能升级计划 03，阶段 1–3 的 P0 核心）。

模块边界（见计划 §7）：

- ``models``      运行 DTO、状态机转换表、reason code，不含业务编排；
- ``privacy``     最小隐私网关：分类、secret 拒绝、脱敏（与事件底座同步交付）；
- ``repository``  存储协议 + 内存实现（测试与降级缓冲用）；
- ``postgres``    PostgreSQL 实现：事件账本与当前投影同一事务提交；
- ``service``     RuntimeGovernance 事务边界与状态转换服务；
- ``exporters``   JSONL 事件导出（trace 降级为导出视图，不再是权威事实源）。

依赖方向（计划 §7，禁止反向）：orchestrator / LLMClient / Tool executor
→ RuntimeGovernance → PostgreSQL；ContextCompiler 与记忆管线只读受控视图。
"""
