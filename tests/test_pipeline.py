"""端到端链路：提问 → 三台并发 → 逐票推送 → 聚合定稿。

全部使用 mock provider，因此不依赖任何 API key，也不产生费用，
但走的是和真实调用完全相同的代码路径。
"""

from __future__ import annotations

import time

import pytest

QUESTION = "Is 3 < 2?"


def _ask(
    client,
    csrf: str,
    text: str = QUESTION,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
):
    payload: dict = {"text": text}
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return client.post(
        "/api/questions", json=payload, headers={"X-CSRF-Token": csrf}
    )


def _wait(client, question_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/questions/{question_id}").json()
        if data["status"] != "processing":
            return data
        time.sleep(0.02)
    pytest.fail(f"提问 {question_id} 超时未定稿")


def test_question_runs_to_verdict(client, make_user, login):
    make_user()
    body = login()

    response = _ask(client, body["csrf_token"])
    assert response.status_code == 201
    question_id = response.json()["id"]

    data = _wait(client, question_id)
    # mock 票型是 melchior=yes / balthasar=no / casper=yes，一票否决。
    assert data["status"] == "no"
    assert data["verdict"] == "no"
    assert data["is_yes_no"] is True
    assert len(data["runs"]) == 3

    by_agent = {run["agent"]: run for run in data["runs"]}
    assert by_agent["melchior"]["status"] == "yes"
    assert by_agent["balthasar"]["status"] == "no"
    assert by_agent["casper"]["status"] == "yes"
    # 每票都要带上模型名与延迟，否则前端无法解释分歧来源。
    assert by_agent["melchior"]["model"] == "mock-model"
    assert by_agent["melchior"]["latency_ms"] is not None


def test_stream_replays_events_and_terminates(client, make_user, login):
    make_user()
    body = login()
    question_id = _ask(client, body["csrf_token"]).json()["id"]
    _wait(client, question_id)

    with client.stream(
        "GET", f"/api/questions/{question_id}/stream"
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert "event: question.classified" in body
    assert "event: agent.vote" in body
    assert "event: verdict" in body
    assert "event: done" in body


def test_ask_requires_csrf(client, make_user, login):
    make_user()
    login()
    response = client.post("/api/questions", json={"text": QUESTION})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_question_is_private_to_owner(client, make_user, login):
    make_user("alice")
    body = login("alice")
    question_id = _ask(client, body["csrf_token"]).json()["id"]
    _wait(client, question_id)

    client.post("/api/auth/logout", headers={"X-CSRF-Token": body["csrf_token"]})

    make_user("bob", "another strong password")
    other = client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "another strong password"},
    ).json()

    # 别人的提问一律 404，不区分"不存在"与"不属于你"。
    assert client.get(f"/api/questions/{question_id}").status_code == 404
    assert client.get(f"/api/questions/{question_id}/stream").status_code == 404
    assert other["user"]["username"] == "bob"


def test_empty_question_rejected(client, make_user, login):
    make_user()
    body = login()
    response = _ask(client, body["csrf_token"], text="   ")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_question"


def test_delete_removes_record(client, make_user, login):
    make_user()
    body = login()
    question_id = _ask(client, body["csrf_token"]).json()["id"]
    _wait(client, question_id)

    response = client.delete(
        f"/api/questions/{question_id}", headers={"X-CSRF-Token": body["csrf_token"]}
    )
    assert response.status_code == 204
    assert client.get(f"/api/questions/{question_id}").status_code == 404
    assert client.get("/api/questions").json()["total"] == 0


def test_delete_requires_csrf(client, make_user, login):
    make_user()
    body = login()
    question_id = _ask(client, body["csrf_token"]).json()["id"]
    _wait(client, question_id)

    assert client.delete(f"/api/questions/{question_id}").status_code == 403


