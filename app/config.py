"""运行期配置。

所有环境相关的开关都走 ``MAGI_`` 前缀的环境变量（或 .env），
密钥类配置只允许来自环境，不写进仓库。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# 开发环境下允许的构建产物路径；生产由 nginx 直接托管。
WEB_DIST = BASE_DIR / "web" / "dist"


def _load_dotenv() -> None:
    """把 .env 灌进 os.environ。

    pydantic-settings 只对自身字段读 .env，而 ``config/agents.yaml`` 里的
    ``${ENV:NAME}`` 替换看的是 os.environ——不打通的话，密钥会静默解析成空串。
    已存在的真实环境变量优先，不覆盖。
    """
    path = BASE_DIR / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip().strip("'\""))


_load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAGI_",
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "prod"] = "dev"
    database_url: str = f"sqlite:///{(DATA_DIR / 'magi.db').as_posix()}"

    # 用于签名分享链接等不可逆派生，生产必须显式提供。
    secret_key: str = "dev-only-insecure-secret"

    # --- 会话 ---
    session_idle_seconds: int = 7 * 24 * 3600
    session_absolute_seconds: int = 30 * 24 * 3600
    # 生产必须为 True（"__Host-" 前缀要求 Secure）。开发用 http，关掉。
    secure_cookies: bool = True
    # 为空时按 secure_cookies 推导。
    cookie_name: str = ""

    # --- 登录防爆破 ---
    login_max_failures: int = 5
    login_window_seconds: int = 15 * 60
    login_lockout_seconds: int = 15 * 60

    # --- 成本护栏 ---
    # 只保留全局日花费熔断；按用户的次数上限已取消。
    global_daily_budget_yuan: float = 30.0
    # 长文本输入：允许整段材料/会议记录直接贴进来。
    max_question_chars: int = 8000
    # 单票输出上限的硬天花板：用户可自定义，但不能越过这里。
    max_output_tokens: int = 8000
    min_password_length: int = 12

    # --- 对外地址（用于生成分享链接、CSRF 校验） ---
    public_origin: str = "http://127.0.0.1:8000"

    # --- agent 配置 ---
    agents_config_path: str = str(BASE_DIR / "config" / "agents.yaml")
    # 单次提问的整体超时；三路并发里最慢的一路决定上限。
    question_timeout_seconds: int = 120

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def session_cookie_name(self) -> str:
        if self.cookie_name:
            return self.cookie_name
        # __Host- 前缀强制 Secure + Path=/ + 无 Domain，是防子域覆盖的最简手段。
        return "__Host-magi_sid" if self.secure_cookies else "magi_sid"

    @model_validator(mode="after")
    def _check_prod(self) -> "Settings":
        if self.is_prod:
            if not self.secure_cookies:
                raise ValueError("生产环境必须启用 secure_cookies")
            if self.secret_key.startswith("dev-only"):
                raise ValueError("生产环境必须设置 MAGI_SECRET_KEY")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：改完环境变量后强制重新读取。"""
    get_settings.cache_clear()
