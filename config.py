"""klonet_agent 的运行配置。

这里集中放模型名称、token 限制、工作区路径、记忆路径、RAG 开关等全局配置。
不要在业务模块里散落硬编码配置，后续部署到服务器时也更方便从环境变量或配置文件读取。
"""

from pathlib import Path
import os


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None

if load_dotenv is not None:
    # Do not depend on the caller's current working directory.  The CLI and
    # systemd service start from different directories in production.
    load_dotenv(PACKAGE_ROOT / ".env")

CHAT_LLM_BASE_URL = os.getenv(
    "CHAT_LLM_BASE_URL", "https://api.yyds168.net/v1",
).strip().rstrip("/")
CHAT_LLM_MODEL = os.getenv("CHAT_LLM_MODEL", "gemini-3.7-flash").strip()
CHAT_LLM_API_KEY_ENV = "CHAT_LLM_API_KEY"
CHAT_LLM_MIN_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("CHAT_LLM_MIN_TIMEOUT_SECONDS", "90")),
)
DEFAULT_MODEL = CHAT_LLM_MODEL
DEFAULT_BASE_URL = CHAT_LLM_BASE_URL
DEFAULT_EMBEDDING_MODEL = os.getenv(
    "DEFAULT_EMBEDDING_MODEL",
    "text-embedding-v4",
)
DEFAULT_EMBEDDING_BASE_URL = os.getenv(
    "DEFAULT_EMBEDDING_BASE_URL",
    "https://ws-o108vxrjw8kdvbrm.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)
# 推理力度：部分中转站的 Gemini 上游不支持 reasoning_effort 参数（会回 500），
# 可通过 KLONET_AGENT_REASONING_EFFORT=off/none/空 关闭该参数的发送。
_reasoning_effort_env = os.getenv(
    "KLONET_AGENT_REASONING_EFFORT", "medium",
).strip().lower()
DEFAULT_REASONING_EFFORT = (
    None
    if _reasoning_effort_env in {"", "none", "off"}
    else _reasoning_effort_env
)
DEFAULT_LLM_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("DEFAULT_LLM_TIMEOUT_SECONDS", "60")),
)
DEFAULT_LLM_MAX_RETRIES = max(
    0, int(os.getenv("DEFAULT_LLM_MAX_RETRIES", "0")),
)
JEV_ENABLED = os.getenv("JEV_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
JEV_API_KEY_ENV = os.getenv("JEV_API_KEY_ENV", "TYPESAFE_API_KEY").strip()
JEV_BASE_URL = os.getenv(
    "JEV_BASE_URL", "https://api.typesafe.ai/v1/systemone",
).strip()
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest").strip()
JEV_TIMEOUT_SECONDS = max(
    0.1, float(os.getenv("JEV_TIMEOUT_SECONDS", "3")),
)
JEV_GRAY_PERCENT = max(
    0, min(int(os.getenv("JEV_GRAY_PERCENT", "0")), 100),
)
JEV_MIN_CONFIDENCE = max(
    0.0, min(float(os.getenv("JEV_MIN_CONFIDENCE", "0.85")), 1.0),
)
JEV_SHADOW_COMPARE = os.getenv("JEV_SHADOW_COMPARE", "true").strip().lower() in {
    "1", "true", "yes", "on",
}
PARATERA_BASE_URL = os.getenv(
    "PARATERA_BASE_URL", "https://llmapi.paratera.com/v1",
).strip()
PARATERA_MODEL = os.getenv("PARATERA_MODEL", "GLM-5.2").strip()
PARATERA_MIN_TIMEOUT_SECONDS = max(
    1.0, float(os.getenv("PARATERA_MIN_TIMEOUT_SECONDS", "120")),
)
PARATERA_RATE_LIMIT_MAX_ATTEMPTS = max(
    1, int(os.getenv("PARATERA_RATE_LIMIT_MAX_ATTEMPTS", "14")),
)
PARATERA_RATE_LIMIT_BACKOFF_SECONDS = max(
    0.0, float(os.getenv("PARATERA_RATE_LIMIT_BACKOFF_SECONDS", "1")),
)
PARATERA_RATE_LIMIT_MAX_BACKOFF_SECONDS = max(
    PARATERA_RATE_LIMIT_BACKOFF_SECONDS,
    float(os.getenv("PARATERA_RATE_LIMIT_MAX_BACKOFF_SECONDS", "8")),
)
LLM_NIGHT_TIMEZONE = os.getenv("LLM_NIGHT_TIMEZONE", "Asia/Shanghai").strip()
LLM_NIGHT_START_HOUR = int(os.getenv("LLM_NIGHT_START_HOUR", "21"))
LLM_NIGHT_END_HOUR = int(os.getenv("LLM_NIGHT_END_HOUR", "9"))
# 旧版上下文压缩（模型回调 write_memory/write_user 全量覆盖）的历史常量。
# 新主链路是 ContextCompiler + TaskCheckpoint，不再在运行时读取这两个值；
# 只有显式打开 KLONET_AGENT_ENABLE_LEGACY_COMPRESSION 时才作为回退路径的参数。
MAX_TOKEN = 500000
HISTORY_MAX_MESSAGES = 20
# 上下文编译器：调用前按模型预算组装上下文。默认启用；
# 设置 KLONET_AGENT_DISABLE_CONTEXT_COMPILER=1 可临时回退旧路径。
CONTEXT_COMPILER_ENABLED = os.getenv(
    "KLONET_AGENT_DISABLE_CONTEXT_COMPILER", "0",
).strip().lower() not in {"1", "true", "yes", "on"}
# 旧版 compress_memory() + MAX_TOKEN 后置压缩。默认关闭，只保留一个版本周期
# 供 A/B 对比与安全回退；打开它意味着恢复“模型自由决定写什么记忆”的旧行为。
LEGACY_MEMORY_COMPRESSION_ENABLED = os.getenv(
    "KLONET_AGENT_ENABLE_LEGACY_COMPRESSION", "0",
).strip().lower() in {"1", "true", "yes", "on"}
# 受控写入管线（记忆系统阶段 3）：候选提取 + 策略过滤 + consolidation。
# 默认关闭。打开前必须有可用的记忆库 DSN，否则候选无处可落；打开之后
# write_memory / write_user 不再有整篇覆盖的权限，改为候选提交与审计。
MEMORY_WRITE_PIPELINE_ENABLED = os.getenv(
    "KLONET_AGENT_MEMORY_WRITE_PIPELINE", "0",
).strip().lower() in {"1", "true", "yes", "on"}
# 一次补扫最多处理多少个未处理事件区间（checkpoint / session 结束时用）。
MEMORY_BACKFILL_LIMIT = max(
    1, int(os.getenv("KLONET_AGENT_MEMORY_BACKFILL_LIMIT", "20")),
)
# 记忆包（MemoryPack）开关：默认关闭。打开后不再把 MEMORY.md / USER.md 全文常驻
# 注入系统提示词，改为每轮按当前问题召回并作为独立证据块注入（计划 §6.7、阶段 5）。
# 关闭时记忆提示词与主链路逐字保持原状。
MEMORY_PACK_ENABLED = os.getenv(
    "KLONET_AGENT_MEMORY_PACK", "0",
).strip().lower() in {"1", "true", "yes", "on"}
# 记忆包自身的 token 上限。它是**最终硬约束**：超预算时整条淘汰最低分的记忆
# （先相似经历、再项目事实、用户偏好最后），绝不截断单条结构。
MEMORY_PACK_TOKEN_BUDGET = max(
    120, int(os.getenv("KLONET_AGENT_MEMORY_PACK_TOKEN_BUDGET", "900")),
)
# 召回时取回的候选条数（进包前还会按类型与 token 再裁一遍）。
MEMORY_PACK_RECALL_LIMIT = max(
    10, int(os.getenv("KLONET_AGENT_MEMORY_PACK_RECALL_LIMIT", "40")),
)
# 软阈值触发压缩时，待覆盖历史低于该 token 数就跳过压缩：收益不足以抵掉
# 一次额外的压缩模型调用（软阈值也可能由系统规则/证据区单独造成）。
CONTEXT_COMPACTION_MIN_TOKENS = max(
    0, int(os.getenv("KLONET_AGENT_COMPACTION_MIN_TOKENS", "512")),
)
MAX_TOOL_ROUNDS = 8
OPS_MAX_TOOL_ROUNDS = 16
SHARED_OPS_MEMORY_RECENT_DAYS = 3
SHARED_OPS_MEMORY_SEARCH_LIMIT = 5
MAX_TODO_CONTINUATIONS = 1
DEFAULT_RAG_TOP_K = 3
RAG_PIPELINE_MODE = os.getenv("RAG_PIPELINE_MODE", "multi_stage").strip().lower()
RAG_QUERY_PLANNER_MODEL = os.getenv(
    "RAG_QUERY_PLANNER_MODEL",
    DEFAULT_MODEL,
).strip()
RAG_QUERY_PLANNER_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("RAG_QUERY_PLANNER_TIMEOUT_SECONDS", "6")),
)
OPS_PRIVILEGE_CLASSIFIER_MODEL = os.getenv(
    "OPS_PRIVILEGE_CLASSIFIER_MODEL",
    DEFAULT_MODEL,
).strip()
_ops_classifier_timeout = float(
    os.getenv("OPS_PRIVILEGE_CLASSIFIER_TIMEOUT_SECONDS", "0")
)
OPS_PRIVILEGE_CLASSIFIER_TIMEOUT_SECONDS = (
    _ops_classifier_timeout if _ops_classifier_timeout > 0 else None
)
OPS_PRIVILEGE_PLANNER_MODEL = os.getenv(
    "OPS_PRIVILEGE_PLANNER_MODEL",
    DEFAULT_MODEL,
).strip()
_ops_planner_timeout = float(
    os.getenv("OPS_PRIVILEGE_PLANNER_TIMEOUT_SECONDS", "30")
)
OPS_PRIVILEGE_PLANNER_TIMEOUT_SECONDS = (
    _ops_planner_timeout if _ops_planner_timeout > 0 else None
)
RAG_RECALL_TOP_K = max(1, int(os.getenv("RAG_RECALL_TOP_K", "30")))
RAG_FUSION_TOP_K = max(1, int(os.getenv("RAG_FUSION_TOP_K", "20")))
RAG_RERANK_TOP_N = max(1, int(os.getenv("RAG_RERANK_TOP_N", "10")))
RAG_RERANK_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("RAG_RERANK_TIMEOUT_SECONDS", "8")),
)
RERANK_MODEL = os.getenv("RERANK_MODEL", "qwen3-rerank").strip()
RERANK_BASE_URL = os.getenv(
    "RERANK_BASE_URL",
    DEFAULT_EMBEDDING_BASE_URL.replace(
        "/compatible-mode/v1",
        "/compatible-api/v1",
    ),
).strip()
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
CODE_INDEX_FILE = PROJECT_ROOT / "knowledge" / "code_index.jsonl"
CODE_VECTOR_INDEX_FILE = PROJECT_ROOT / "knowledge" / "code_vectors.jsonl"
AUTO_BUILD_KNOWLEDGE_VECTORS = os.getenv(
    "KLONET_AGENT_AUTO_BUILD_VECTORS",
    "1",
).strip().lower() in {"1", "true", "yes", "on"}
KNOWLEDGE_VECTOR_BUILD_BATCH_SIZE = max(
    1,
    int(os.getenv("KLONET_AGENT_VECTOR_BATCH_SIZE", "10")),
)
TRACE_FILE = PROJECT_ROOT / "tracing" / "trace.jsonl"
KLONET_UPSTREAM_SOURCE_ROOT = Path(
    os.getenv(
        "KLONET_UPSTREAM_SOURCE_ROOT",
        str(PROJECT_ROOT.parent / "vemu_uestc"),
    )
).expanduser()
KLONET_SOURCE_ROOT = Path(
    os.getenv(
        "KLONET_SOURCE_ROOT",
        str(PROJECT_ROOT / "knowledge" / "klonet_source"),
    )
).expanduser()

DEFAULT_USER_ID = "default"
DEFAULT_PROJECT_ID = "default"
DEFAULT_MODE = "mentor"


def ops_real_execution_enabled() -> bool:
    """Return whether Ops recipes may call the real server-side helper."""

    return ops_real_execution_mode() == "enabled"


def ops_real_execution_mode() -> str:
    """Return enabled, disabled, missing, or invalid for Ops execution config."""

    raw = os.getenv("KLONET_AGENT_OPS_REAL_EXECUTION")
    if raw is None or not raw.strip():
        return "missing"
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return "enabled"
    if value in {"0", "false", "no", "off"}:
        return "disabled"
    return "invalid"
