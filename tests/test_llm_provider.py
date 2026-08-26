"""Time-window and failover invariants for the unified LLM provider path."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def _router_at(hour: int, minute: int = 0):
    from klonet_agent.llm.provider import ProviderRouter

    zone = ZoneInfo("Asia/Shanghai")
    return ProviderRouter(
        daytime_key="day-secret",
        paratera_keys=("night-secret-1", "night-secret-2"),
        now=lambda: datetime(2026, 8, 24, hour, minute, tzinfo=zone),
    )


def test_provider_router_uses_glm_from_21_until_09_beijing_time():
    assert _router_at(20, 59).resolve("ignored-model")[0].provider == "daytime"
    assert _router_at(21, 0).resolve("deepseek-model")[0].provider == "paratera"
    assert _router_at(8, 59).resolve("deepseek-model")[0].provider == "paratera"
    assert _router_at(9, 0).resolve("ignored-model")[0].provider == "daytime"


def test_provider_router_uses_the_single_global_daytime_model():
    target = _router_at(12).resolve("caller-specific-model")[0]

    assert target.base_url == "https://api.yyds168.net/v1"
    assert target.model == "gemini-3.7-flash"
    assert target.min_timeout_seconds == 90.0


def test_provider_router_preserves_two_night_keys_without_exposing_them():
    targets = _router_at(23).resolve("deepseek-model")

    assert [item.model for item in targets] == ["GLM-5.2", "GLM-5.2"]
    assert [item.provider for item in targets] == ["paratera", "paratera"]
    assert [item.min_timeout_seconds for item in targets] == [120.0, 120.0]
    rendered = repr(targets)
    assert "night-secret" not in rendered
    assert rendered.count("<redacted>") == 2


def test_llm_client_retries_second_night_key_only_for_provider_failures():
    from klonet_agent.llm.client import LLMClient

    class ProviderFailure(RuntimeError):
        status_code = 402

    calls = []

    class FailingCompletions:
        @staticmethod
        def create(**kwargs):
            calls.append(("first", kwargs["model"]))
            raise ProviderFailure("quota")

    class PassingCompletions:
        @staticmethod
        def create(**kwargs):
            calls.append(("second", kwargs["model"]))
            return "ok"

    clients = {
        "night-secret-1": SimpleNamespace(
            chat=SimpleNamespace(completions=FailingCompletions()),
        ),
        "night-secret-2": SimpleNamespace(
            chat=SimpleNamespace(completions=PassingCompletions()),
        ),
    }
    llm = object.__new__(LLMClient)
    llm.model = "deepseek-model"
    llm._router = _router_at(23)
    llm._client_for = lambda target: clients[target.api_key]

    result = llm.complete([{"role": "user", "content": "hello"}])

    assert result == "ok"
    assert calls == [("first", "GLM-5.2"), ("second", "GLM-5.2")]


def test_llm_client_does_not_hide_contract_error_with_second_key():
    from klonet_agent.llm.client import LLMClient

    calls = []

    class ContractCompletions:
        @staticmethod
        def create(**kwargs):
            calls.append("first")
            raise ValueError("bad local contract")

    class UnexpectedCompletions:
        @staticmethod
        def create(**kwargs):
            calls.append("second")
            return "wrong"

    clients = {
        "night-secret-1": SimpleNamespace(
            chat=SimpleNamespace(completions=ContractCompletions()),
        ),
        "night-secret-2": SimpleNamespace(
            chat=SimpleNamespace(completions=UnexpectedCompletions()),
        ),
    }
    llm = object.__new__(LLMClient)
    llm.model = "deepseek-model"
    llm._router = _router_at(23)
    llm._client_for = lambda target: clients[target.api_key]

    try:
        llm.complete([{"role": "user", "content": "hello"}])
    except ValueError as exc:
        assert str(exc) == "bad local contract"
    else:
        raise AssertionError("contract error must be surfaced")
    assert calls == ["first"]


def test_wrapped_401_rate_limit_rotates_keys_then_retries_with_bounded_backoff(
    monkeypatch,
):
    from klonet_agent.llm.client import LLMClient

    class WrappedRateLimit(RuntimeError):
        status_code = 401
        body = {"error": {"message": "RateLimitError: 您的账户已达到速率限制"}}

    calls = []

    class FirstKey:
        @staticmethod
        def create(**kwargs):
            calls.append("first")
            if calls.count("first") == 1:
                raise WrappedRateLimit("wrapped")
            return "ok"

    class SecondKey:
        @staticmethod
        def create(**kwargs):
            calls.append("second")
            raise WrappedRateLimit("wrapped")

    clients = {
        "night-secret-1": SimpleNamespace(
            chat=SimpleNamespace(completions=FirstKey()),
        ),
        "night-secret-2": SimpleNamespace(
            chat=SimpleNamespace(completions=SecondKey()),
        ),
    }
    sleeps = []
    monkeypatch.setattr("klonet_agent.llm.client.time.sleep", sleeps.append)
    llm = object.__new__(LLMClient)
    llm.model = "deepseek-model"
    llm._router = _router_at(23)
    llm._client_for = lambda target: clients[target.api_key]
    llm._rate_limit_max_attempts = 4
    llm._rate_limit_backoff_seconds = 1
    llm._rate_limit_max_backoff_seconds = 8

    assert llm.complete([{"role": "user", "content": "hello"}]) == "ok"
    assert calls == ["first", "second", "first"]
    assert sleeps == [1]


def test_genuine_401_never_cycles_back_to_same_key(monkeypatch):
    from klonet_agent.llm.client import LLMClient

    class AuthenticationFailure(RuntimeError):
        status_code = 401
        body = {"error": {"message": "invalid api key"}}

    calls = []

    class Failing:
        def __init__(self, name):
            self.name = name

        def create(self, **kwargs):
            calls.append(self.name)
            raise AuthenticationFailure("invalid")

    clients = {
        key: SimpleNamespace(
            chat=SimpleNamespace(completions=Failing(name)),
        )
        for key, name in (
            ("night-secret-1", "first"),
            ("night-secret-2", "second"),
        )
    }
    monkeypatch.setattr(
        "klonet_agent.llm.client.time.sleep",
        lambda _delay: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )
    llm = object.__new__(LLMClient)
    llm.model = "deepseek-model"
    llm._router = _router_at(23)
    llm._client_for = lambda target: clients[target.api_key]
    llm._rate_limit_max_attempts = 8

    try:
        llm.complete([{"role": "user", "content": "hello"}])
    except AuthenticationFailure:
        pass
    else:
        raise AssertionError("authentication failure must surface")
    assert calls == ["first", "second"]


def test_exhausted_wrapped_rate_limit_reports_throttling_not_authentication(
    monkeypatch,
):
    from klonet_agent.llm.client import LLMClient

    class WrappedRateLimit(RuntimeError):
        status_code = 401
        body = {"error": {"message": "RateLimitError: rate limit"}}

    class Failing:
        @staticmethod
        def create(**kwargs):
            raise WrappedRateLimit("gateway authentication wrapper")

    client = SimpleNamespace(chat=SimpleNamespace(completions=Failing()))
    monkeypatch.setattr("klonet_agent.llm.client.time.sleep", lambda _delay: None)
    llm = object.__new__(LLMClient)
    llm.model = "deepseek-model"
    llm._router = _router_at(23)
    llm._client_for = lambda _target: client
    llm._rate_limit_max_attempts = 3
    llm._rate_limit_backoff_seconds = 0
    llm._rate_limit_max_backoff_seconds = 0

    try:
        llm.complete([{"role": "user", "content": "hello"}])
    except RuntimeError as exc:
        assert "rate limit remained active" in str(exc)
        assert isinstance(exc.__cause__, WrappedRateLimit)
    else:
        raise AssertionError("bounded rate-limit exhaustion must surface")
