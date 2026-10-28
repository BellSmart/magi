"""鉴权测试。

覆盖的是"未登录打不进任何接口"这条承诺，以及几条容易被忽略的边界：
不存在的接口也要先被拦、CSRF 必须校验、封禁要能立刻踢下线。
"""

from __future__ import annotations

import pytest

PASSWORD = "correct horse battery staple"


def test_healthz_is_public(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_page_is_public(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "MAGI" in response.text


def test_api_requires_login(client):
    response = client.get("/api/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_unknown_api_is_guarded_before_routing(client):
    """不存在的接口也必须先撞上鉴权，否则等于暴露路由表。"""
    response = client.get("/api/definitely-not-a-route")
    assert response.status_code == 401


def test_page_redirects_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_security_headers_present_on_rejection(client):
    response = client.get("/api/me")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_login_sets_httponly_cookie(client, make_user):
    make_user()
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert response.status_code == 200

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    # 开发环境走 http，不能用 __Host- 前缀（它强制 Secure）。
    assert "magi_sid=" in cookie


def test_login_returns_csrf_token(client, make_user, login):
    make_user()
    body = login()
    assert body["user"]["username"] == "alice"
    assert len(body["csrf_token"]) > 20


def test_me_does_not_leak_secrets(client, make_user, login):
    make_user()
    login()
    response = client.get("/api/me")
    assert response.status_code == 200
    assert "password_hash" not in response.text


def test_login_rejects_wrong_password(client, make_user):
    make_user()
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_unknown_user_and_wrong_password_look_identical(client):
    """两者错误码必须一致，否则可以拿来枚举账号。"""
    response = client.post(
        "/api/auth/login", json={"username": "ghost", "password": "whatever123"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_lockout_after_repeated_failures(client, make_user):
    make_user()
    for _ in range(5):
        client.post(
            "/api/auth/login", json={"username": "alice", "password": "bad password"}
        )

    # 即使口令正确，锁定期内也必须拒绝。
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert response.status_code == 429
    assert response.headers["retry-after"]


def test_mutation_requires_csrf(client, make_user, login):
    make_user()
    login()

    response = client.post("/api/auth/logout")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    # 被拒的请求不能顺带把会话干掉
    assert client.get("/api/me").status_code == 200


def test_logout_revokes_session(client, make_user, login):
    make_user()
    body = login()

    response = client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": body["csrf_token"]}
    )
    assert response.status_code == 200
    assert client.get("/api/me").status_code == 401


def test_disabled_user_cannot_log_in(client, make_user):
    make_user(is_active=False)
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert response.status_code == 401


def test_disabling_user_kills_live_session(client, make_user, login):
    user_id = make_user()
    login()
    assert client.get("/api/me").status_code == 200

    from app.db import session_scope
    from app.models import User

    with session_scope() as db:
        db.get(User, user_id).is_active = False

    # 下一次请求即失效，不需要等 cookie 过期。
    assert client.get("/api/me").status_code == 401


def test_usernames_are_case_sensitive(client, make_user):
    make_user("Alice", "correct-horse-battery")

    wrong = client.post(
        "/api/auth/login", json={"username": "alice", "password": "correct-horse-battery"}
    )
    assert wrong.status_code == 401

    right = client.post(
        "/api/auth/login", json={"username": "Alice", "password": "correct-horse-battery"}
    )
    assert right.status_code == 200
    assert right.json()["user"]["username"] == "Alice"


@pytest.mark.parametrize("payload", [{}, {"username": "alice"}])
def test_login_validation_errors_are_generic(client, payload):
    response = client.post("/api/auth/login", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
