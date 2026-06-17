"""命令行入口。

从旧版 main.py 迁移到这里，负责启动本地交互式对话。
CLI 只处理输入输出，不承载 agent 的核心业务逻辑。
"""

from klonet_agent.agents import get_profile
from klonet_agent.config import DEFAULT_PROJECT_ID, DEFAULT_USER_ID
from klonet_agent.orchestrator import AgentOrchestrator
from klonet_agent.session import AgentSession


def run_chat(mode: str = "mentor", user_id: str = DEFAULT_USER_ID, project_id: str = DEFAULT_PROJECT_ID):
    """进入命令行对话流程，即旧版 main.py 中的外层 while 循环。"""

    print("正在启动 Klonet 专用教学协作 Agent...")
    profile = get_profile(mode)
    session = AgentSession(user_id=user_id, project_id=project_id, mode=profile.name)
    orchestrator = AgentOrchestrator(profile=profile, session=session)
    history = orchestrator.init_history()
    token = 0
    print(f"Klonet Agent：已进入 {profile.name} 模式。(输入 exit 退出)\n")

    try:
        while True:
            user_input = input("小白：").strip()

            # 处理空输入。
            if not user_input:
                continue

            # 处理退出逻辑。
            if user_input == "exit":
                print("Klonet Agent：本次会话结束。")
                print(f"Klonet Agent：本次累计 token 约 {token}")
                break

            _, history, token = orchestrator.single_chat(user_input, history, token)
            print("")
    except KeyboardInterrupt:
        # 捕捉 Ctrl+C 强制退出。
        print(f"\nKlonet Agent：会话被中断，本次累计 token 约 {token}")


if __name__ == "__main__":
    run_chat()
