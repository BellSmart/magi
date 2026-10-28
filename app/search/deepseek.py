"""DeepSeek 自研搜索引擎。

POST https://api.deepseek.com/v1/tools/search
鉴权与模型接口相同（Bearer），计费按次（5 元 / 1000 次），QPS 100。

相比 Tavily 的好处：
- 和模型共用同一个 key，不用再维护第二个供应商；
- 便宜一个数量级，因此可以多轮检索；
- 支持 site: / lang: / after: 等语法，便于给每个人格做来源偏置。
"""

from __future__ import annotations

import hashlib

import httpx

from app.search.base import SearchError, SearchResult
from app.search.cache import TTLCache

DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
SNIPPET_LIMIT = 900


class DeepSeekSearch:
    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_s: float = 20.0,
        cache_ttl_seconds: int = 21600,
        need_content: bool = False,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint or DEFAULT_ENDPOINT
        self._timeout = timeout_s
        self._cache = TTLCache(ttl_seconds=cache_ttl_seconds)
        self._need_content = need_content
        self.last_credits = 1.0

    @property
    def name(self) -> str:
        return "deepseek"

    @staticmethod
    def _cache_key(query: str, max_results: int, domains: list[str]) -> str:
        raw = f"{query.strip()}|{max_results}|{','.join(sorted(domains))}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        ori_question: str | None = None,
    ) -> list[SearchResult]:
        domains = include_domains or []
        key = self._cache_key(query, max_results, domains)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        # DeepSeek 用搜索语法表达站点限制，而不是独立参数。
        full_query = query
        if domains:
            full_query = f"{query} " + " ".join(f"site:{d}" for d in domains)
        full_query = full_query[:128]

        payload: dict = {
            "query": full_query,
            "count": max(1, min(max_results, 50)),
            "need_content": self._need_content,
        }
        if ori_question:
            payload["ori_question"] = ori_question[:128]

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise SearchError(f"检索请求失败：{exc}") from exc

        if response.status_code >= 400:
            raise SearchError(
                f"检索返回 HTTP {response.status_code}: {response.text[:200]}"
            )

        data = response.json()
        items = data.get("results") or data.get("data") or []

        results = [
            SearchResult(
                title=(item.get("title") or item.get("url") or "").strip(),
                url=item.get("url", ""),
                snippet=(
                    item.get("summary") or item.get("snippet") or item.get("content") or ""
                ).strip()[:SNIPPET_LIMIT],
                published_at=item.get("published_at") or item.get("date"),
            )
            for item in items
            if item.get("url")
        ]

        self._cache.set(key, results)
        return results
