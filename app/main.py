"""应用装配。

生产形态：nginx 托管前端静态文件并把 /api 反代到这里；
本服务只负责接口与会话，不负责渲染页面。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import auth, questions, settings as settings_api, system
from app.auth.middleware import AuthGuardMiddleware, SecurityHeadersMiddleware
from app.config import BASE_DIR, get_settings
from app.db import init_db

STATIC_DIR = BASE_DIR / "app" / "static"

logger = logging.getLogger(__name__)


def _warn_on_unconfigured_providers() -> None:
    """启动即提示缺 key，省得用户提交问题后才看到一堆 401。"""
    from app.agents.config import get_agents_config

    try:
        config = get_agents_config()
    except Exception:  # noqa: BLE001 - 配置坏了不该让服务起不来
        logger.exception("agents.yaml 加载失败")
        return

    missing = {
        agent.provider
        for agent in config.agents
        if not config.provider(agent.provider).is_configured
    }
    if missing:
        logger.warning(
            "以下 provider 未配置 API key，调用会直接失败：%s", ", ".join(sorted(missing))
        )


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_db()
        _warn_on_unconfigured_providers()
        yield

    app = FastAPI(
        title="MAGI",
        version=__version__,
        lifespan=lifespan,
        # 生产不暴露交互式文档，减少攻击面。
        docs_url=None if settings.is_prod else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/api/openapi.json",
    )

    # 先加的在内层：SecurityHeaders 必须包住 401/403 响应，故后加。
    app.add_middleware(AuthGuardMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(questions.router)
    app.include_router(settings_api.router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _html(name: str) -> FileResponse:
        # 页面不缓存：改前端后用户只要普通刷新就能拿到新版，不用记着按 Ctrl+F5。
        response = FileResponse(STATIC_DIR / name)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def console() -> FileResponse:
        # 过渡期的控制台页面，复用上游 style.css；M4 换成 React 后由 nginx 托管。
        return _html("console.html")

    @app.get("/history", include_in_schema=False)
    def history_page() -> FileResponse:
        return _html("history.html")

    @app.get("/settings", include_in_schema=False)
    def settings_page() -> FileResponse:
        return _html("settings.html")

    @app.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return _html("login.html")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        payload = detail if isinstance(detail, dict) else {
            "code": f"http_{exc.status_code}",
            "message": str(detail),
        }
        return JSONResponse(
            {"error": payload}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "请求参数不合法"}},
            status_code=400,
        )

    return app


app = create_app()
