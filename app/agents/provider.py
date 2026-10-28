"""OpenAI 兼容的聊天客户端。

三家 provider（DeepSeek / Kimi / 千问）都实现 ``/v1/chat/completions``，
所以这里只需要一个实现；差异全部收在配置里。

三条硬性约定：
1. **不用模块级全局 api_key**（上游 ai.py 的写法在多线程下有竞态）。
2. 只对可恢复错误重试（429 / 5xx / 连接错误）；4xx 参数错误重试是白烧钱。
3. 每次调用都回报 token 用量，成本必须能算出来。
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agents.config import ProviderConfig

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable

    @property
    def is_transient(self) -> bool:
        """瞬时故障判为弃权而不是错误：一次抖动不该否决整个结果。"""
        return self.retryable or (self.status is not None and self.status in RETRYABLE_STATUS)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_yuan: float = 0.0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hit_tokens + self.cache_miss_tokens
        return round(self.cache_hit_tokens / total * 100, 1) if total else 0.0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Completion:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""


def _cost(provider: ProviderConfig, usage: dict[str, Any]) -> Usage:
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)

    # DeepSeek 返回 prompt_cache_hit_tokens / prompt_cache_miss_tokens；
    # OpenAI 系则是 prompt_tokens_details.cached_tokens。两种都认。
    hit = int(
        usage.get("prompt_cache_hit_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    miss = int(usage.get("prompt_cache_miss_tokens") or max(prompt - hit, 0))

    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        cost_yuan=(
            miss / 1_000_000 * provider.price_in_yuan_per_mtok
            + hit / 1_000_000 * provider.price_in_cache_yuan_per_mtok
            + completion / 1_000_000 * provider.price_out_yuan_per_mtok
        ),
    )


async def chat(
    provider: ProviderConfig,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> Completion:
    if provider.is_mock:
        return _mock_completion(model=model, meta=meta)

    if not provider.api_key:
        # 早失败：否则会以空 Bearer 打过去，换回一个难懂的 401。
        raise ProviderError(
            f"{provider.base_url} 未配置 API key，请检查环境变量", status=401
        )

    payload: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
    if extra:
        payload.update(extra)

    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }

    last_error: ProviderError | None = None
    async with httpx.AsyncClient(timeout=provider.timeout_s) as client:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await client.post(url, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = ProviderError(
                    f"连接 {provider.base_url} 失败：{exc}", retryable=True
                )
            else:
                if response.status_code < 400:
                    return _parse(response.json(), provider)

                body = response.text[:500]
                last_error = ProviderError(
                    f"HTTP {response.status_code}: {body}",
                    status=response.status_code,
                    retryable=response.status_code in RETRYABLE_STATUS,
                )
                if not last_error.retryable:
                    raise last_error

            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 * (2**attempt) + random.uniform(0, 0.25))

    assert last_error is not None
    raise last_error


def _parse(data: dict[str, Any], provider: ProviderConfig) -> Completion:
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - 防御性
        raise ProviderError(f"响应格式异常：{data!r}"[:500]) from exc

    calls = [
        ToolCall(
            id=call.get("id", ""),
            name=call.get("function", {}).get("name", ""),
            arguments=call.get("function", {}).get("arguments", "{}"),
        )
        for call in message.get("tool_calls") or []
    ]

    return Completion(
        content=message.get("content"),
        tool_calls=calls,
        usage=_cost(provider, data.get("usage") or {}),
        model=data.get("model", ""),
    )


# --- mock provider ---------------------------------------------------------
#
# 用 ``base_url: mock://`` 启用，仅供本地链路验证与测试，不产生任何网络请求。
# 三个 agent 给出不同的票型，正好覆盖聚合规则。

_MOCK_VOTES = {
    "melchior": ("yes", "Yes."),
    "balthasar": ("no", "No."),
    "casper": ("yes", "Yes."),
}


def _mock_completion(*, model: str, meta: dict[str, Any] | None) -> Completion:
    role = (meta or {}).get("role", "answer")
    agent = (meta or {}).get("agent", "melchior")
    vote, text = _MOCK_VOTES.get(agent, ("yes", "Yes."))

    if role == "classify_form":
        content = "QUESTION"
    elif role == "classify_answer":
        content = vote
    elif role == "rewrite_query":
        content = f"mock 検索クエリ {agent}"
    else:
        # 与真实路径一致：首行表态，正文另起。
        content = f"{vote.upper()}\n\n（mock {agent}）{text}"

    return Completion(
        content=content,
        usage=Usage(prompt_tokens=42, completion_tokens=7, cost_yuan=0.0),
        model=f"mock:{model}",
    )
