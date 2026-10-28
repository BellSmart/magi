"""加载 config/agents.yaml。

密钥用 ``${ENV:NAME}`` 占位，加载时替换成环境变量值。
这样配置文件可以进仓库，密钥不会。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from app.config import BASE_DIR

_ENV_PATTERN = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


class ProviderConfig(BaseModel):
    base_url: str
    api_key: str = ""
    timeout_s: float = 60.0
    price_in_yuan_per_mtok: float = 0.0
    price_out_yuan_per_mtok: float = 0.0
    # 上下文缓存命中的单价，通常远低于未命中（DeepSeek 约为 1/50）。
    price_in_cache_yuan_per_mtok: float = 0.0
    # 是否支持 {"thinking": {"type": "disabled"}} 这类开关。
    # DeepSeek 默认开启思考模式，思维链会吃掉 max_tokens，短输出任务必须关掉。
    thinking_switch: bool = False

    @property
    def is_mock(self) -> bool:
        return self.base_url.startswith("mock://")

    @property
    def is_configured(self) -> bool:
        return self.is_mock or bool(self.api_key)


class AgentConfig(BaseModel):
    id: str
    order: int
    display: str
    provider: str
    model: str
    persona: str
    temperature: float = 0.7
    tools: list[str] = Field(default_factory=list)
    extra_searches: int = 0
    reasoning_effort: str | None = None
    # 检索角度：用它把命题改写成该人格自己的短查询，再检索。
    # 直接拼关键词会把一整句命题变成关键词汤，召回的往往离题。
    search_angle: str = ""
    # 可选：限定来源域名（留空不限制）。
    search_domains: list[str] = Field(default_factory=list)


class ModelOption(BaseModel):
    """用户可选的模型白名单条目。"""

    provider: str
    model: str
    label: str = ""


class ClassifierConfig(BaseModel):
    provider: str
    model: str
    max_tokens: int = 8


class VotingConfig(BaseModel):
    quorum: int = 2
    veto_on_no: bool = True
    # 用户未显式选择时采用的判定规则：veto（一票否决）/ majority（多数决）
    default_policy: Literal["veto", "majority"] = "veto"
    # weak_yes：条件票是弱赞成，只有压过明确赞成票才降为「状 態」（默认）
    # blocking：任何一张条件票都使结论变为「状 態」（旧语义）
    conditional_policy: Literal["weak_yes", "blocking"] = "weak_yes"


class SearchConfig(BaseModel):
    provider: str = "tavily"
    api_key: str = ""
    shared_max_results: int = 8
    per_agent_max_results: int = 5
    max_calls_per_question: int = 6
    cache_ttl_seconds: int = 21600

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


class AgentsConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    classifier: ClassifierConfig
    voting: VotingConfig = Field(default_factory=VotingConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    agents: list[AgentConfig]
    # 用户可在设置页选择的模型。为空时退化为"每个 agent 的默认模型"。
    models: list[ModelOption] = Field(default_factory=list)

    def provider(self, name: str) -> ProviderConfig:
        if name not in self.providers:
            raise KeyError(f"未定义的 provider: {name}")
        return self.providers[name]

    def agent(self, agent_id: str) -> AgentConfig:
        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        raise KeyError(f"未定义的 agent: {agent_id}")

    @property
    def agents_in_order(self) -> list[AgentConfig]:
        return sorted(self.agents, key=lambda a: a.order)


def model_catalog(config: AgentsConfig) -> list[ModelOption]:
    """用户可选的模型清单。

    没有显式配置 models 时退化为"每个 agent 的默认模型"，保证老配置不改也能用。
    """
    if config.models:
        return list(config.models)

    seen: set[tuple[str, str]] = set()
    catalog: list[ModelOption] = []
    for agent in config.agents:
        key = (agent.provider, agent.model)
        if key in seen:
            continue
        seen.add(key)
        catalog.append(
            ModelOption(provider=agent.provider, model=agent.model, label=agent.model)
        )
    return catalog


def load_agents_config(path: Path | str | None = None) -> AgentsConfig:
    from app.config import get_settings

    target = Path(path or get_settings().agents_config_path)
    if not target.is_absolute():
        target = BASE_DIR / target

    with target.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    return AgentsConfig.model_validate(_resolve(raw))


@lru_cache(maxsize=1)
def get_agents_config() -> AgentsConfig:
    return load_agents_config()


def reset_agents_config_cache() -> None:
    get_agents_config.cache_clear()
