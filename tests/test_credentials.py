"""用户自带密钥：加密存取与按用户隔离。"""

from __future__ import annotations

from sqlalchemy import select

from app.credentials import (
    apply_credentials,
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    user_credentials,
)
from app.db import session_scope
from app.models import User, UserCredential


def _make_user(username: str) -> int:
    from uuid import uuid4

    from app.auth.passwords import hash_password

    with session_scope() as db:
        user = User(username=f"{username}-{uuid4().hex[:6]}", password_hash=hash_password("x" * 16))
        db.add(user)
        db.flush()
        return user.id


def test_roundtrip():
    blob = encrypt_secret("sk-abcdef0123456789")
    assert blob != "sk-abcdef0123456789"
    assert decrypt_secret(blob) == "sk-abcdef0123456789"


def test_same_plaintext_gives_different_ciphertext():
    assert encrypt_secret("sk-same") != encrypt_secret("sk-same")


def test_mask_keeps_only_edges():
    masked = mask_secret("sk-1234567890abcd")
    assert masked.startswith("sk-")
    assert masked.endswith("abcd")
    assert "1234567890" not in masked


def test_credentials_are_per_user():
    from app.credentials import set_credential

    alice = _make_user("alice-cred")
    bob = _make_user("bob-cred")

    with session_scope() as db:
        set_credential(db, alice, "deepseek", "sk-alice-key-0001")

    with session_scope() as db:
        assert user_credentials(db, alice) == {"deepseek": "sk-alice-key-0001"}
        assert user_credentials(db, bob) == {}
        rows = db.scalars(
            select(UserCredential).where(UserCredential.user_id == alice)
        ).all()
        # 库里不能出现明文
        assert "sk-alice-key-0001" not in rows[0].ciphertext


def _real_cfg():
    from app.agents.config import AgentsConfig

    return AgentsConfig.model_validate(
        {
            "providers": {
                "vendor": {"base_url": "https://example.invalid/v1", "api_key": ""}
            },
            "classifier": {"provider": "vendor", "model": "m"},
            "agents": [
                {
                    "id": "a",
                    "order": 1,
                    "display": "A",
                    "provider": "vendor",
                    "model": "m",
                    "persona": "p",
                }
            ],
        }
    )


def test_apply_credentials_swaps_keys_in():
    cfg = _real_cfg()
    applied = apply_credentials(cfg, {"vendor": "sk-user-own"})
    assert applied.provider("vendor").api_key == "sk-user-own"
    # 原配置不被污染
    assert cfg.provider("vendor").api_key == ""


def test_apply_credentials_skips_mock():
    from app.agents.config import AgentsConfig

    cfg = AgentsConfig.model_validate(
        {
            "providers": {"mock": {"base_url": "mock://local", "api_key": "keep-me"}},
            "classifier": {"provider": "mock", "model": "m"},
            "agents": [
                {
                    "id": "a",
                    "order": 1,
                    "display": "A",
                    "provider": "mock",
                    "model": "m",
                    "persona": "p",
                }
            ],
        }
    )
    assert apply_credentials(cfg, {}).provider("mock").api_key == "keep-me"
