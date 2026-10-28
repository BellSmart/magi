"""登录、登出、当前用户。"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agents.answer import normalise_effort, resolve_max_tokens
from app.agents.config import get_agents_config
from app.auth.middleware import client_ip
from app.auth.passwords import DUMMY_HASH, hash_password, needs_rehash, verify_password
from app.auth.sessions import create_session, revoke
from app.config import get_settings
from app.db import get_db
from app.models import AuthSession, LoginAttempt, User, utcnow

router = APIRouter(prefix="/api", tags=["auth"])


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _attempt_key(username: str, ip: str | None) -> str:
    # 用户名大小写敏感，限流桶也跟着区分，避免两个账号互相影响。
    return f"{username.strip()}|{ip or 'unknown'}"


def _recent_failures(db: Session, key: str) -> int:
    settings = get_settings()
    since = utcnow() - timedelta(seconds=settings.login_window_seconds)
    return int(
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.key == key,
                LoginAttempt.success.is_(False),
                LoginAttempt.created_at >= since,
            )
        )
        or 0
    )


def _prune_attempts(db: Session) -> None:
    """顺手清掉过期记录，避免表无限增长。"""
    settings = get_settings()
    cutoff = utcnow() - timedelta(seconds=max(settings.login_window_seconds * 4, 86400))
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))


def _user_payload(user: User) -> dict:
    return {"id": user.id, "username": user.username, "is_admin": user.is_admin}


@router.post("/auth/login")
def login(
    payload: LoginPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    settings = get_settings()
    ip = client_ip(request)
    key = _attempt_key(payload.username, ip)

    if _recent_failures(db, key) >= settings.login_max_failures:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "too_many_attempts",
                "message": "尝试次数过多，请稍后再试",
            },
            headers={"Retry-After": str(settings.login_lockout_seconds)},
        )

    user = db.scalar(
        select(User).where(User.username == payload.username.strip())
    )

    # 用户不存在时也走一次哈希校验，避免用响应时间探测账号是否存在。
    if user is None:
        verify_password(DUMMY_HASH, payload.password)
        ok = False
    else:
        ok = user.is_active and verify_password(user.password_hash, payload.password)

    db.add(
        LoginAttempt(key=key, username=payload.username.strip()[:64], ip=ip, success=ok)
    )
    _prune_attempts(db)

    if not ok:
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "用户名或口令不正确"},
        )

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    token, session = create_session(
        db, user, ip=ip, user_agent=request.headers.get("user-agent")
    )
    db.commit()

    response = JSONResponse(
        {
            "user": _user_payload(user),
            "csrf_token": session.csrf_token,
        }
    )
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_absolute_seconds,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/auth/logout")
def logout(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    settings = get_settings()
    session: AuthSession | None = getattr(request.state, "session", None)
    if session is not None:
        revoke(db, session)
        db.commit()

    response = JSONResponse({"ok": True})
    # 删除时的属性必须与原 cookie 一致，否则浏览器不会覆盖。
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )
    return response


@router.get("/me")
def me(request: Request) -> dict:
    user = cast(User, request.state.user)
    session: AuthSession = request.state.session
    settings = get_settings()
    return {
        "user": _user_payload(user),
        "csrf_token": session.csrf_token,
        "limits": {
            "max_question_chars": settings.max_question_chars,
            "max_output_tokens": settings.max_output_tokens,
        },
        "answer": {
            "max_tokens": resolve_max_tokens(
                user.answer_max_tokens, settings.max_output_tokens
            ),
            "reasoning_effort": normalise_effort(user.answer_reasoning_effort),
        },
        "vote_policy": user.vote_policy
        or get_agents_config().voting.default_policy,
    }
