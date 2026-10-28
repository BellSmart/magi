"""用户自带 API key 的存取。

服务端不留任何厂商密钥：每个用户填自己的 key，按用户加密存库，
调用时再解出来拼进一份"生效配置"，下游代码不需要知道 key 是从哪来的。

加密用 AES-GCM，密钥由 MAGI_SECRET_KEY 经 HKDF 派生。
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.config import AgentsConfig, SearchOption
from app.config import get_settings
from app.models import UserCredential

_INFO = b"magi-user-credentials"


def _box() -> AESGCM:
    secret = get_settings().secret_key
    if not secret:
        raise RuntimeError("secret_key 未设置，无法加密用户密钥")
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO
    ).derive(secret.encode("utf-8"))
    return AESGCM(key)


def encrypt_secret(plaintext: str) -> str:
    nonce = os.urandom(12)
    blob = _box().encrypt(nonce, plaintext.encode("utf-8"), _INFO)
    return base64.b64encode(nonce + blob).decode("ascii")


def decrypt_secret(encoded: str) -> str:
    raw = base64.b64decode(encoded)
    return _box().decrypt(raw[:12], raw[12:], _INFO).decode("utf-8")


def mask_secret(plaintext: str) -> str:
    """只回显尾部几位，用于界面确认"填过了"。"""
    if len(plaintext) <= 8:
        return "••••"
    return f"{plaintext[:3]}…{plaintext[-4:]}"


def set_credential(db: Session, user_id: int, provider: str, api_key: str) -> None:
    row = db.scalar(
        select(UserCredential).where(
            UserCredential.user_id == user_id, UserCredential.provider == provider
        )
    )
    ciphertext = encrypt_secret(api_key)
    hint = mask_secret(api_key)
    if row is None:
        db.add(
            UserCredential(
                user_id=user_id, provider=provider, ciphertext=ciphertext, hint=hint
            )
        )
    else:
        row.ciphertext = ciphertext
        row.hint = hint
    db.commit()


def delete_credential(db: Session, user_id: int, provider: str) -> None:
    row = db.scalar(
        select(UserCredential).where(
            UserCredential.user_id == user_id, UserCredential.provider == provider
        )
    )
    if row is not None:
        db.delete(row)
        db.commit()


def credential_rows(db: Session, user_id: int) -> list[UserCredential]:
    return list(
        db.scalars(
            select(UserCredential).where(UserCredential.user_id == user_id)
        ).all()
    )


def user_credentials(db: Session, user_id: int) -> dict[str, str]:
    """provider -> 明文 key。解不开的行直接跳过（比如换了 MAGI_SECRET_KEY）。"""
    out: dict[str, str] = {}
    for row in credential_rows(db, user_id):
        try:
            out[row.provider] = decrypt_secret(row.ciphertext)
        except Exception:  # noqa: BLE001 - 解密失败等价于没配
            continue
    return out


def apply_credentials(cfg: AgentsConfig, creds: dict[str, str]) -> AgentsConfig:
    """把用户的 key 拼进一份配置副本。

    下游（runner / classifier / 搜索）都只认 cfg.provider(x).api_key，
    所以换 key 不需要改它们。
    """
    providers = {
        name: (
            provider
            if provider.is_mock
            else provider.model_copy(update={"api_key": creds.get(name, "")})
        )
        for name, provider in cfg.providers.items()
    }
    search = cfg.search.model_copy(
        update={"api_key": creds.get(cfg.search.provider, "")}
    )
    return cfg.model_copy(update={"providers": providers, "search": search})


def search_choices(cfg: AgentsConfig) -> list:
    """设置页里可以列出的检索后端（没有配置 options 时给一条兜底）。"""
    if cfg.search.options:
        return list(cfg.search.options)
    return [SearchOption(key=cfg.search.provider, label=cfg.search.provider)]


def apply_search_choice(
    cfg: AgentsConfig, choice: str | None, creds: dict[str, str]
) -> AgentsConfig:
    """把用户选的检索后端拼进配置。

    kind=none 或拿不到 key 时把 api_key 清空，``search.enabled`` 为假，
    编排层就会跳过检索——这与「选了但没配 key」的预期一致。
    """
    options = search_choices(cfg)
    if not cfg.search.options:
        return cfg

    chosen = cfg.search.option(choice) or cfg.search.option(cfg.search.provider)
    if chosen is None:
        chosen = options[0]

    if chosen.kind == "none":
        update = {"provider": "none", "api_key": "", "endpoint": ""}
    else:
        update = {
            "provider": chosen.kind,
            "endpoint": chosen.endpoint or cfg.search.endpoint,
            "api_key": creds.get(chosen.credential_name, ""),
        }
    return cfg.model_copy(update={"search": cfg.search.model_copy(update=update)})
