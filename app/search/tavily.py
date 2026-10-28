"""Tavily 检索。

接口：POST https://api.tavily.com/search
鉴权：Authorization: Bearer tvly-...
计费：basic = 1 credit，advanced = 2 credits。默认用 basic，够给 LLM 当依据。
"""

from __future__ import annotations

import hashlib

import httpx

from app.search.base import SearchError, SearchResult
from app.search.cache import TTLCache

ENDPOINT = "https://api.tavily.com/search"
SNIPPET_LIMIT = 900


class TavilySearch:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 20.0,
        search_depth: str = "basic",
        cache_ttl_seconds: int = 21600,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout_s
        self._depth = search_depth
        self._cache = TTLCache(ttl_seconds=cache_ttl_seconds)
        self.last_credits = 1.0 if search_depth == "basic" else 2.0

    @property
    def name(self) -> str:
        return "tavily"

    @staticmethod
    def _cache_key(query: str, max_results: int, depth: str) -> str:
        raw = f"{query.strip().lower()}|{max_results}|{depth}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        key = self._cache_key(query, max_results, self._depth)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        payload = {
            "query": query,
            "search_depth": self._depth,
            "max_results": max(1, min(max_results, 20)),
            "include_answer": False,
            "include_raw_content": False,
            "topic": "general",
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(ENDPOINT, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise SearchError(f"检索请求失败：{exc}") from exc

        if response.status_code >= 400:
            raise SearchError(f"检索返回 HTTP {response.status_code}: {response.text[:200]}")

        data = response.json()
        results = [
            SearchResult(
                title=(item.get("title") or item.get("url") or "").strip(),
                url=item.get("url", ""),
                snippet=(item.get("content") or "").strip()[:SNIPPET_LIMIT],
                published_at=item.get("published_date"),
            )
            for item in data.get("results", [])
            if item.get("url")
        ]

        self._cache.set(key, results)
        return results


def build_provider(config) -> TavilySearch | None:  # noqa: ANN001
    """按配置造 provider；未配置 key 时返回 None（检索降级为关闭）。"""
    if not config.api_key:
        return None
    return TavilySearch(
        config.api_key,
        cache_ttl_seconds=config.cache_ttl_seconds,
    )
