#!/usr/bin/env bash
# 对比测试实验臂启动器：以锁定的模型配置运行 klonet_agent。
#
# 模型与供给方的锁定逻辑全部在 scripts/eval_env.sh，由两个实验臂共同 source。
# **不要在本文件里重复实现配置** —— 两臂各自读配置正是导致模型不一致的原因
# （实测踩过：对照臂读到 .env 的 gemini-3.7-flash，实验臂用 gpt-5.6-sol）。
#
# 用法：
#   bash scripts/run_eval_session.sh ops    lht test
#   bash scripts/run_eval_session.sh mentor lht test
#
# 覆盖模型：
#   KLONET_EVAL_MODEL=gpt-5.6-terra bash scripts/run_eval_session.sh ops lht test

set -euo pipefail

MODE="${1:-ops}"
USER_ID="${2:-lht}"
PROJECT_ID="${3:-test}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=/dev/null
source "$ROOT/scripts/eval_env.sh"

echo "运行清单 | mode=$MODE | model=$CHAT_LLM_MODEL | base_url=$CHAT_LLM_BASE_URL | user=$USER_ID | project=$PROJECT_ID" >&2

cd "$ROOT"
exec python -m klonet_agent.agent \
  --mode "$MODE" --user-id "$USER_ID" --project-id "$PROJECT_ID"
