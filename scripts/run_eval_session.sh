#!/usr/bin/env bash
# 对比测试专用启动器：在不修改 .env 的前提下，把模型与 provider 锁到实验配置。
#
# 为什么需要单独一个启动器：
#   1. config.py 会 load_dotenv(.env)，而仓库的 tests/test_llm_provider.py 直接断言
#      .env 里的模型名（GLM-5.2）。把实验模型写进 .env 会让这些测试失败。
#   2. load_dotenv 未开启 override，因此 shell 环境变量优先于 .env，
#      可以在此处覆盖而完全不改动仓库配置。
#   3. llm/provider.py 的 ProviderRouter 按北京时间切换 provider。必须让夜间分支
#      也指向同一后端与同一模型，否则跨时段运行会把"模型差异"混入"Agent 差异"。
#
# 用法：
#   bash scripts/run_eval_session.sh ops    lht test
#   bash scripts/run_eval_session.sh mentor lht test
#
# 也可用环境变量覆盖模型：
#   KLONET_EVAL_MODEL=gpt-5.6-terra bash scripts/run_eval_session.sh ops lht test

set -euo pipefail

MODE="${1:-ops}"
USER_ID="${2:-lht}"
PROJECT_ID="${3:-test}"
MODEL="${KLONET_EVAL_MODEL:-gpt-5.6-sol}"
BASE_URL="${KLONET_EVAL_BASE_URL:-https://api.yyds168.net/v1}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"

read_env() {
  sed -n "s/^$1=//p" "$ENV_FILE" | head -1 | tr -d "\"'"
}

CHAT_KEY="$(read_env CHAT_LLM_API_KEY)"
if [ -z "$CHAT_KEY" ]; then
  echo "错误：$ENV_FILE 中缺少 CHAT_LLM_API_KEY，无法进行实验。" >&2
  exit 1
fi

# 日间与夜间两个 provider 指向同一目标：无论 is_night_window() 返回什么，
# 解析出的 (model, base_url) 都只有一种组合。
export CHAT_LLM_BASE_URL="$BASE_URL"
export CHAT_LLM_MODEL="$MODEL"
export CHAT_LLM_API_KEY="$CHAT_KEY"
export PARATERA_BASE_URL="$BASE_URL"
export PARATERA_MODEL="$MODEL"
export PARATERA_API_KEY_1="$CHAT_KEY"
export PARATERA_API_KEY_2="$CHAT_KEY"

cd "$ROOT"
exec python -m klonet_agent.agent \
  --mode "$MODE" --user-id "$USER_ID" --project-id "$PROJECT_ID"
