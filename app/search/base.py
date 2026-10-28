"""检索层的抽象。

换供应商（Brave / Serper / 自建 SearXNG）只需要再实现一个 search()，
上层 orchestrator 与 agent 不感知差异。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SearchError(Exception):
    pass


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None


class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        ori_question: str | None = None,
    ) -> list[SearchResult]:
        ...
