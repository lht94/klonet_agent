"""Shared test isolation for external retrieval services."""

import os


# Unit tests opt in with an injected provider when they exercise automatic
# vector construction.  Never spend real embedding quota during collection.
os.environ.setdefault("KLONET_AGENT_AUTO_BUILD_VECTORS", "0")
os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("CHAT_LLM_API_KEY", "")
os.environ.setdefault("EMBEDDING_API_KEY", "")
os.environ.setdefault("DASHSCOPE_API_KEY", "")
os.environ.setdefault("OPENAI_API_KEY", "")
