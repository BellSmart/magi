"""一次提问的编排。

流程：分类 → （检索）→ 三路并发 → 逐票落库并广播 → 聚合 → 定稿。

关键取舍：三路 agent 用 ``asyncio.as_completed``，谁先回来谁先推送，
而不是等三路齐了再一起算——页面上的闪烁状态因此有真实语义。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from sqlalchemy import select

from app.agents.aggregator import REASON_TEXT, aggregate
from app.agents.classifier import VOTABLE_FORMS, classify_form, rewrite_queries
from app.agents.config import AgentConfig, get_agents_config, model_catalog
from app.agents.runner import AgentOutcome, run_agent
from app.db import session_scope
from app.models import (
    AgentRun,
    Question,
    QuestionEvent,
    SearchCall,
    Source,
    User,
    UserAgentSetting,
    utcnow,
)
from app.search.base import SearchError
from app.search.factory import build_provider

logger = logging.getLogger(__name__)

# 后台任务集合：不持有引用的话，任务可能在跑完前被 GC 掉。
_RUNNING: set[asyncio.Task[None]] = set()

# provider 带进程内缓存，复用同一个实例才有意义。
_search_provider = None
_search_signature: tuple[str, int] | None = None
SEARCH_TIMEOUT_SECONDS = 25.0

# 送进 prompt 的来源上限。查得多不等于全塞进去：
# 14 条 × 400 字会把 prompt 撑到 3000+ token，边际收益很低。
PROMPT_SOURCE_CAP = 11

# 合并时给"本台自己的检索结果"预留的条数
SOURCE_EXTRA_SLOTS = 4

# 共享检索的视角：既要事实，也要反方，避免三台都只看到支持性材料。
SHARED_ANGLE = (
    "a neutral factual overview: what is actually being claimed, what the "
    "mainstream evidence says, and what the strongest opposing view is"
)


def _norm_title(title: str | None) -> str:
    """标题归一：同一篇文章在不同镜像站的标题只差后缀，按前缀去重。"""
    return re.sub(r"[\W_]+", "", (title or "").lower())[:40]


def _is_duplicate_title(key: str, seen: set[str]) -> bool:
    """互为前缀即视为同一篇（镜像站常给标题加" - 版权归…"之类的尾巴）。"""
    if len(key) < 8:
        return False
    return any(key.startswith(s) or s.startswith(key) for s in seen)


def _dedupe(items: list, seen_urls: set[str], seen_titles: set[str]) -> list:
    kept = []
    for item in items:
        url = getattr(item, "url", None) or item.get("url")
        title = getattr(item, "title", None) or item.get("title")
        tkey = _norm_title(title)
        if url in seen_urls or _is_duplicate_title(tkey, seen_titles):
            continue
        seen_urls.add(url)
        if tkey:
            seen_titles.add(tkey)
        kept.append(item)
    return kept


def _merge_sources(
    base: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    cap: int = PROMPT_SOURCE_CAP,
    extra_slots: int = SOURCE_EXTRA_SLOTS,
) -> list[dict[str, Any]]:
    """共享基线 + 本台新增。

    必须先给 extra 留位置：基线一旦占满 cap，截断后本台自己的检索结果
    会被整体挤掉，三台看到的又变成同一份（这个坑踩过一次）。
    """
    seen_urls = {item.get("url") for item in base}
    seen_titles = {_norm_title(item.get("title")) for item in base}
    picked_extra = _dedupe(extra, seen_urls, seen_titles)[:extra_slots]
    head = base[: max(0, cap - len(picked_extra))]
    return head, picked_extra


def emit(question_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
    with session_scope() as db:
        db.add(
            QuestionEvent(
                question_id=question_id, type=event_type, payload=payload or {}
            )
        )


def _get_search_provider(cfg):  # noqa: ANN001, ANN202
    global _search_provider, _search_signature
    if not cfg.search.enabled:
        return None
    signature = (
        cfg.search.provider,
        cfg.search.api_key,
        cfg.search.endpoint,
        cfg.search.cache_ttl_seconds,
    )
    if _search_provider is None or _search_signature != signature:
        _search_provider = build_provider(cfg.search)
        _search_signature = signature
    return _search_provider


def reset_search_provider() -> None:
    global _search_provider, _search_signature
    _search_provider = None
    _search_signature = None


def _persist_search(
    question_id: str,
    query: str,
    results: list,  # noqa: ANN001
    latency_ms: int,
    credits: float,
    scope: str = "shared",
    provider_name: str = "unknown",
) -> None:
    with session_scope() as db:
        call = SearchCall(
            question_id=question_id,
            scope=scope,
            query=query,
            provider=provider_name,
            hit_count=len(results),
            latency_ms=latency_ms,
            credits=credits,
        )
        db.add(call)
        db.flush()
        for rank, item in enumerate(results):
            db.add(
                Source(
                    search_call_id=call.id,
                    url=item.url,
                    title=item.title,
                    snippet=item.snippet,
                    published_at=item.published_at,
                    rank=rank,
                )
            )


async def _run_queries(
    cfg,  # noqa: ANN001
    question_id: str,
    queries: list[str],
    *,
    max_results: int,
    scope: str,
    include_domains: list[str] | None = None,
    ori_question: str | None = None,
) -> list:
    """并发跑一批查询，逐条落库，返回按 URL 去重后的结果。"""
    provider = _get_search_provider(cfg)
    if provider is None:
        return []

    async def one(query: str):
        started = time.perf_counter()
        try:
            found = await asyncio.wait_for(
                provider.search(
                    query,
                    max_results=max_results,
                    include_domains=include_domains,
                    ori_question=ori_question,
                ),
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, SearchError) as exc:
            logger.warning("%s 的检索失败（%s）：%s", scope, query[:40], exc)
            return None
        latency = int((time.perf_counter() - started) * 1000)
        _persist_search(
            question_id, query, found, latency,
            getattr(provider, "last_credits", 0.0), scope=scope,
            provider_name=getattr(provider, "name", cfg.search.provider),
        )
        return found

    batches = await asyncio.gather(*(one(q) for q in queries))

    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    merged: list = []
    for batch in batches:
        merged.extend(_dedupe(batch or [], seen_urls, seen_titles))
    return merged


async def _shared_search(
    cfg, question_id: str, text: str  # noqa: ANN001
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """共享基线：三台共同的事实托底。

    跑多路查询（事实 + 反方），确保不只是搜到支持性材料。
    即使某台自己的检索跑偏，它也还有这份基线可依。
    """
    provider = _get_search_provider(cfg)
    if provider is None:
        return [], {"enabled": False, "count": 0, "results": []}

    queries = await rewrite_queries(
        cfg, text, angle=SHARED_ANGLE, count=cfg.search.shared_queries
    )
    results = await _run_queries(
        cfg, question_id, queries,
        max_results=cfg.search.shared_max_results,
        scope="shared",
        ori_question=" ".join(text.split())[:128],
    )

    for_model = [
        {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "published_at": item.published_at,
        }
        for item in results
    ]
    return for_model, {
        "enabled": True,
        "count": len(for_model),
        "queries": queries,
        "results": [
            {"title": item["title"], "url": item["url"], "snippet": item["snippet"][:280]}
            for item in for_model
        ],
    }


async def _agent_search(
    cfg, question_id: str, text: str, agent  # noqa: ANN001
) -> tuple[list[str], list[dict[str, Any]]]:
    """该人格自己的检索。

    分歧如果只来自"对同一份材料的权重不同"，那是推理风格的差异；
    让三台拿到的材料本身就有偏向，才会出现"你们看的根本不是同一批证据"。

    查询先按该人格的视角改写成英文短句（正反各一），而不是把关键词
    糊在命题后面——后者会让检索抓住关键词而不是议题。
    """
    provider = _get_search_provider(cfg)
    if provider is None or not agent.search_angle:
        return [], []

    queries = await rewrite_queries(
        cfg, text, angle=agent.search_angle, count=cfg.search.per_agent_queries
    )
    results = await _run_queries(
        cfg, question_id, queries,
        max_results=cfg.search.per_agent_max_results,
        scope=agent.id,
        include_domains=agent.search_domains or None,
        ori_question=" ".join(text.split())[:128],
    )
    return queries, [
        {
            "title": item.title,
            "url": item.url,
            "snippet": item.snippet,
            "published_at": item.published_at,
        }
        for item in results
    ]


def _persist_outcome(question_id: str, outcome: AgentOutcome) -> None:
    with session_scope() as db:
        db.add(
            AgentRun(
                question_id=question_id,
                agent_id=outcome.agent_id,
                display_name=outcome.display,
                provider=outcome.provider,
                model=outcome.model,
                status=outcome.status,
                vote=outcome.vote,
                answer=outcome.answer,
                conditions=outcome.conditions,
                error=outcome.error,
                latency_ms=outcome.latency_ms,
                prompt_tokens=outcome.usage.prompt_tokens,
                completion_tokens=outcome.usage.completion_tokens,
                cache_hit_tokens=outcome.usage.cache_hit_tokens,
                cache_miss_tokens=outcome.usage.cache_miss_tokens,
                cost_yuan=outcome.usage.cost_yuan,
                finished_at=utcnow(),
            )
        )


def _vote_payload(
    outcome: AgentOutcome, sources: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "agent": outcome.agent_id,
        "display": outcome.display,
        "status": outcome.status,
        "answer": outcome.answer,
        "conditions": outcome.conditions,
        "error": outcome.error,
        "latency_ms": outcome.latency_ms,
        "model": outcome.model,
        # 该台实际依据的来源（可能是它自己的偏置检索结果）
        "sources": [
            {"title": item.get("title"), "url": item.get("url")}
            for item in (sources or [])[:8]
        ],
    }


def _effective_agents(cfg, user_id: int) -> list[AgentConfig]:  # noqa: ANN001
    """把用户自己的模型选择套到默认人格配置上。

    任何一项不合法（模型被移出白名单、provider 缺 key）就回落到默认值，
    而不是让整次提问失败。
    """
    with session_scope() as db:
        rows = db.scalars(
            select(UserAgentSetting).where(UserAgentSetting.user_id == user_id)
        ).all()
    overrides = {row.agent_id: row for row in rows}
    if not overrides:
        return cfg.agents_in_order

    allowed = {(option.provider, option.model) for option in model_catalog(cfg)}
    resolved: list[AgentConfig] = []
    for agent in cfg.agents_in_order:
        row = overrides.get(agent.id)
        if (
            row is not None
            and (row.provider, row.model) in allowed
            and cfg.provider(row.provider).is_configured
        ):
            resolved.append(
                agent.model_copy(update={"provider": row.provider, "model": row.model})
            )
        else:
            resolved.append(agent)
    return resolved


def spawn(question_id: str) -> None:
    """在当前事件循环里起一个后台任务。"""
    task = asyncio.get_running_loop().create_task(run_question(question_id))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


async def run_question(question_id: str) -> None:
    started = time.perf_counter()
    cfg = get_agents_config()

    with session_scope() as db:
        question = db.get(Question, question_id)
        if question is None:
            logger.warning("question %s 不存在，跳过", question_id)
            return
        text = question.text
        user_id = question.user_id
        max_tokens = question.max_tokens
        reasoning_effort = question.reasoning_effort
        owner = db.get(User, user_id)
        vote_policy = (owner.vote_policy if owner else None) or cfg.voting.default_policy

    agents = _effective_agents(cfg, user_id)

    emit(question_id, "question.started", {"text": text})

    try:
        form = await classify_form(cfg, text)
    except Exception as exc:  # noqa: BLE001 - 分类失败不该让整次提问消失
        logger.exception("分类失败")
        _finish(
            question_id,
            status="error",
            verdict=None,
            degraded=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        emit(question_id, "error", {"message": f"问题分类失败：{exc}"[:500]})
        emit(question_id, "done", {})
        return

    votable = form in VOTABLE_FORMS
    with session_scope() as db:
        question_row = db.get(Question, question_id)
        question_row.form = form
        question_row.is_yes_no = votable
    emit(question_id, "question.classified", {"form": form, "is_yes_no": votable})

    # 共享检索：给三台一个共同的事实基线。
    sources, search_event = await _shared_search(cfg, question_id, text)
    emit(question_id, "search.shared", search_event)

    # 各自检索：在共享基线上再按人格视角追加，让三台的材料既不完全相同、
    # 又都有人兜底。某一台的查询跑偏时，它至少还有基线可依。
    agent_sources: dict[str, list[dict[str, Any]]] = {}
    if cfg.search.enabled and any(agent.search_angle for agent in agents):
        per_agent = await asyncio.gather(
            *(_agent_search(cfg, question_id, text, agent) for agent in agents),
            return_exceptions=True,
        )
        for agent, result in zip(agents, per_agent):
            if isinstance(result, Exception):
                continue
            queries, found = result
            head, own = _merge_sources(sources, found)
            merged = head + own
            if found:
                agent_sources[agent.id] = merged
            emit(
                question_id,
                "search.agent",
                {
                    "agent": agent.id,
                    "queries": queries,
                    "count": len(merged),
                    "added": len(found),
                    "results": [
                        {"title": r["title"], "url": r["url"]} for r in merged
                    ],
                },
            )

    tasks = [
        asyncio.create_task(
            run_agent(
                cfg=cfg,
                agent=agent,
                question=text,
                form=form,
                sources=agent_sources.get(agent.id) or sources or None,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
            )
        )
        for agent in agents
    ]

    outcomes: dict[str, AgentOutcome] = {}
    try:
        for completed in asyncio.as_completed(tasks):
            outcome = await completed
            outcomes[outcome.agent_id] = outcome
            _persist_outcome(question_id, outcome)
            emit(
                question_id,
                "agent.vote",
                _vote_payload(
                    outcome, agent_sources.get(outcome.agent_id) or sources
                ),
            )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    ordered = [outcomes[agent.id] for agent in agents if agent.id in outcomes]
    verdict = aggregate(
        [outcome.status for outcome in ordered],
        is_yes_no=votable,
        quorum=cfg.voting.quorum,
        policy=vote_policy,
        veto_on_no=cfg.voting.veto_on_no,
        conditional_policy=cfg.voting.conditional_policy,
    )

    latency_ms = int((time.perf_counter() - started) * 1000)
    _finish(
        question_id,
        status=verdict.status,
        verdict=verdict.status,
        degraded=verdict.degraded,
        latency_ms=latency_ms,
    )

    emit(
        question_id,
        "verdict",
        {
            "status": verdict.status,
            "tally": verdict.tally,
            "degraded": verdict.degraded,
            "reason": verdict.reason_code,
            "reason_text": REASON_TEXT.get(verdict.reason_code, ""),
            "latency_ms": latency_ms,
        },
    )
    emit(question_id, "done", {})


def _finish(
    question_id: str,
    *,
    status: str,
    verdict: str | None,
    degraded: bool,
    latency_ms: int,
) -> None:
    with session_scope() as db:
        question = db.get(Question, question_id)
        if question is None:
            return
        question.status = status
        question.verdict = verdict
        question.degraded = degraded
        question.latency_ms = latency_ms
        question.finished_at = utcnow()
