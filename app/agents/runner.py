"""单个 agent 的执行：作答 → 读票。

票型优先从回答首行读（模型被要求以 YES/NO/CONDITIONAL 开头），
读不出来才回退到一次 LLM 归类调用。常见路径因此只花一次调用，
而不是上游那样每台两次。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.agents.classifier import (
    VOTABLE_FORMS,
    classify_answer,
    parse_leading_vote,
    strip_leading_vote,
)
from app.agents.config import AgentConfig, AgentsConfig
from app.agents.answer import DEFAULT_EFFORT, derive_words, normalise_effort
from app.agents.prompts import build_system_prompt, build_user_message
from app.agents.provider import ProviderError, Usage, chat


@dataclass
class AgentOutcome:
    agent_id: str
    display: str
    provider: str
    model: str
    status: str  # yes / no / conditional / abstain / error / info
    answer: str | None = None
    conditions: str | None = None
    error: str | None = None
    latency_ms: int = 0
    usage: Usage = field(default_factory=Usage)

    @property
    def vote(self) -> str | None:
        return self.status if self.status in {"yes", "no", "conditional"} else None


def _failure(agent: AgentConfig, exc: Exception, latency_ms: int) -> AgentOutcome:
    """瞬时故障记弃权，结构性故障记错误。

    一次 429 或超时不该让另两台的有效票全部作废。
    """
    if isinstance(exc, asyncio.TimeoutError):
        return AgentOutcome(
            agent.id, agent.display, agent.provider, agent.model,
            "abstain", error="超时未返回", latency_ms=latency_ms,
        )

    if isinstance(exc, ProviderError) and exc.is_transient:
        return AgentOutcome(
            agent.id, agent.display, agent.provider, agent.model,
            "abstain", error=str(exc)[:500], latency_ms=latency_ms,
        )

    return AgentOutcome(
        agent.id, agent.display, agent.provider, agent.model,
        "error", error=str(exc)[:500], latency_ms=latency_ms,
    )


async def run_agent(
    *,
    cfg: AgentsConfig,
    agent: AgentConfig,
    question: str,
    form: str,
    sources: list[dict] | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> AgentOutcome:
    provider = cfg.provider(agent.provider)
    votable = form in VOTABLE_FORMS
    limit = max_tokens or 2000
    effort = normalise_effort(reasoning_effort or DEFAULT_EFFORT)
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                agent, sources=bool(sources), votable=votable
            ),
        },
        {
            "role": "user",
            "content": build_user_message(
                question, sources, form=form, length_words=derive_words(limit)
            ),
        },
    ]

    try:
        completion = await asyncio.wait_for(
            chat(
                provider,
                model=agent.model,
                messages=messages,
                temperature=agent.temperature,
                max_tokens=limit,
                extra={"reasoning_effort": effort},
                meta={"role": "answer", "agent": agent.id},
            ),
            timeout=provider.timeout_s,
        )
    except (asyncio.TimeoutError, ProviderError) as exc:
        return _failure(agent, exc, elapsed())

    raw = (completion.content or "").strip()
    if not raw:
        return AgentOutcome(
            agent.id, agent.display, agent.provider, agent.model,
            "error", error="模型返回空内容", latency_ms=elapsed(), usage=completion.usage,
        )

    if not votable:
        return AgentOutcome(
            agent.id, agent.display, agent.provider, agent.model,
            "info", answer=raw, latency_ms=elapsed(), usage=completion.usage,
        )

    vote = parse_leading_vote(raw)
    if vote is None:
        # 模型没按规定开场，兜底再问一次；仍读不出就弃权，绝不猜。
        try:
            vote = await asyncio.wait_for(
                classify_answer(cfg, agent, question, raw),
                timeout=provider.timeout_s,
            )
        except (asyncio.TimeoutError, ProviderError) as exc:
            outcome = _failure(agent, exc, elapsed())
            outcome.status = "abstain"
            outcome.answer = raw
            outcome.error = f"票型归类失败：{outcome.error}"
            return outcome

        if vote is None:
            return AgentOutcome(
                agent.id, agent.display, agent.provider, agent.model,
                "abstain", answer=raw, error="无法从回答中读出票型",
                latency_ms=elapsed(), usage=completion.usage,
            )

    body = strip_leading_vote(raw) or raw
    return AgentOutcome(
        agent.id, agent.display, agent.provider, agent.model,
        vote,
        answer=body,
        conditions=body if vote == "conditional" else None,
        latency_ms=elapsed(),
        usage=completion.usage,
    )
