"""无需登录的系统端点。"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__

router = APIRouter(tags=["system"])


@router.get("/healthz")
def healthz() -> dict:
    # 只暴露"活着"和版本，不泄露依赖状态与配置。
    return {"status": "ok", "version": __version__}
