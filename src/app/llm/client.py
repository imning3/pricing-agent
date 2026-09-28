"""LLM 抽象层：mock / litellm（OpenAI 兼容：在线 API、vLLM、Ollama 均可）。

统一接口 chat_json(system, user) -> dict|list；
litellm 不可导入时启动即报配置错误（mock 模式不需要）。
"""
from __future__ import annotations

import os
from typing import Protocol

from app.core.errors import LLMError
from app.pipeline.extractor import parse_json_block


class LLMClient(Protocol):
    async def chat_json(self, system: str, user: str) -> dict | list: ...
    async def chat_text(self, system: str, user: str) -> str: ...


class MockClient:
    """确定性假模型：联调用。chat_json 返回空数组（由 MockExtractor 兜底，不走此路径）。"""

    async def chat_json(self, system: str, user: str) -> dict | list:
        return []

    async def chat_text(self, system: str, user: str) -> str:
        return "（mock 回复）收到您的消息，当前处于 mock 模式。"


class LiteLLMClient:
    def __init__(self, model: str, base_url: str, api_key: str, timeout: int):
        try:
            import litellm  # 延迟导入：mock 模式无需安装
        except ImportError as exc:
            raise LLMError("LLM_BACKEND=litellm 但未安装 litellm（pip install litellm）", retryable=False) from exc
        # 内网适配：关闭遥测上报与外网价格表拉取（服务器无外网，避免静默重试拖慢调用）
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        self._litellm = litellm
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout

    async def _acompletion(self, system: str, user: str, temperature: float = 0.1):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        # base_url/api_base 同传：openai 兼容方（vLLM/Ollama/中转）取 base_url，
        # anthropic 等原生方取 api_base，litellm 按模型前缀路由
        resp = await self._litellm.acompletion(
            model=self._model, messages=messages, temperature=temperature,
            timeout=self._timeout,
            base_url=self._base_url or None, api_base=self._base_url or None,
            api_key=self._api_key or None,
        )
        return resp.choices[0].message.content or ""

    async def chat_json(self, system: str, user: str) -> dict | list:
        text = await self._acompletion(system, user)
        return parse_json_block(text)

    async def chat_text(self, system: str, user: str) -> str:
        return await self._acompletion(system, user, temperature=0.5)
