"""按配置选择搜索后端。

Tavily 与 DeepSeek 都实现同一个 ``search`` 接口，切换只改 yaml 的
``search.provider``，编排层不需要知道用的是哪一家。
"""

from __future__ import annotations

from app.search.base import SearchError
from app.search.deepseek import DeepSeekSearch
from app.search.tavily import TavilySearch


def build_provider(config):  # noqa: ANN001, ANN201
    """返回可用的搜索实例；没有 key 时返回 None（检索整体跳过）。"""
    if not config.api_key:
        return None

    provider = (config.provider or "tavily").strip().lower()

    if provider == "deepseek":
        return DeepSeekSearch(
            config.api_key,
            endpoint=getattr(config, "endpoint", "") or "",
            timeout_s=getattr(config, "timeout_s", 20.0),
            cache_ttl_seconds=config.cache_ttl_seconds,
        )

    if provider == "tavily":
        return TavilySearch(
            config.api_key,
            cache_ttl_seconds=config.cache_ttl_seconds,
        )

    raise SearchError(f"未知的搜索后端：{config.provider}")


__all__ = ["build_provider"]
