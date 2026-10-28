"""测试夹具：每个用例一套独立的临时数据库与配置。"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

# 全部走 mock:// provider：不发网络请求，但走完整的 runner/聚合/SSE 链路。
# 三台故意给出不同票型（yes / no / yes），用来验证一票否决。
MOCK_AGENTS_YAML = """
providers:
  mock:
    base_url: mock://local
    api_key: ""
    timeout_s: 5

classifier:
  provider: mock
  model: mock-classifier
  max_tokens: 8

voting:
  quorum: 2
  veto_on_no: true

search:
  provider: tavily
  api_key: ""

models:
  - provider: mock
    model: mock-model
    label: Mock A
  - provider: mock
    model: mock-model-2
    label: Mock B

agents:
  - id: melchior
    order: 1
    display: MELCHIOR • 1
    provider: mock
    model: mock-model
    persona: You are a scientist.
  - id: balthasar
    order: 2
    display: BALTHASAR • 2
    provider: mock
    model: mock-model
    persona: You are a mother.
  - id: casper
    order: 3
    display: CASPER • 3
    provider: mock
    model: mock-model
    persona: You are a woman.
"""


@pytest.fixture()
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MAGI_ENV", "dev")
    monkeypatch.setenv(
        "MAGI_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    monkeypatch.setenv("MAGI_SECURE_COOKIES", "false")
    monkeypatch.setenv("MAGI_SECRET_KEY", "test-secret-key")

    config_path = tmp_path / "agents.yaml"
    config_path.write_text(MOCK_AGENTS_YAML, encoding="utf-8")
    monkeypatch.setenv("MAGI_AGENTS_CONFIG_PATH", config_path.as_posix())

    from app import db
    from app.agents.config import reset_agents_config_cache
    from app.config import reset_settings_cache

    reset_settings_cache()
    reset_agents_config_cache()
    db.reset_engine()

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client

    db.reset_engine()
    reset_agents_config_cache()


@pytest.fixture()
def make_user() -> Callable[..., int]:
    def _make(
        username: str = "alice",
        password: str = "correct horse battery staple",
        *,
        is_active: bool = True,
        is_admin: bool = False,
        daily_quota: int | None = None,
    ) -> int:
        from app.auth.passwords import hash_password
        from app.db import session_scope
        from app.models import User

        with session_scope() as db:
            user = User(
                username=username,
                password_hash=hash_password(password),
                is_active=is_active,
                is_admin=is_admin,
                daily_quota=daily_quota,
            )
            db.add(user)
            db.flush()
            return user.id

    return _make


@pytest.fixture()
def login(client: TestClient) -> Callable[..., dict]:
    def _login(
        username: str = "alice", password: str = "correct horse battery staple"
    ) -> dict:
        response = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        return response.json()

    return _login
