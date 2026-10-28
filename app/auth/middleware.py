"""鉴权守卫。

要点：**页面和接口走同一道门**。历史教训是只保护 HTML 路由，
攻击者直接 POST 后端接口即可绕过——对 Dash 是
``/_dash-update-component``，对本项目是所有 ``/api/**``。
"""

from __future__ import annotations

import hmac
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.auth.sessions import resolve_session
from app.config import get_settings
from app.db import session_scope
from app.models import AuthSession, User

# 无需登录即可访问的路径。
PUBLIC_EXACT = {"/login", "/healthz", "/favicon.ico"}
PUBLIC_PREFIXES = ("/api/auth/login", "/assets/", "/static/")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        settings = get_settings()
        token = request.cookies.get(settings.session_cookie_name)
        resolved = await run_in_threadpool(self._resolve, token)

        if resolved is None:
            if path.startswith("/api/"):
                return JSONResponse(
                    {
                        "error": {
                            "code": "unauthenticated",
                            "message": "未登录或会话已失效",
                        }
                    },
                    status_code=401,
                )
            target = quote(path + (f"?{request.url.query}" if request.url.query else ""))
            return RedirectResponse(f"/login?next={target}", status_code=303)

        user, session = resolved

        if request.method not in SAFE_METHODS:
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(supplied, session.csrf_token):
                return JSONResponse(
                    {"error": {"code": "csrf_failed", "message": "请求校验失败，请刷新页面重试"}},
                    status_code=403,
                )

        request.state.user = user
        request.state.session = session
        return await call_next(request)

    @staticmethod
    def _resolve(token: str | None) -> tuple[User, AuthSession] | None:
        if not token:
            return None
        with session_scope() as db:
            return resolve_session(db, token)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """兜底安全头。生产环境 nginx 也会加一份，重复无害。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response
