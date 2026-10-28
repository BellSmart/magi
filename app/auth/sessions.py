"""服务端会话。

不用 JWT 的关键理由：要能立刻吊销。封禁用户、改密、登出都必须
在下一秒失效，而 JWT 做不到（除非再建吊销表，那就等价于会话表了）。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuthSession, User, utcnow

# 每请求都写 last_seen 会把 SQLite 变成写热点，隔一分钟记一次足够。
_TOUCH_INTERVAL = timedelta(seconds=60)


def _aware(value: datetime) -> datetime:
    """SQLite 取回的 datetime 不带 tzinfo，统一补成 UTC 再比较。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(
    db: Session, user: User, *, ip: str | None, user_agent: str | None
) -> tuple[str, AuthSession]:
    """返回（明文令牌，会话记录）。明文只在此刻返回，之后只有哈希。"""
    settings = get_settings()
    now = utcnow()
    token = secrets.token_urlsafe(32)
    session = AuthSession(
        id=hash_token(token),
        user_id=user.id,
        csrf_token=new_csrf_token(),
        created_at=now,
        last_seen_at=now,
        absolute_expires_at=now + timedelta(seconds=settings.session_absolute_seconds),
        ip=ip,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(session)
    db.flush()
    return token, session


def resolve_session(db: Session, token: str | None) -> tuple[User, AuthSession] | None:
    """校验令牌，顺带做滑动续期。任何一项不满足都会吊销并返回 None。"""
    if not token:
        return None

    settings = get_settings()
    session = db.get(AuthSession, hash_token(token))
    if session is None or session.revoked_at is not None:
        return None

    now = utcnow()
    if now >= _aware(session.absolute_expires_at):
        _revoke(db, session)
        return None

    if now - _aware(session.last_seen_at) > timedelta(
        seconds=settings.session_idle_seconds
    ):
        _revoke(db, session)
        return None

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        _revoke(db, session)
        return None

    if now - _aware(session.last_seen_at) > _TOUCH_INTERVAL:
        session.last_seen_at = now

    return user, session


def _revoke(db: Session, session: AuthSession) -> None:
    session.revoked_at = utcnow()


def revoke(db: Session, session: AuthSession) -> None:
    _revoke(db, session)


def revoke_all_for_user(db: Session, user_id: int) -> int:
    """改密 / 封禁时调用，踢掉该用户全部在线会话。"""
    result = db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return result.rowcount or 0


def active_sessions(db: Session, user_id: int) -> int:
    return len(
        db.scalars(
            select(AuthSession.id).where(
                AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
            )
        ).all()
    )
