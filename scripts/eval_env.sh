#!/usr/bin/env bash
# 对比测试共用的模型与供给方锁定配置。
#
# 两个实验臂（klonet_agent 的 mentor/ops，以及对照臂 baseline_agent）**必须**都 source
# 本文件，否则模型不一致，全部结论失效。
#
# 这不是假设性风险 —— 实测踩过：对照臂直接读仓库 .env，拿到 CHAT_LLM_MODEL=gemini-3.7-flash，
# 而实验臂走启动器注入 gpt-5.6-sol，两臂跑的不是同一个模型。共用本文件即可消除该风险。
#
# 为什么不改 .env：
#   1. tests/test_llm_provider.py 直接断言 .env 里的模型名，改 .env 会导致 3 个测试失败；
#   2. config.py 的 load_dotenv 未开启 override，shell 环境变量天然优先；
#   3. .env 是运行时配置，不在 diff 中，同步时容易被误覆盖。
#
# 为什么把日夜两个 provider 指向同一目标：
#   llm/provider.py 的 ProviderRouter 按北京时间 21:00-09:00 切到 PARATERA_* 分支。
#   两个分支指向同一后端、同一模型、同一密钥后，is_night_window() 无论真假，
#   解析出的 (model, base_url) 都只有一种组合。
#
# 用法：
#   source scripts/eval_env.sh
#
# 可用环境变量覆盖模型：
#   KLONET_EVAL_MODEL=gpt-5.6-terra source scripts/eval_env.sh

# 模型供给方。两臂必须取同一个值，否则模型不一致、结论失效。
#   deepseek —— 官方直连 https://api.deepseek.com
#   relay    —— yyds168 中转（gpt-5.6-sol）
# 选 deepseek 的原因：中转端点单次调用延迟在 2.6s~100s 之间抖动（28 倍），
# 且 gpt-5.6-sol 每次调用带约 4392 固定 prompt token（deepseek 仅 35~88）。
# 前者让耗时不可比，后者让 token 不可比。官方直连两者都干净。
KLONET_EVAL_PROVIDER="${KLONET_EVAL_PROVIDER:-deepseek}"

case "$KLONET_EVAL_PROVIDER" in
  deepseek)
    : "${KLONET_EVAL_MODEL:=deepseek-v4-pro}"
    : "${KLONET_EVAL_BASE_URL:=https://api.deepseek.com}"
    _KLONET_EVAL_KEY_NAME="DEEPSEEK_API_KEY"
    ;;
  relay)
    : "${KLONET_EVAL_MODEL:=gpt-5.6-sol}"
    : "${KLONET_EVAL_BASE_URL:=https://api.yyds168.net/v1}"
    _KLONET_EVAL_KEY_NAME="CHAT_LLM_API_KEY"
    ;;
  *)
    echo "错误：未知的 KLONET_EVAL_PROVIDER=$KLONET_EVAL_PROVIDER（可选 deepseek / relay）" >&2
    exit 1
    ;;
esac

_KLONET_EVAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_klonet_read_env() {
  sed -n "s/^$1=//p" "$_KLONET_EVAL_ROOT/.env" | head -1 | tr -d "\"'"
}

_KLONET_EVAL_KEY="$(_klonet_read_env "$_KLONET_EVAL_KEY_NAME")"
if [ -z "$_KLONET_EVAL_KEY" ]; then
  echo "错误：$_KLONET_EVAL_ROOT/.env 中缺少 $_KLONET_EVAL_KEY_NAME，无法进行实验。" >&2
  exit 1
fi

export CHAT_LLM_BASE_URL="$KLONET_EVAL_BASE_URL"
export CHAT_LLM_MODEL="$KLONET_EVAL_MODEL"
export CHAT_LLM_API_KEY="$_KLONET_EVAL_KEY"

export PARATERA_BASE_URL="$KLONET_EVAL_BASE_URL"
export PARATERA_MODEL="$KLONET_EVAL_MODEL"
export PARATERA_API_KEY_1="$_KLONET_EVAL_KEY"
export PARATERA_API_KEY_2="$_KLONET_EVAL_KEY"

# 供实验记录使用：把锁定后的模型写进运行清单，便于事后核对两臂是否一致。
export KLONET_EVAL_LOCKED_PROVIDER="$KLONET_EVAL_PROVIDER"
export KLONET_EVAL_LOCKED_MODEL="$KLONET_EVAL_MODEL"
export KLONET_EVAL_LOCKED_BASE_URL="$KLONET_EVAL_BASE_URL"
