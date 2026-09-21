"""临时校验脚本：确认实验期日夜两个 provider 分支解析到同一模型与后端。

仅用于本地验证配置，不进入生产调用链。验证完可删除。
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"C:\Users\lht\OneDrive\课设\agent开发\klonet_agent")

from klonet_agent.config import (  # noqa: E402
    CHAT_LLM_MODEL,
    DEFAULT_BASE_URL,
    PARATERA_BASE_URL,
    PARATERA_MODEL,
)
from klonet_agent.llm.provider import ProviderRouter  # noqa: E402

print("CHAT_LLM_MODEL =", CHAT_LLM_MODEL)
print("PARATERA_MODEL =", PARATERA_MODEL)
print("daytime url    =", DEFAULT_BASE_URL)
print("paratera url   =", PARATERA_BASE_URL)
print()

router = ProviderRouter.from_environment()
tz = ZoneInfo("Asia/Shanghai")
cases = (
    ("day 14:00", 14),
    ("day 08:59", 8),
    ("night 21:00", 21),
    ("night 22:00", 22),
)

for label, hour in cases:
    moment = datetime(2026, 9, 21, hour, 0, tzinfo=tz)
    targets = router.resolve("ignored-model-name")
    night = router.is_night_window(moment)
    if not targets:
        print("%16s | night=%-5s | NO CREDENTIALS" % (label, night))
        continue
    for target in targets:
        print(
            "%16s | night=%-5s | provider=%-8s | model=%-14s | url=%s"
            % (label, night, target.provider, target.model, target.base_url)
        )

print()
print("has_credentials =", router.has_credentials)
