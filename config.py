"""klonet_agent 的运行配置。

这里集中放模型名称、token 限制、工作区路径、记忆路径、RAG 开关等全局配置。
不要在业务模块里散落硬编码配置，后续部署到服务器时也更方便从环境变量或配置文件读取。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
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
# 记忆权威归属：与阶段 6 的四态状态机同名（legacy / shadow / compare / cutover）。
# 默认 **cutover**（2026-10-10 起）：数据库优先——配了记忆库 DSN 就走受控写入与
# 按需召回，Markdown 退出常驻注入；**库不可用（未配 DSN / 连不上）时自动降级回
# Markdown 注入与文件写入**，降级会在 trace 留痕（memory_markdown_fallback）。
# 想回到旧行为可显式设 KLONET_AGENT_MEMORY_AUTHORITY=legacy。
# 取值非法时退回 legacy —— 读到一个拼错的字就切换权威仍是最糟的失败方向，
# 而 legacy 是上一代已验证的稳定行为。
MEMORY_AUTHORITY = os.getenv(
    "KLONET_AGENT_MEMORY_AUTHORITY", "cutover",
).strip().lower()
if MEMORY_AUTHORITY not in {"legacy", "shadow", "compare", "cutover"}:
    MEMORY_AUTHORITY = "legacy"

# 运行治理层（通用功能升级计划 03，阶段 1）。默认关闭：治理层与记忆库同为
# 可选部署件（状态必须落到 PostgreSQL，没有 DSN 时打开只会每轮多一次失败
# 连接）。打开后：任务/失败状态改变 fail closed，telemetry 事件降级缓冲；
# 治理事件同时导出到 GOVERNANCE_TRACE_FILE（JSONL 只是导出，不是权威）。
RUNTIME_GOVERNANCE_ENABLED = os.getenv(
    "KLONET_AGENT_RUNTIME_GOVERNANCE", "0",
).strip().lower() in {"1", "true", "yes", "on"}
GOVERNANCE_TRACE_FILE = PROJECT_ROOT / "tracing" / "governance.jsonl"


def markdown_memory_is_authoritative() -> bool:
    """Markdown 是否仍是回答时读取的记忆来源。

    只在 ``cutover`` 之后为 False。``shadow`` / ``compare`` 都还以 Markdown 回答
    （前者只验证影子写入能不能落下来，后者只做双读对比）。
    """

    return MEMORY_AUTHORITY != "cutover"


def memory_cutover_enabled() -> bool:
    """是否处于"数据库优先"。

    这是阶段 7 的**单一切换点**：``KLONET_AGENT_MEMORY_AUTHORITY=cutover``
    一次把三件事同时打开——数据库读取管线、受控写入管线、按需召回注入，
    Markdown 降级为迁移/导出来源。

    注意 cutover ≠ "Markdown 永久退场"：这是**默认值**（2026-10-10 起），
    但运行时若召回链路不可用（未配 DSN、连不上库），orchestrator 会自动
    降级回 Markdown 注入与文件写入，并在 trace 留痕。降级是**可用性**手段，
    不改变权威归属（那由迁移状态机管）。
    """

    return MEMORY_AUTHORITY == "cutover"


# 三个开关都认 cutover：开一个变量就能整体切换，避免"切了一半"的状态。
# 也可以单独用各自的变量灰度（例如只开写入管线跑 shadow 阶段）。
MEMORY_WRITE_PIPELINE_ENABLED = MEMORY_WRITE_PIPELINE_ENABLED or memory_cutover_enabled()
MEMORY_PACK_ENABLED = MEMORY_PACK_ENABLED or memory_cutover_enabled()


# --- 记忆生命周期维护 Worker（04 计划 §7）-------------------------------- #
#
# Worker 是**独立进程**（不随 Agent 主链路启动），默认关闭。所有值走环境变量，
# 但**不做静默修正**：非法周期/批量/租约会让 ``load_maintenance_config()`` 抛
# ``MaintenanceConfigError``，让进程以明确的配置错误退出码结束——一个把
# lease 配成 0 的部署如果被"悄悄改成默认值"，会以最难排查的方式出问题
# （两个 worker 抢同一 job、或租约永不过期）。
#
# 完整取值见 ``doc/`` 的运维 runbook；这里只固化契约。

class MaintenanceConfigError(ValueError):
    """维护 Worker 配置非法。启动必须拒绝，不静默修正。"""


@dataclass(frozen=True)
class MaintenanceConfig:
    """Worker 全部可调参数。默认值即 04 计划 §7 表格。"""

    enabled: bool = False
    poll_seconds: float = 5.0
    shutdown_grace_seconds: float = 30.0
    lease_seconds: float = 120.0
    embedding_batch_size: int = 20
    embedding_interval_seconds: int = 10
    expiration_interval_seconds: int = 3600
    expiration_batch_size: int = 500
    purge_interval_seconds: int = 86400
    purge_batch_size: int = 1000
    consolidation_interval_seconds: int = 86400
    consolidation_batch_size: int = 100
    dry_run: bool = False

    def __post_init__(self) -> None:
        """严格校验：任何非法值都让构造失败。

        刻意**不 clamp**——``min 1`` / ``max 0`` 这类修正会让一个配错的部署
        看起来正常启动，然后在生产上以"任务永远跑不起来"或"租约永不过期"
        的形式失败。
        """

        problems: list[str] = []
        if self.poll_seconds <= 0:
            problems.append(f"poll_seconds 必须 > 0，实际 {self.poll_seconds}")
        if self.shutdown_grace_seconds <= 0:
            problems.append(
                f"shutdown_grace_seconds 必须 > 0，实际 {self.shutdown_grace_seconds}"
            )
        if self.lease_seconds <= 0:
            problems.append(f"lease_seconds 必须 > 0，实际 {self.lease_seconds}")
        if self.lease_seconds < self.poll_seconds:
            problems.append(
                f"lease_seconds({self.lease_seconds}) 不能小于 poll_seconds({self.poll_seconds})"
            )
        if self.shutdown_grace_seconds > self.lease_seconds:
            # grace 比 lease 还长：收到 SIGTERM 后想在 grace 内跑完，但 lease
            # 早就过期、别的 worker 已经把同一 job 领走了。
            problems.append(
                f"shutdown_grace_seconds({self.shutdown_grace_seconds}) 不能大于 "
                f"lease_seconds({self.lease_seconds})"
            )
        for name, value in (
            ("embedding_batch_size", self.embedding_batch_size),
            ("expiration_batch_size", self.expiration_batch_size),
            ("purge_batch_size", self.purge_batch_size),
            ("consolidation_batch_size", self.consolidation_batch_size),
        ):
            if value <= 0:
                problems.append(f"{name} 必须 > 0，实际 {value}")
        for name, value in (
            ("embedding_interval_seconds", self.embedding_interval_seconds),
            ("expiration_interval_seconds", self.expiration_interval_seconds),
            ("purge_interval_seconds", self.purge_interval_seconds),
            ("consolidation_interval_seconds", self.consolidation_interval_seconds),
        ):
            if value <= 0:
                problems.append(f"{name} 必须 > 0，实际 {value}")
        if problems:
            raise MaintenanceConfigError(
                "MaintenanceConfig 非法：" + "；".join(problems)
            )

    def job_interval_seconds(self, job_name: str) -> int:
        """某 Job 的默认调度周期（秒）。

        未登记的 job 名返回 ``poll_seconds``——调用方（service.py 的 registry）
        会在注册阶段先拒掉未知名字，这里只兜底不抛。
        """

        return {
            "embedding_outbox": self.embedding_interval_seconds,
            "expiration": self.expiration_interval_seconds,
            "purge": self.purge_interval_seconds,
            "consolidation": self.consolidation_interval_seconds,
        }.get(job_name, max(1, int(self.poll_seconds)))

    def job_batch_size(self, job_name: str) -> int:
        return {
            "embedding_outbox": self.embedding_batch_size,
            "expiration": self.expiration_batch_size,
            "purge": self.purge_batch_size,
            "consolidation": self.consolidation_batch_size,
        }.get(job_name, 100)


_MAINTENANCE_ENV_PREFIX = "KLONET_AGENT_MAINTENANCE_"


def _maintenance_env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise MaintenanceConfigError(
        f"{name} 取值非法：{raw!r}（应为 1/0/true/false/yes/no/on/off）"
    )


def _maintenance_env_value(
    env: Mapping[str, str], name: str, default: str, kind: str
) -> str:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    text = str(raw).strip()
    try:
        if kind == "int":
            int(text)
        elif kind == "float":
            float(text)
        else:  # pragma: no cover - 防御性分支
            raise ValueError(kind)
    except ValueError as exc:
        raise MaintenanceConfigError(
            f"{name} 取值非法：{raw!r}（应为 {kind}）"
        ) from exc
    return text


def load_maintenance_config(env: Mapping[str, str] | None = None) -> MaintenanceConfig:
    """从环境变量构造 ``MaintenanceConfig``。

    与 ``config.py`` 顶层常量不同，**不提供模块级默认实例**：这个配置一旦读错
    就直接拒绝启动，所以必须由 Worker 启动路径显式调用、显式处理异常。
    """

    source: Mapping[str, str] = os.environ if env is None else env

    def _int(name: str, default: int) -> int:
        return int(_maintenance_env_value(source, name, str(default), "int"))

    def _float(name: str, default: float) -> float:
        return float(_maintenance_env_value(source, name, str(default), "float"))

    config = MaintenanceConfig(
        enabled=_maintenance_env_bool(
            source, f"{_MAINTENANCE_ENV_PREFIX}ENABLED", False
        ),
        poll_seconds=_float(f"{_MAINTENANCE_ENV_PREFIX}POLL_SECONDS", 5.0),
        shutdown_grace_seconds=_float(
            f"{_MAINTENANCE_ENV_PREFIX}SHUTDOWN_GRACE_SECONDS", 30.0
        ),
        lease_seconds=_float(f"{_MAINTENANCE_ENV_PREFIX}LEASE_SECONDS", 120.0),
        embedding_batch_size=_int(
            f"{_MAINTENANCE_ENV_PREFIX}EMBEDDING_BATCH_SIZE", 20
        ),
        embedding_interval_seconds=_int(
            f"{_MAINTENANCE_ENV_PREFIX}EMBEDDING_INTERVAL_SECONDS", 10
        ),
        expiration_interval_seconds=_int(
            f"{_MAINTENANCE_ENV_PREFIX}EXPIRATION_INTERVAL_SECONDS", 3600
        ),
        expiration_batch_size=_int(
            f"{_MAINTENANCE_ENV_PREFIX}EXPIRATION_BATCH_SIZE", 500
        ),
        purge_interval_seconds=_int(
            f"{_MAINTENANCE_ENV_PREFIX}PURGE_INTERVAL_SECONDS", 86400
        ),
        purge_batch_size=_int(
            f"{_MAINTENANCE_ENV_PREFIX}PURGE_BATCH_SIZE", 1000
        ),
        consolidation_interval_seconds=_int(
            f"{_MAINTENANCE_ENV_PREFIX}CONSOLIDATION_INTERVAL_SECONDS", 86400
        ),
        consolidation_batch_size=_int(
            f"{_MAINTENANCE_ENV_PREFIX}CONSOLIDATION_BATCH_SIZE", 100
        ),
        dry_run=_maintenance_env_bool(
            source, f"{_MAINTENANCE_ENV_PREFIX}DRY_RUN", False
        ),
    )
    return config
# 软阈值触发压缩时，待覆盖历史低于该 token 数就跳过压缩：收益不足以抵掉
# 一次额外的压缩模型调用（软阈值也可能由系统规则/证据区单独造成）。
CONTEXT_COMPACTION_MIN_TOKENS = max(
    0, int(os.getenv("KLONET_AGENT_COMPACTION_MIN_TOKENS", "512")),
)

# 阶段 7 顺序开关 KLONET_AGENT_LEGACY_COMPRESSION_ORDER 定义在
# context/compiler.py 与 orchestrator.py 各自的 _legacy_compression_order()
# 里 —— 刻意让 context 包保持自包含，不由配置层反向注入。
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
