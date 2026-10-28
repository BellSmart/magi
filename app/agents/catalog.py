"""从 provider 的 ``/models`` 拉可用模型列表。

用户把 key 填进来之后，设置页会用这把 key 真打一次 ``GET {base_url}/models``：
既验证 key 能用，也拿到这家此刻真实可用的模型，不再依赖 YAML 里的静态白名单。

结果在进程内缓存 10 分钟，避免每次打开设置页都把六家打一遍。
"""

from __future__ import annotations

import hashlib
import time

import httpx

from app.agents.config import ProviderConfig

TTL_SECONDS = 600
_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}


class ModelListError(Exception):
    """拿不到模型列表（key 错、网络不通、接口不兼容都算）。"""


def _cache_key(provider: ProviderConfig, api_key: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{provider.base_url}|{api_key}".encode()).hexdigest()[:16]
    return (provider.base_url, digest)


def cached_models(provider: ProviderConfig, api_key: str) -> list[str] | None:
    item = _CACHE.get(_cache_key(provider, api_key))
    if item is None:
        return None
    expires_at, models = item
    if expires_at < time.time():
        _CACHE.pop(_cache_key(provider, api_key), None)
        return None
    return models


def fetch_models(
    provider: ProviderConfig, api_key: str, *, timeout: float = 12.0, force: bool = False
) -> list[str]:
    """取模型列表；命中缓存直接返回。失败抛 ModelListError。"""
    if not api_key:
        raise ModelListError("API キーが未登録です")
    if provider.is_mock:
        models = ["mock-model", "mock-model-2"]
        _CACHE[_cache_key(provider, api_key)] = (time.time() + TTL_SECONDS, models)
        return models

    if not force:
        cached = cached_models(provider, api_key)
        if cached is not None:
            return cached

    url = provider.base_url.rstrip("/") + "/models"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                url, headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as exc:
        raise ModelListError(f"接続に失敗しました：{exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:200].replace("\n", " ")
        if response.status_code in (401, 403):
            raise ModelListError("API キーが拒否されました（401/403）")
        raise ModelListError(f"HTTP {response.status_code}: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelListError("応答が JSON ではありません") from exc

    raw = payload.get("data") if isinstance(payload, dict) else payload
    models: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("name")
        else:
            model_id = item
        if model_id:
            models.append(str(model_id))

    # 去重 + 排序，界面上稳定
    models = sorted(dict.fromkeys(models))
    if not models:
        raise ModelListError("モデル一覧が空でした")

    _CACHE[_cache_key(provider, api_key)] = (time.time() + TTL_SECONDS, models)
    return models
