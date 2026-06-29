"""命令行入口。

从旧版 main.py 迁移到这里，负责启动本地交互式对话。
CLI 只处理输入输出，不承载 agent 的核心业务逻辑。
"""

import sys
from typing import Optional

from klonet_agent.agents import get_profile
from klonet_agent.config import DEFAULT_PROJECT_ID, DEFAULT_USER_ID
from klonet_agent.orchestrator import AgentOrchestrator
from klonet_agent.session import AgentSession


def configure_console_encoding(stdin=None, stdout=None, stderr=None):
    """统一 CLI 编码，管道输入遇到损坏数据时直接报错。"""

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    # 交互式 stdin 继续使用系统控制台编码；管道输入统一要求 UTF-8。
    if (
        hasattr(stdin, "isatty")
        and not stdin.isatty()
        and hasattr(stdin, "reconfigure")
    ):
        stdin.reconfigure(encoding="utf-8", errors="strict")

    for stream in (stdout, stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_piped_prompt(stdin=None) -> Optional[str]:
    """非交互 stdin 一次读取完整问题，避免多行内容被拆成多个回合。"""

    stdin = stdin or sys.stdin
    if not hasattr(stdin, "isatty") or stdin.isatty():
        return None
    return stdin.read().strip()


def clear_interactive_input_line(stdout=None):
    """在支持 ANSI 的交互式终端里清掉刚提交的输入行。"""

    stdout = stdout or sys.stdout
    if not hasattr(stdout, "isatty") or not stdout.isatty():
        return
    stdout.write("\033[A\033[2K")
    stdout.flush()


def run_chat(
    mode: str = "mentor",
    user_id: str = DEFAULT_USER_ID,
    project_id: str = DEFAULT_PROJECT_ID,
    answer_style: str = "default",
):
    """进入命令行对话流程，即旧版 main.py 中的外层 while 循环。"""

    configure_console_encoding()
    print("正在启动 Klonet 专用教学协作 Agent...")
    profile = get_profile(mode)
    session = AgentSession(user_id=user_id, project_id=project_id, mode=profile.name)
    orchestrator = AgentOrchestrator(
        profile=profile,
        session=session,
        answer_style=answer_style,
    )
    history = orchestrator.init_history()
    token = 0
    print(f"Klonet Agent：已进入 {profile.name} 模式。(输入 exit 退出)\n")

    try:
        piped_prompt = read_piped_prompt()
        if piped_prompt is not None:
            if piped_prompt:
                _, history, token = orchestrator.single_chat(
                    piped_prompt,
                    history,
                    token,
                )
            print(f"Klonet Agent：本次累计 token 约 {token}")
            return

        while True:
            user_input = input("用户：").strip()
            clear_interactive_input_line()

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
    except UnicodeDecodeError:
        print(
            "Klonet Agent：stdin 不是有效的 UTF-8。"
            "请将管道输出设置为 UTF-8，或使用 UTF-8 输入文件。"
        )
    except EOFError:
        print(f"Klonet Agent：输入结束，本次累计 token 约 {token}")
    except KeyboardInterrupt:
        # 捕捉 Ctrl+C 强制退出。
        print(f"\nKlonet Agent：会话被中断，本次累计 token 约 {token}")


if __name__ == "__main__":
    run_chat()
