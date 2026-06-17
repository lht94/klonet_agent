"""大模型客户端封装。

这里统一负责初始化 SDK 客户端、选择模型、发送 messages。
上层只关心“给我一个模型响应”，不需要知道具体供应商和 SDK 细节。
"""

from __future__ import annotations

import os
from typing import Any

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv():
        return None

from klonet_agent.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
)


# os.environ 只能读取系统环境变量；load_dotenv 会把 .env 文件里的变量也加载进来。
# 这样本地开发时可以把 DEEPSEEK_API_KEY 写在 .env 中，服务器部署时也可以直接用系统环境变量。
load_dotenv()


class LLMClient:
    """统一的大模型调用入口。

    这个类封装底层 OpenAI SDK 客户端。当前默认连接 DeepSeek 接口，
    后续如果切换模型供应商，只需要优先改这一层。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
    ):
        # api_key: str | None 表示 api_key 可以是字符串，也可以是 None。
        # 如果调用方没有显式传入 api_key，就从环境变量 DEEPSEEK_API_KEY 中读取。
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        # base_url 默认指向 DeepSeek 接口。OpenAI SDK 支持自定义 base_url，
        # 所以这里可以用 OpenAI 客户端去调用 DeepSeek 的 OpenAI-compatible API。
        self.base_url = base_url
        # model 默认从 config.py 读取，避免在多个文件里重复硬编码模型名。
        self.model = model
        # 这里初始化的是底层 SDK 客户端。后续其他模块不应该直接操作它，
        # 而是通过 LLMClient.complete() 发起模型请求。
        from openai import OpenAI

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        stream: bool = False,
    ):
        """发送一次 Chat Completions 请求并返回原始模型响应。

        这个方法替代旧版 runner.py 中直接调用
        client.chat.completions.create(...) 的代码。
        """

        # request 是最终传给 SDK 的参数字典。先集中组装，再用 **request 展开传入，
        # 这样后续增加 temperature、extra_body 等参数时更容易管理。
        request = {
            "model": self.model,
            "messages": messages,
            # stream=False 表示等待模型完整回复后再返回，和旧版行为一致。
            "stream": stream,
            # reasoning_effort 用来控制推理强度，默认值放在 config.py 中统一管理。
            "reasoning_effort": reasoning_effort,
        }
        # tools 是可选参数。没有工具时不传 tools 字段，避免某些模型接口对空列表兼容不好。
        if tools is not None:
            request["tools"] = tools

        # 真正发起 HTTP 请求的位置。上层模块只调用 complete()，
        # 不需要知道 OpenAI SDK 的具体调用链。
        return self.client.chat.completions.create(**request)
