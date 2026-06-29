"""klonet_agent 的运行配置。

这里集中放模型名称、token 限制、工作区路径、记忆路径、RAG 开关等全局配置。
不要在业务模块里散落硬编码配置，后续部署到服务器时也更方便从环境变量或配置文件读取。
"""

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT

DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
DEFAULT_EMBEDDING_BASE_URL = "https://ws-o108vxrjw8kdvbrm.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_REASONING_EFFORT = "medium"
MAX_TOKEN = 500000
HISTORY_MAX_MESSAGES = 20
MAX_TOOL_ROUNDS = 8
OPS_MAX_TOOL_ROUNDS = 16
SHARED_OPS_MEMORY_RECENT_DAYS = 3
SHARED_OPS_MEMORY_SEARCH_LIMIT = 5
MAX_TODO_CONTINUATIONS = 1
DEFAULT_RAG_TOP_K = 3
RAG_SEARCH_BUDGETS = {
    "general": 1,
    "klonet": 2,
    "mixed": 2,
}

MEMORY_DIR = PROJECT_ROOT / "memory"
JOURNAL_DIR = PROJECT_ROOT / "journals"
WORKSPACE_DIR = PROJECT_ROOT / "workspaces"
KNOWLEDGE_INDEX_FILE = PROJECT_ROOT / "knowledge" / "index.jsonl"
KNOWLEDGE_VECTOR_INDEX_FILE = PROJECT_ROOT / "knowledge" / "vectors.jsonl"
TRACE_FILE = PROJECT_ROOT / "tracing" / "trace.jsonl"
KLONET_SOURCE_ROOT = PROJECT_ROOT / "klonet_knowledge" / "02_vemu_uestc_code"

DEFAULT_USER_ID = "default"
DEFAULT_PROJECT_ID = "default"
DEFAULT_MODE = "mentor"
