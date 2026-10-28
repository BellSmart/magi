"""提问、逐票推送、历史。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import orchestrator
from app.agents.answer import normalise_effort, resolve_max_tokens
from app.config import get_settings
from app.agents.config import get_agents_config
from app.credentials import user_credentials
from app.db import get_db, session_scope
from app.models import AgentRun, Question, QuestionEvent, SearchCall, User, utcnow

router = APIRouter(prefix="/api/questions", tags=["questions"])

SSE_POLL_SECONDS = 0.25
SSE_KEEPALIVE_SECONDS = 15
SSE_MAX_SECONDS = 300


class AskPayload(BaseModel):
    # 硬上限放宽到 20000，真正的限制由 settings.max_question_chars 决定，
    # 这样改配置不用同时改模型定义。
    text: str = Field(min_length=1, max_length=20000)
    # 答申长度与思考强度：不传则用该用户在设置页里的默认值。
    max_tokens: int | None = Field(default=None, ge=128, le=32000)
    reasoning_effort: Literal["low", "high", "max"] | None = None


def _owned_question(db: Session, question_id: str, user: User) -> Question:
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "提问不存在"},
        )
    if question.user_id != user.id:
        # 不区分"不存在"与"不属于你"，避免用返回码探测 id。
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "提问不存在"},
        )
    return question


def _question_payload(question: Question) -> dict[str, Any]:
    return {
        "id": question.id,
        "text": question.text,
        "status": question.status,
        "verdict": question.verdict,
        "degraded": question.degraded,
        "is_yes_no": question.is_yes_no,
        "form": question.form,
        "max_tokens": question.max_tokens,
        "reasoning_effort": question.reasoning_effort,
        "latency_ms": question.latency_ms,
        "created_at": question.created_at.isoformat(),
    }


def _run_payload(run: AgentRun) -> dict[str, Any]:
    return {
        "agent": run.agent_id,
        "display": run.display_name,
        "model": run.model,
        "provider": run.provider,
        "status": run.status,
        "vote": run.vote,
        "answer": run.answer,
        "conditions": run.conditions,
        "error": run.error,
        "latency_ms": run.latency_ms,
        "tokens": {
            "prompt": run.prompt_tokens,
            "completion": run.completion_tokens,
            "cache_hit": run.cache_hit_tokens,
            "cache_miss": run.cache_miss_tokens,
        },
        "cache_hit_rate": (
            round(run.cache_hit_tokens / (run.cache_hit_tokens + run.cache_miss_tokens) * 100, 1)
            if (run.cache_hit_tokens + run.cache_miss_tokens)
            else 0.0
        ),
        "cost_yuan": round(run.cost_yuan, 6),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def ask(
    payload: AskPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    settings = get_settings()
    user = cast(User, request.state.user)

    text = payload.text.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "empty_question", "message": "问题不能为空"},
        )
    if len(text) > settings.max_question_chars:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "question_too_long",
                "message": f"问题最长 {settings.max_question_chars} 字",
            },
        )

    # 同时只允许一问在跑：三路 agent × 搜索的开销不适合并发叠加。
    busy = db.scalar(
        select(func.count())
        .select_from(Question)
        .where(Question.user_id == user.id, Question.status == "processing")
    )
    if busy:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "busy", "message": "上一问还在处理中"},
        )

    # 服务端不持有厂商密钥：一个都没配就直接拒绝，别让它跑到 agent 里才报错。
    config = get_agents_config()
    if not any(p.is_mock for p in config.providers.values()):
        if not user_credentials(db, user.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "missing_credentials",
                    "message": "先に「設 定」で API キーを登録してください",
                },
            )

    # 账户级默认值优先，请求可临时覆盖。
    requested_tokens = (
        payload.max_tokens if payload.max_tokens is not None else user.answer_max_tokens
    )
    output_tokens = resolve_max_tokens(requested_tokens, settings.max_output_tokens)
    effort = payload.reasoning_effort or normalise_effort(user.answer_reasoning_effort)

    question = Question(
        user_id=user.id,
        text=text,
        status="processing",
        max_tokens=output_tokens,
        reasoning_effort=effort,
    )
    db.add(question)
    db.commit()

    orchestrator.spawn(question.id)
    return {"id": question.id}


@router.get("")
def history(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    user = cast(User, request.state.user)
    rows = db.scalars(
        select(Question)
        .where(Question.user_id == user.id)
        .order_by(Question.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = int(
        db.scalar(
            select(func.count()).select_from(Question).where(Question.user_id == user.id)
        )
        or 0
    )

    # 列表里带上票型分布，一次分组查询解决，避免逐条 N+1。
    tally_by_question: dict[str, dict[str, int]] = {}
    if rows:
        grouped = db.execute(
            select(AgentRun.question_id, AgentRun.status, func.count())
            .where(AgentRun.question_id.in_([row.id for row in rows]))
            .group_by(AgentRun.question_id, AgentRun.status)
        ).all()
        for question_id, agent_status, count in grouped:
            tally_by_question.setdefault(question_id, {})[agent_status] = int(count)

    items = []
    for row in rows:
        payload = _question_payload(row)
        payload["tally"] = tally_by_question.get(row.id, {})
        items.append(payload)

    return {"total": total, "items": items}


@router.get("/{question_id}")
def detail(
    question_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user = cast(User, request.state.user)
    question = _owned_question(db, question_id, user)
    runs = db.scalars(
        select(AgentRun)
        .where(AgentRun.question_id == question_id)
        .order_by(AgentRun.id)
    ).all()

    # 引用来源：详情页要把当时检索到的东西原样还回来，而不是重新检索一次。
    sources: list[dict[str, Any]] = []
    search_summary: list[dict[str, Any]] = []
    for call in db.scalars(
        select(SearchCall).where(SearchCall.question_id == question_id)
    ).all():
        search_summary.append(
            {
                "query": call.query,
                "provider": call.provider,
                "hits": call.hit_count,
                "latency_ms": call.latency_ms,
                "credits": call.credits,
            }
        )
        for source in call.sources:
            sources.append(
                {
                    "title": source.title,
                    "url": source.url,
                    "snippet": source.snippet,
                }
            )

    data = _question_payload(question)
    data["runs"] = [_run_payload(run) for run in runs]
    data["sources"] = sources
    data["search"] = search_summary
    return data


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(
    question_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = cast(User, request.state.user)
    question = _owned_question(db, question_id, user)
    # agent_runs / question_events / search_calls 由外键级联删除（SQLite 已开 foreign_keys）。
    db.delete(question)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def clear_history(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    user = cast(User, request.state.user)
    ids = db.scalars(select(Question.id).where(Question.user_id == user.id)).all()
    if ids:
        db.execute(delete(Question).where(Question.id.in_(ids)))
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _fetch_events(question_id: str, last_id: int) -> list[dict[str, Any]]:
    with session_scope() as db:
        rows = db.scalars(
            select(QuestionEvent)
            .where(
                QuestionEvent.question_id == question_id,
                QuestionEvent.id > last_id,
            )
            .order_by(QuestionEvent.id)
        ).all()
        return [
            {"id": row.id, "type": row.type, "payload": row.payload} for row in rows
        ]


def _status_of(question_id: str) -> str | None:
    with session_scope() as db:
        question = db.get(Question, question_id)
        return question.status if question else None


@router.get("/{question_id}/stream")
async def stream(
    question_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    user = cast(User, request.state.user)
    question = _owned_question(db, question_id, user)

    try:
        last_id = int(request.headers.get("last-event-id") or 0)
    except ValueError:
        last_id = 0

    async def generate():  # noqa: ANN202
        nonlocal last_id
        deadline = time.monotonic() + SSE_MAX_SECONDS
        last_signal = time.monotonic()

        while True:
            if await request.is_disconnected():
                break

            for event in _fetch_events(question_id, last_id):
                last_id = event["id"]
                payload = json.dumps(event["payload"], ensure_ascii=False)
                yield f"id: {last_id}\nevent: {event['type']}\ndata: {payload}\n\n"
                last_signal = time.monotonic()

            current = _status_of(question_id)
            if current is not None and current != "processing":
                # 定稿后再拉一次，确保 verdict/done 不会漏在两次轮询之间。
                trailing = _fetch_events(question_id, last_id)
                if not trailing:
                    break
                continue

            if time.monotonic() >= deadline:
                yield 'event: error\ndata: {"message":"推送超时"}\n\n'
                break

            if time.monotonic() - last_signal >= SSE_KEEPALIVE_SECONDS:
                yield ": keepalive\n\n"
                last_signal = time.monotonic()

            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 告诉 nginx 不要缓冲，否则事件会攒着一起到达。
            "X-Accel-Buffering": "no",
        },
    )
