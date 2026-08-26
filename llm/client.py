"""大模型客户端封装。

这里统一负责初始化 SDK 客户端、选择模型、发送 messages。
上层只关心“给我一个模型响应”，不需要知道具体供应商和 SDK 细节。
"""

from __future__ import annotations

import os
import inspect
import time
from typing import Any

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv():
        return None

from klonet_agent.config import (
    CHAT_LLM_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_LLM_MAX_RETRIES,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    PARATERA_RATE_LIMIT_BACKOFF_SECONDS,
    PARATERA_RATE_LIMIT_MAX_ATTEMPTS,
    PARATERA_RATE_LIMIT_MAX_BACKOFF_SECONDS,
)
from klonet_agent.llm.provider import ProviderRouter, ProviderTarget


# os.environ 只能读取系统环境变量；load_dotenv 会把 .env 文件里的变量也加载进来。
# 本地开发把 CHAT_LLM_API_KEY 写在 .env 中；服务器也可直接使用系统环境变量。
load_dotenv()


class LLMClient:
    """统一的大模型调用入口。

    这个类封装底层 OpenAI SDK 客户端，并在每次请求前通过唯一的
    ProviderRouter 选择当前供应商。上层 Agent 不感知供应商切换。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        self.model = model
        self._router = None
        effective_timeout = (
            DEFAULT_LLM_TIMEOUT_SECONDS if timeout is None else float(timeout)
        )
        effective_retries = (
            DEFAULT_LLM_MAX_RETRIES
            if max_retries is None
            else max(0, int(max_retries))
        )
        self._client_options: dict[str, Any] = {
            "timeout": max(1.0, effective_timeout),
            "max_retries": effective_retries,
        }
        self._clients: dict[tuple[str, str], Any] = {}
        self._rate_limit_max_attempts = PARATERA_RATE_LIMIT_MAX_ATTEMPTS
        self._rate_limit_backoff_seconds = PARATERA_RATE_LIMIT_BACKOFF_SECONDS
        self._rate_limit_max_backoff_seconds = (
            PARATERA_RATE_LIMIT_MAX_BACKOFF_SECONDS
        )

        # Normal Agent construction uses the time-aware provider router.
        # Explicit transport arguments remain a fixed-provider escape hatch for
        # tests and deliberate integrations, never for normal orchestration.
        if api_key is None and base_url is None:
            self._router = ProviderRouter.from_environment()
            current = self._router.resolve(model)
            self.api_key = current[0].api_key if current else None
            self.base_url = current[0].base_url if current else ""
            return

        self.api_key = api_key or os.environ.get(CHAT_LLM_API_KEY_ENV)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        from openai import OpenAI

        client_options = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            **self._client_options,
        }
        self.client = OpenAI(**client_options)

    @property
    def has_credentials(self) -> bool:
        if getattr(self, "_router", None) is not None:
            return self._router.has_credentials
        return bool(getattr(self, "api_key", None))

    def _targets(self) -> tuple[tuple[Any, str], ...]:
        router = getattr(self, "_router", None)
        if router is None:
            return ((self.client, self.model),)
        targets = router.resolve(self.model)
        if not targets:
            raise RuntimeError("active LLM provider has no configured API key")
        return tuple((self._client_for(target), target.model) for target in targets)

    def _client_for(self, target: ProviderTarget) -> Any:
        cache_key = (target.base_url, target.api_key)
        existing = self._clients.get(cache_key)
        if existing is not None:
            return existing
        from openai import OpenAI

        options = dict(self._client_options)
        minimum = target.min_timeout_seconds
        configured = options.get("timeout")
        if minimum is not None and (
            configured is None or float(configured) < minimum
        ):
            options["timeout"] = minimum
        client = OpenAI(
            api_key=target.api_key,
            base_url=target.base_url,
            **options,
        )
        self._clients[cache_key] = client
        return client

    @staticmethod
    def _may_try_next_key(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        return status in {401, 402, 403, 429}

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        """Recognize provider throttling even when a gateway wraps it as 401."""

        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return True
        body = getattr(exc, "body", None)
        text = str(body if body is not None else exc).lower()
        return any(marker in text for marker in (
            "ratelimit", "rate limit", "rate_limit", "too many requests",
            "速率限制", "请求过于频繁", "调用频率",
        ))

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float | None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
            delay = float(value)
        except (AttributeError, TypeError, ValueError):
            return None
        return max(0.0, delay)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
        stream: bool = False,
        *,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ):
        """发送一次 Chat Completions 请求并返回原始模型响应。

        这个方法替代旧版 runner.py 中直接调用
        client.chat.completions.create(...) 的代码。
        """

        targets = self._targets()
        router = getattr(self, "_router", None)
        routed_targets = router.resolve(self.model) if router is not None else ()
        paratera_active = bool(routed_targets) and all(
            target.provider == "paratera" for target in routed_targets
        )
        max_attempts = (
            max(
                len(targets),
                int(getattr(
                    self, "_rate_limit_max_attempts",
                    PARATERA_RATE_LIMIT_MAX_ATTEMPTS,
                )),
            )
            if paratera_active
            else len(targets)
        )
        last_error: Exception | None = None
        rate_limit_round = 0
        for attempt in range(max_attempts):
            index = attempt % len(targets)
            client, active_model = targets[index]
            request = {
                "model": active_model,
                "messages": messages,
                "stream": stream,
            }
            create = client.chat.completions.create
            parameters = inspect.signature(create).parameters
            if reasoning_effort is not None and (
                "reasoning_effort" in parameters
                or any(
                    item.kind == inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                )
            ):
                request["reasoning_effort"] = reasoning_effort
            if tools is not None:
                request["tools"] = tools
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
            if temperature is not None:
                request["temperature"] = temperature
            if response_format is not None:
                request["response_format"] = response_format
            if max_tokens is not None:
                request["max_tokens"] = max(1, int(max_tokens))
            if extra_body is not None:
                request["extra_body"] = extra_body
            try:
                return create(**request)
            except Exception as exc:
                last_error = exc
                if not self._may_try_next_key(exc):
                    raise
                is_rate_limit = self._is_rate_limit(exc)
                if not is_rate_limit:
                    # Authentication/quota failures may fail over to each
                    # configured key once, but must never cycle indefinitely.
                    if attempt + 1 >= len(targets):
                        raise
                    continue
                if not paratera_active or attempt + 1 >= max_attempts:
                    raise RuntimeError(
                        "LLM provider rate limit remained active after "
                        "bounded key rotation and backoff"
                    ) from exc
                # Try every independent key once before sleeping.  Subsequent
                # rounds use Retry-After when present, otherwise bounded
                # exponential backoff.  This is explicit transport policy, not
                # an SDK hidden retry.
                if (attempt + 1) % len(targets) == 0:
                    configured_base = float(getattr(
                        self, "_rate_limit_backoff_seconds",
                        PARATERA_RATE_LIMIT_BACKOFF_SECONDS,
                    ))
                    configured_max = float(getattr(
                        self, "_rate_limit_max_backoff_seconds",
                        PARATERA_RATE_LIMIT_MAX_BACKOFF_SECONDS,
                    ))
                    delay = self._retry_after_seconds(exc)
                    if delay is None:
                        delay = min(
                            configured_max,
                            configured_base * (2 ** rate_limit_round),
                        )
                    else:
                        delay = min(configured_max, delay)
                    rate_limit_round += 1
                    if delay > 0:
                        time.sleep(delay)
        assert last_error is not None
        raise last_error