def test_cannot_delete_another_users_record(client, make_user, login):
    make_user("alice")
    alice = login("alice")
    question_id = _ask(client, alice["csrf_token"]).json()["id"]
    _wait(client, question_id)
    client.post("/api/auth/logout", headers={"X-CSRF-Token": alice["csrf_token"]})

    make_user("bob", "another strong password")
    bob = client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "another strong password"},
    ).json()

    response = client.delete(
        f"/api/questions/{question_id}", headers={"X-CSRF-Token": bob["csrf_token"]}
    )
    assert response.status_code == 404


def test_clear_history_only_touches_own_records(client, make_user, login):
    make_user("alice")
    alice = login("alice")
    first = _ask(client, alice["csrf_token"]).json()["id"]
    _wait(client, first)
    client.post("/api/auth/logout", headers={"X-CSRF-Token": alice["csrf_token"]})

    make_user("bob", "another strong password")
    bob = client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "another strong password"},
    ).json()
    second = _ask(client, bob["csrf_token"]).json()["id"]
    _wait(client, second)

    response = client.delete("/api/questions", headers={"X-CSRF-Token": bob["csrf_token"]})
    assert response.status_code == 204
    assert client.get("/api/questions").json()["total"] == 0

    # Alice 的记录不应被 Bob 的清空操作波及
    client.post("/api/auth/logout", headers={"X-CSRF-Token": bob["csrf_token"]})
    client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse battery staple"},
    )
    listing = client.get("/api/questions").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == first


def test_history_and_detail_payload(client, make_user, login):
    make_user()
    body = login()
    question_id = _ask(client, body["csrf_token"]).json()["id"]
    _wait(client, question_id)

    detail = client.get(f"/api/questions/{question_id}").json()
    # 详情页要把当时的引用来源原样还回来，而不是重新检索一次。
    assert "sources" in detail
    assert isinstance(detail["sources"], list)
    assert detail["form"] == "question"

    listing = client.get("/api/questions").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == question_id


def test_long_text_is_accepted(client, make_user, login):
    make_user()
    body = login()
    long_text = ("这是一段用于测试的长材料，包含多个句子。" * 200)[:6000]
    assert len(long_text) > 2000

    response = _ask(client, body["csrf_token"], text=long_text)
    assert response.status_code == 201
    data = _wait(client, response.json()["id"])
    assert data["status"] != "processing"
    assert data["text"].startswith("这是一段用于测试")


def test_custom_max_tokens_is_stored(client, make_user, login):
    make_user()
    body = login()
    question_id = _ask(
        client, body["csrf_token"], text="自定义输出上限", max_tokens=600
    ).json()["id"]
    data = _wait(client, question_id)
    assert data["max_tokens"] == 600


def test_max_tokens_is_clamped_to_server_cap(client, make_user, login):
    make_user()
    body = login()
    question_id = _ask(
        client, body["csrf_token"], text="超过硬上限", max_tokens=20000
    ).json()["id"]
    data = _wait(client, question_id)
    # 服务端硬上限默认 8000
    assert data["max_tokens"] == 8000


def test_max_tokens_below_floor_is_rejected(client, make_user, login):
    make_user()
    body = login()
    response = _ask(client, body["csrf_token"], max_tokens=64)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_reasoning_effort_defaults_to_low(client, make_user, login):
    make_user()
    body = login()
    question_id = _ask(client, body["csrf_token"]).json()["id"]
    data = _wait(client, question_id)
    assert data["reasoning_effort"] == "low"
    assert data["max_tokens"] == 2000


def test_reasoning_effort_is_stored_and_returned(client, make_user, login):
    make_user()
    body = login()
    for effort in ("high", "max"):
        question_id = _ask(
            client, body["csrf_token"], text="测试思考强度", reasoning_effort=effort
        ).json()["id"]
        data = _wait(client, question_id)
        assert data["reasoning_effort"] == effort


def test_invalid_reasoning_effort_is_rejected(client, make_user, login):
    make_user()
    body = login()
    response = _ask(client, body["csrf_token"], reasoning_effort="ultra")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_text_over_limit_is_rejected(client, make_user, login, monkeypatch):
    make_user()
    body = login()
    response = _ask(client, body["csrf_token"], text="x" * 8001)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "question_too_long"
