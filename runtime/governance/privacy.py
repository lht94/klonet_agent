"""最小隐私网关（计划 §4.6，阶段 1 与事件底座同步交付）。

职责边界（计划 §1.1）：只负责**数据分类、脱敏与准入**，不判断"某条信息
是否值得成为记忆"——那是 ``memory/write_policy`` 的事；记忆写入管线不能
绕过本网关，本网关也不替代记忆价值判断。

四级分类：public / internal / sensitive / secret。

- ``secret``（API key、token、密码、私钥）：**拒绝持久化与注入**，
  抛 :class:`SecretRejectedError`——fail closed，绝不静默保留；
- ``sensitive``：脱敏后放行，并产出 :class:`RedactionRecord` 留痕
  （只记规则、类别与哈希，不保存原文）。

规则版本 ``PRIVACY_RULES_VERSION`` 随每次脱敏结果一起被审计。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from klonet_agent.runtime.governance.models import (
    PrivacyClass,
    RedactionRecord,
)

# 规则集版本：检测/脱敏行为变化时递增，用于审计与回放对比。
PRIVACY_RULES_VERSION = "1"


class PrivacyViolationError(RuntimeError):
    """数据未通过隐私准入。"""


class SecretRejectedError(PrivacyViolationError):
    """检测到 secret 级数据，拒绝进入事件、prompt、日志或 eval。"""


@dataclass(frozen=True)
class _SecretRule:
    rule_id: str
    category: str
    pattern: re.Pattern[str]


# secret 级检测规则。宁可误报（调用方显式放行），不可漏报。
_SECRET_RULES: tuple[_SecretRule, ...] = (
    _SecretRule(
        "sk-api-key",
        "api_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    ),
    _SecretRule(
        "aws-access-key",
        "api_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    _SecretRule(
        "github-token",
        "api_key",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ),
    _SecretRule(
        "private-key-block",
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
    _SecretRule(
        "password-assignment",
        "password",
        re.compile(
            r"\b(?:password|passwd|pwd|api[_-]?key|secret|token)\b"
            r"\s*[:=]\s*(?!\s*['\"]?[$<{])(\S{6,})",
            re.IGNORECASE,
        ),
    ),
    _SecretRule(
        "bearer-token",
        "token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}\b", re.IGNORECASE),
    ),
)

# sensitive 级模式：脱敏后放行。
_SENSITIVE_RULES: tuple[_SecretRule, ...] = (
    _SecretRule(
        "ipv4-address",
        "network_address",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ),
    _SecretRule(
        "email-address",
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def detect(text: str) -> PrivacyClass:
    """对一段文本给出最高风险等级。"""

    value = str(text or "")
    for rule in _SECRET_RULES:
        if rule.pattern.search(value):
            return PrivacyClass.SECRET
    for rule in _SENSITIVE_RULES:
        if rule.pattern.search(value):
            return PrivacyClass.SENSITIVE
    return PrivacyClass.INTERNAL


def redact_text(text: str) -> tuple[str, list[RedactionRecord]]:
    """脱敏一段文本，返回 ``(脱敏后文本, 脱敏留痕列表)``。

    secret 命中的片段被替换为占位符；sensitive 命中的片段同样替换但单独
    分类。替换顺序按规则在文本中的实际命中位置进行，避免前一条规则改写
    内容导致后一条规则哈希失真。
    """

    value = str(text or "")
    records: list[RedactionRecord] = []
    spans: list[tuple[int, int, str, _SecretRule]] = []
    for rule in _SECRET_RULES:
        for match in rule.pattern.finditer(value):
            spans.append((match.start(), match.end(), match.group(0), rule))
    for rule in _SENSITIVE_RULES:
        for match in rule.pattern.finditer(value):
            spans.append((match.start(), match.end(), match.group(0), rule))
    if not spans:
        return value, records

    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    pieces: list[str] = []
    cursor = 0
    for start, end, raw, rule in spans:
        if start < cursor:
            continue
        pieces.append(value[cursor:start])
        pieces.append(f"[REDACTED:{rule.category}]")
        cursor = end
        records.append(
            RedactionRecord(
                rule_id=rule.rule_id,
                category=rule.category,
                content_hash=_content_hash(raw),
                privacy_class=PrivacyClass.SECRET
                if rule in _SECRET_RULES
                else PrivacyClass.SENSITIVE,
            )
        )
    pieces.append(value[cursor:])
    return "".join(pieces), records


class PrivacyGateway:
    """统一隐私准入边界。

    所有进入事件账本、trace 导出与 eval 的 payload 都先经过
    :meth:`admit`；secret 一律拒绝，sensitive 就地脱敏并留痕。
    """

    def __init__(self, rules_version: str = PRIVACY_RULES_VERSION):
        self.rules_version = rules_version

    def classify(self, text: str) -> PrivacyClass:
        return detect(text)

    def redact(self, text: str) -> tuple[str, list[RedactionRecord]]:
        return redact_text(text)

    def admit_payload(
        self, payload: dict
    ) -> tuple[dict, list[RedactionRecord]]:
        """对一个即将持久化的事件 payload 做准入。

        返回 ``(可安全持久化的 payload, 脱敏留痕)``：

        - 命中 **secret**：抛 :class:`SecretRejectedError`（fail closed），
          异常上携带脱敏留痕（只有规则、类别与哈希），由调用方决定丢弃或
          脱敏后重试——绝不把明文秘密写进事件、prompt 或日志；
        - 命中 **sensitive**：就地脱敏后放行，并返回留痕记录。

        payload 是嵌套结构，这里做深度遍历，字符串字段逐个过规则。
        """

        records: list[RedactionRecord] = []
        sanitized = self._admit_value(payload, records)
        secrets = [r for r in records if r.privacy_class == PrivacyClass.SECRET]
        if secrets:
            raise SecretRejectedError(
                f"payload 命中 {len(secrets)} 处 secret 级数据，已拒绝持久化"
                f"（规则版本 {self.rules_version}，类别: "
                f"{sorted({r.category for r in secrets})}）"
            )
        return sanitized, records

    def _admit_value(self, value, records: list[RedactionRecord]):
        if isinstance(value, str):
            sanitized, new_records = redact_text(value)
            records.extend(new_records)
            return sanitized
        if isinstance(value, dict):
            return {
                key: self._admit_value(item, records)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            admitted = [self._admit_value(item, records) for item in value]
            return type(value)(admitted) if isinstance(value, tuple) else admitted
        return value


def default_gateway() -> PrivacyGateway:
    """进程级默认网关实例。规则集是纯函数式的，共享实例没有状态风险。"""

    return PrivacyGateway()
