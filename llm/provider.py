"""Authoritative time-aware provider selection for all chat LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from typing import Callable
from zoneinfo import ZoneInfo

from klonet_agent.config import (
    CHAT_LLM_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LLM_NIGHT_END_HOUR,
    LLM_NIGHT_START_HOUR,
    LLM_NIGHT_TIMEZONE,
    PARATERA_BASE_URL,
    PARATERA_MIN_TIMEOUT_SECONDS,
    PARATERA_MODEL,
)


@dataclass(frozen=True, repr=False)
class ProviderTarget:
    """One transport target; repr deliberately never exposes credentials."""

    provider: str
    base_url: str
    model: str
    api_key: str
    min_timeout_seconds: float | None = None

    def __repr__(self) -> str:
        return (
            "ProviderTarget(provider=%r, base_url=%r, model=%r, api_key=<redacted>)"
            % (self.provider, self.base_url, self.model)
        )


class ProviderRouter:
    """Select the sole active provider for the current request time."""

    def __init__(
        self,
        *,
        daytime_key: str,
        paratera_keys: tuple[str, ...],
        daytime_base_url: str = DEFAULT_BASE_URL,
        daytime_model: str = DEFAULT_MODEL,
        paratera_base_url: str = PARATERA_BASE_URL,
        paratera_model: str = PARATERA_MODEL,
        paratera_min_timeout_seconds: float = PARATERA_MIN_TIMEOUT_SECONDS,
        timezone: str = LLM_NIGHT_TIMEZONE,
        night_start_hour: int = LLM_NIGHT_START_HOUR,
        night_end_hour: int = LLM_NIGHT_END_HOUR,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 <= night_start_hour <= 23 or not 0 <= night_end_hour <= 23:
            raise ValueError("LLM provider schedule hours must be within 0..23")
        if night_start_hour == night_end_hour:
            raise ValueError("LLM provider schedule cannot cover an ambiguous full day")
        self.daytime_key = daytime_key.strip()
        self.paratera_keys = tuple(dict.fromkeys(
            key.strip() for key in paratera_keys if key.strip()
        ))
        self.daytime_base_url = daytime_base_url.rstrip("/")
        self.daytime_model = daytime_model.strip()
        self.paratera_base_url = paratera_base_url.rstrip("/")
        self.paratera_model = paratera_model
        self.paratera_min_timeout_seconds = max(
            1.0, float(paratera_min_timeout_seconds),
        )
        self.timezone = ZoneInfo(timezone)
        self.night_start_hour = night_start_hour
        self.night_end_hour = night_end_hour
        self._now = now or (lambda: datetime.now(self.timezone))

    @classmethod
    def from_environment(cls) -> "ProviderRouter":
        combined = os.getenv("PARATERA_API_KEYS", "")
        keys = [item.strip() for item in combined.split(",") if item.strip()]
        keys.extend([
            os.getenv("PARATERA_API_KEY_1", "").strip(),
            os.getenv("PARATERA_API_KEY_2", "").strip(),
        ])
        return cls(
            daytime_key=os.getenv(CHAT_LLM_API_KEY_ENV, ""),
            paratera_keys=tuple(keys),
        )

    def is_night_window(self, moment: datetime | None = None) -> bool:
        current = moment or self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=self.timezone)
        else:
            current = current.astimezone(self.timezone)
        hour = current.hour
        if self.night_start_hour > self.night_end_hour:
            return hour >= self.night_start_hour or hour < self.night_end_hour
        return self.night_start_hour <= hour < self.night_end_hour

    def resolve(self, _requested_model: str) -> tuple[ProviderTarget, ...]:
        if self.is_night_window():
            return tuple(
                ProviderTarget(
                    "paratera", self.paratera_base_url, self.paratera_model, key,
                    self.paratera_min_timeout_seconds,
                )
                for key in self.paratera_keys
            )
        if not self.daytime_key:
            return ()
        return (
            ProviderTarget(
                "daytime", self.daytime_base_url, self.daytime_model,
                self.daytime_key,
            ),
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.resolve("credential-check"))
