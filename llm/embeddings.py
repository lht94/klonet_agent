"""OpenAI-compatible embedding client for semantic retrieval adapters."""

from __future__ import annotations

import os
from typing import Any, Sequence

from klonet_agent.config import (
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
)


class EmbeddingError(RuntimeError):
    """embedding 调用失败的基类。

    记忆库的 worker（阶段 4）要区分"值得退避重试"和"重试也没有意义"，所以
    不把所有失败都塞进一个类型里——否则一次维度写错会被当成网络抖动重试到
    天荒地老。
    """


class EmbeddingUnavailable(EmbeddingError):
    """供应商侧不可用：网络、超时、限流、鉴权或 5xx。

    标记为可重试。鉴权失败重试同样不会成功，但它属于"部署配置要修"，
    与"代码里维度写错了"是两类问题，故仍归在这里，由运维从 last_error 里看出来。
    """


class EmbeddingDimensionMismatch(EmbeddingError):
    """返回向量的维度与期望不符。

    这是配置错误（换过模型但没换 schema），重试不会变好。必须显式失败，
    而不是截断或补齐——那样写进库的向量是错的，而且不会有任何报错。
    """

    # 消费者（记忆库的 embedding worker）用这个标记决定"不重试"，
    # 而不需要 import 本模块或判断异常类型——embedder 是被注入的。
    permanent = True


# 供应商异常按名字解析而不是直接 import：`openai` 在这个仓库里是可选依赖，
# 只在真正构造客户端时才被要求存在。
_PROVIDER_ERROR_NAMES = (
    "APIError",
    "APIConnectionError",
    "APITimeoutError",
    "RateLimitError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "BadRequestError",
    "UnprocessableEntityError",
    "InternalServerError",
    "APIStatusError",
)

_provider_error_types: tuple[type, ...] | None = None


def _provider_error_types_cached() -> tuple[type, ...]:
    global _provider_error_types
    if _provider_error_types is None:
        try:
            import openai
        except Exception:  # pragma: no cover - 取决于可选依赖
            _provider_error_types = ()
        else:
            _provider_error_types = tuple(
                candidate
                for candidate in (
                    getattr(openai, name, None) for name in _PROVIDER_ERROR_NAMES
                )
                if isinstance(candidate, type)
            )
    return _provider_error_types


def get_embedding_api_key() -> str | None:
    """Return the first configured embedding API key."""

    return (
        os.environ.get("EMBEDDING_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def build_default_embedding_provider():
    """Build the default embedding callable when credentials are configured."""

    if not get_embedding_api_key():
        return None
    return EmbeddingClient().embed_text


class EmbeddingClient:
    """Small adapter around an OpenAI-compatible embeddings endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_EMBEDDING_BASE_URL,
        model: str = DEFAULT_EMBEDDING_MODEL,
        client: Any | None = None,
        *,
        dimensions: int | None = None,
        model_version: str | None = None,
    ):
        self.api_key = api_key or get_embedding_api_key()
        self.base_url = base_url
        self.model = model
        # 期望维度。为 None 时不校验，既有调用方零影响；
        # 记忆库会显式传 1024（对应 schema 里的 vector(1024)）。
        self.dimensions = dimensions
        # 写进 memory_versions.embedding_version 的身份串。默认取模型名——
        # 换模型必然换向量空间，用模型名当版本比一个自增数字更能说明问题。
        self.model_version = model_version or model
        if client is not None:
            self.client = client
        else:
            from openai import OpenAI

            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    @property
    def identity(self) -> tuple[str, str]:
        """``(model, version)``。

        两者必须一起落库：只存向量不存身份，事后无法判断这个向量是哪个模型
        算出来的，也就无法在换模型时决定哪些需要重算。
        """

        return (self.model, self.model_version)

    def embed_text(self, text: str) -> tuple[float, ...]:
        """Return a dense vector for one text input."""

        response = self._create(input=text)
        if not getattr(response, "data", None):
            return ()
        embedding = getattr(response.data[0], "embedding", None) or ()
        return self._coerce(embedding)

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Return embeddings in one request when the provider supports batching."""

        inputs = tuple(str(text) for text in texts)
        if not inputs:
            return ()
        response = self._create(input=list(inputs))
        if not getattr(response, "data", None):
            return ()
        ordered = sorted(
            response.data,
            key=lambda item: int(getattr(item, "index", 0)),
        )
        return tuple(
            self._coerce(getattr(item, "embedding", None) or ())
            for item in ordered
        )

    # ------------------------------------------------------------- 内部 --

    def _create(self, *, input: Any) -> Any:
        """调用供应商，并把它的异常翻译成本模块的分类。

        请求参数与顺序刻意与改造前逐字一致，既有测试对请求体的断言不受影响。
        """

        try:
            return self.client.embeddings.create(
                model=self.model,
                input=input,
            )
        except EmbeddingError:
            raise
        except Exception as exc:
            if isinstance(exc, _provider_error_types_cached()):
                raise EmbeddingUnavailable(
                    f"embedding 服务调用失败（{type(exc).__name__}）：{exc}"
                ) from exc
            # 非供应商异常（编码错误、Fake client 抛的断言失败之类）原样冒泡：
            # 把它们归类成"服务不可用"会掩盖真正的编程错误。
            raise

    def _coerce(self, embedding: Any) -> tuple[float, ...]:
        vector = tuple(float(value) for value in embedding)
        expected = self.dimensions
        # 空向量不在这里判失败：既有调用方（知识库 / intent case）依赖"空即无结果"
        # 的语义。记忆库侧由 worker 显式把空向量当失败处理。
        if expected is not None and vector and len(vector) != expected:
            raise EmbeddingDimensionMismatch(
                f"embedding 返回 {len(vector)} 维，期望 {expected} 维"
                f"（模型 {self.model!r}）。换模型必须同时换 schema 并重嵌入，"
                "不能在这里截断或补齐"
            )
        return vector
