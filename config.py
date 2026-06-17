"""klonet_agent 的运行配置。

这里集中放模型名称、token 限制、工作区路径、记忆路径、RAG 开关等全局配置。
不要在业务模块里散落硬编码配置，后续部署到服务器时也更方便从环境变量或配置文件读取。
"""

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT

DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_REASONING_EFFORT = "medium"
MAX_TOKEN = 500000

MEMORY_DIR = PROJECT_ROOT / "memory"
JOURNAL_DIR = PROJECT_ROOT / "journals"
WORKSPACE_DIR = PROJECT_ROOT / "workspaces"
KNOWLEDGE_INDEX_FILE = PROJECT_ROOT / "knowledge" / "index.jsonl"
TRACE_FILE = PROJECT_ROOT / "tracing" / "trace.jsonl"

DEFAULT_USER_ID = "default"
DEFAULT_PROJECT_ID = "default"
DEFAULT_MODE = "mentor"
