"""FastAPI 依赖：从 request.state 取中间件已解析好的身份。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.models import AuthSession, User


def current_user(request: Request) -> User:
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录"
        )
    return user


def current_session(request: Request) -> AuthSession:
    session: AuthSession | None = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已失效"
        )
    return session


def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
CurrentSession = Annotated[AuthSession, Depends(current_session)]
AdminUser = Annotated[User, Depends(require_admin)]
