"""每个用户的模型配置互相独立，且只能选白名单里的模型。"""

from __future__ import annotations

PASSWORD = "correct horse battery staple"


def _login(client, username):
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _set(client, csrf, agents):
    return client.put(
        "/api/settings/agents",
        json={"agents": agents},
        headers={"X-CSRF-Token": csrf},
    )


def test_default_settings_follow_config(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")

    data = client.get("/api/settings/agents").json()
    assert [agent["id"] for agent in data["agents"]] == [
        "melchior",
        "balthasar",
        "casper",
    ]
    assert all(agent["custom"] is False for agent in data["agents"])
    assert {option["model"] for option in data["options"]} == {
        "mock-model",
        "mock-model-2",
    }


def test_user_can_switch_model_for_one_persona(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")

    response = _set(
        client,
        csrf,
        {"melchior": {"provider": "mock", "model": "mock-model-2"}},
    )
    assert response.status_code == 200

    agents = {a["id"]: a for a in response.json()["agents"]}
    assert agents["melchior"]["model"] == "mock-model-2"
    assert agents["melchior"]["custom"] is True
    # 另外两台不受影响
    assert agents["balthasar"]["custom"] is False


def test_settings_are_isolated_between_users(client, make_user):
    make_user("alice")
    alice_csrf = _login(client, "alice")
    _set(client, alice_csrf, {"melchior": {"provider": "mock", "model": "mock-model-2"}})
    client.post("/api/auth/logout", headers={"X-CSRF-Token": alice_csrf})

    make_user("bob")
    _login(client, "bob")

    bob_agents = {a["id"]: a for a in client.get("/api/settings/agents").json()["agents"]}
    # Bob 没设置过，必须还是默认值
    assert bob_agents["melchior"]["model"] == "mock-model"
    assert bob_agents["melchior"]["custom"] is False


def test_model_outside_catalog_is_rejected(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")

    response = _set(
        client,
        csrf,
        {"melchior": {"provider": "mock", "model": "gpt-9-ultra"}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_allowed"


def test_unknown_persona_is_rejected(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")

    response = _set(client, csrf, {"ghost": {"provider": "mock", "model": "mock-model"}})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_agent"


def test_settings_require_csrf(client, make_user):
    make_user("alice")
    _login(client, "alice")
    response = client.put(
        "/api/settings/agents",
        json={"agents": {"melchior": {"provider": "mock", "model": "mock-model-2"}}},
    )
    assert response.status_code == 403


def test_reset_restores_defaults(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")
    _set(client, csrf, {"casper": {"provider": "mock", "model": "mock-model-2"}})

    response = client.delete("/api/settings/agents", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    agents = {a["id"]: a for a in response.json()["agents"]}
    assert agents["casper"]["custom"] is False
    assert agents["casper"]["model"] == "mock-model"


# --- 答申の長さ -------------------------------------------------------------


def test_answer_defaults(client, make_user):
    make_user("alice")
    _login(client, "alice")

    data = client.get("/api/settings/answer").json()
    assert data["max_tokens"] == 2000
    assert data["reasoning_effort"] == "low"
    assert [item["key"] for item in data["efforts"]] == ["low", "high", "max"]


def test_answer_can_be_customised(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")

    response = client.put(
        "/api/settings/answer",
        json={"max_tokens": 6000, "reasoning_effort": "high"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["max_tokens"] == 6000
    assert response.json()["reasoning_effort"] == "high"


def test_answer_cap_is_clamped_to_server_limit(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")

    data = client.put(
        "/api/settings/answer",
        json={"max_tokens": 99999},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert data["max_tokens"] == 8000


def test_reasoning_effort_is_validated(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")

    response = client.put(
        "/api/settings/answer",
        json={"reasoning_effort": "ultra"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_effort"


def test_answer_settings_are_isolated(client, make_user):
    make_user("alice")
    alice_csrf = _login(client, "alice")
    client.put(
        "/api/settings/answer",
        json={"max_tokens": 6000, "reasoning_effort": "high"},
        headers={"X-CSRF-Token": alice_csrf},
    )
    client.post("/api/auth/logout", headers={"X-CSRF-Token": alice_csrf})

    make_user("bob")
    _login(client, "bob")
    data = client.get("/api/settings/answer").json()
    assert data["max_tokens"] == 2000
    assert data["reasoning_effort"] == "low"


def test_question_uses_account_answer_settings(client, make_user, login):
    import time

    make_user("alice")
    csrf = _login(client, "alice")
    client.put(
        "/api/settings/answer",
        json={"max_tokens": 6000, "reasoning_effort": "high"},
        headers={"X-CSRF-Token": csrf},
    )

    created = client.post(
        "/api/questions",
        json={"text": "设定是否生效"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201
    question_id = created.json()["id"]

    deadline = time.time() + 10
    while time.time() < deadline:
        detail = client.get(f"/api/questions/{question_id}").json()
        if detail["status"] != "processing":
            break
        time.sleep(0.02)

    assert detail["max_tokens"] == 6000
    assert detail["reasoning_effort"] == "high"


# --- 判定ルール -------------------------------------------------------------


def test_voting_defaults_to_veto(client, make_user):
    make_user("alice")
    _login(client, "alice")

    data = client.get("/api/settings/voting").json()
    assert data["policy"] == "veto"
    assert [item["key"] for item in data["policies"]] == ["veto", "majority"]


def test_voting_policy_is_validated(client, make_user):
    make_user("alice")
    csrf = _login(client, "alice")

    response = client.put(
        "/api/settings/voting", json={"policy": "dictatorship"}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_policy"


def _ask_and_wait(client, csrf, text="投票规则是否生效"):
    import time

    created = client.post(
        "/api/questions", json={"text": text}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == 201, created.text
    question_id = created.json()["id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        detail = client.get(f"/api/questions/{question_id}").json()
        if detail["status"] != "processing":
            return detail
        time.sleep(0.02)
    raise AssertionError("question did not finish")


def test_majority_policy_approves_where_veto_rejects(client, make_user):
    """mock 的票型固定为 yes / no / yes：
    一票否決なら 拒 絶、多数決なら 合 意 になるはず。"""
    make_user("alice")
    csrf = _login(client, "alice")

    veto = _ask_and_wait(client, csrf)
    assert veto["status"] == "no"

    client.put(
        "/api/settings/voting", json={"policy": "majority"}, headers={"X-CSRF-Token": csrf}
    )
    majority = _ask_and_wait(client, csrf)
    assert majority["status"] == "yes"


def test_voting_policy_is_isolated(client, make_user):
    make_user("alice")
    alice_csrf = _login(client, "alice")
    client.put(
        "/api/settings/voting", json={"policy": "majority"}, headers={"X-CSRF-Token": alice_csrf}
    )
    client.post("/api/auth/logout", headers={"X-CSRF-Token": alice_csrf})

    make_user("bob")
    _login(client, "bob")
    assert client.get("/api/settings/voting").json()["policy"] == "veto"
