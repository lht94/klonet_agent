"""服务端入口。

未来部署到服务器时可以在这里接入 FastAPI、Flask 或任务队列。
这一层负责接收用户请求、创建会话和返回结果，具体 agent 行为仍交给 orchestrator。
"""
