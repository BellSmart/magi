"""每个用户对每个人格的模型选择，以及各自的 API key。

服务端不持有任何厂商密钥：key 由用户在设置页填、按用户加密存库。
可选模型只列出"这个用户自己有 key"的 provider，避免选到必然报错的组合。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agents.config import get_agents_config, model_catalog
from app.agents.aggregator import POLICY_LABELS, is_valid_policy
from app.agents.answer import (
    DEFAULT_MAX_TOKENS,
    EFFORT_LABELS,
    MIN_OUTPUT_TOKENS,
    is_valid_effort,
    normalise_effort,
    resolve_max_tokens,
)
from app.config import get_settings
from app.credentials import (
    credential_rows,
    delete_credential,
    set_credential,
    user_credentials,
)
from app.db import get_db
from app.models import User, UserAgentSetting

router = APIRouter(prefix="/api/settings", tags=["settings"])


class AgentChoice(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=64)


class AgentSettingsPayload(BaseModel):
    agents: dict[str, AgentChoice]


class AnswerSettingsPayload(BaseModel):
    max_tokens: int | None = None
    reasoning_effort: str | None = None


class VotingPayload(BaseModel):
    policy: str


def _catalog_with_availability(config, creds: dict[str, str]):  # noqa: ANN001, ANN202
    """可选模型 = 用户自己配了 key 的 provider。"""
    return [
        {
            "provider": option.provider,
            "model": option.model,
            "label": option.label or option.model,
            "available": config.provider(option.provider).is_mock
            or option.provider in creds,
        }
        for option in model_catalog(config)
        if option.provider in config.providers
    ]


def _overrides(db: Session, user_id: int) -> dict[str, UserAgentSetting]:
    rows = db.scalars(
        select(UserAgentSetting).where(UserAgentSetting.user_id == user_id)
    ).all()
    return {row.agent_id: row for row in rows}


@router.get("/agents")
def get_agent_settings(request: Request, db: Session = Depends(get_db)) -> dict:
    user = cast(User, request.state.user)
    config = get_agents_config()
    options = _catalog_with_availability(config, user_credentials(db, user.id))
    overrides = _overrides(db, user.id)

    agents = []
    for agent in config.agents_in_order:
        row = overrides.get(agent.id)
        agents.append(
            {
                "id": agent.id,
                "display": agent.display,
                "provider": row.provider if row else agent.provider,
                "model": row.model if row else agent.model,
                "default_provider": agent.provider,
                "default_model": agent.model,
                "custom": row is not None,
            }
        )

    return {"agents": agents, "options": options}


@router.put("/agents")
def put_agent_settings(
    payload: AgentSettingsPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    user = cast(User, request.state.user)
    config = get_agents_config()
    options = _catalog_with_availability(config, user_credentials(db, user.id))
    allowed = {
        (option["provider"], option["model"]) for option in options if option["available"]
    }
    known_agents = {agent.id for agent in config.agents}

    for agent_id, choice in payload.agents.items():
        if agent_id not in known_agents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "unknown_agent", "message": f"未知人格：{agent_id}"},
            )
        key = (choice.provider, choice.model)
        if key not in allowed:
            # 不回显完整白名单，只说明这个组合不可用，避免探测服务端配置。
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "model_not_allowed",
                    "message": f"{agent_id} 所选模型不可用",
                },
            )

    existing = _overrides(db, user.id)
    for agent_id, choice in payload.agents.items():
        row = existing.get(agent_id)
        if row is None:
            db.add(
                UserAgentSetting(
                    user_id=user.id,
                    agent_id=agent_id,
                    provider=choice.provider,
                    model=choice.model,
                )
            )
        else:
            row.provider = choice.provider
            row.model = choice.model

    db.commit()
    return get_agent_settings(request, db)


@router.delete("/agents")
def reset_agent_settings(request: Request, db: Session = Depends(get_db)) -> dict:
    user = cast(User, request.state.user)
    db.execute(delete(UserAgentSetting).where(UserAgentSetting.user_id == user.id))
    db.commit()
    return get_agent_settings(request, db)


def _answer_payload(user: User) -> dict:
    settings = get_settings()
    return {
        "max_tokens": resolve_max_tokens(user.answer_max_tokens, settings.max_output_tokens),
        "reasoning_effort": normalise_effort(user.answer_reasoning_effort),
        "default_max_tokens": DEFAULT_MAX_TOKENS,
        "efforts": [
            {"key": key, "label": label} for key, label in EFFORT_LABELS.items()
        ],
        "limits": {"min": MIN_OUTPUT_TOKENS, "max": settings.max_output_tokens},
    }


@router.get("/answer")
def get_answer_settings(request: Request) -> dict:
    return _answer_payload(cast(User, request.state.user))


@router.put("/answer")
def put_answer_settings(
    payload: AnswerSettingsPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    if payload.reasoning_effort is not None and not is_valid_effort(
        payload.reasoning_effort
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_effort", "message": "未知的思考强度"},
        )

    user = cast(User, request.state.user)
    # request.state.user 绑在中间件的会话上，不能直接改；这里重新取一次。
    row = db.get(User, user.id)
    row.answer_max_tokens = resolve_max_tokens(
        payload.max_tokens
        if payload.max_tokens is not None
        else row.answer_max_tokens,
        get_settings().max_output_tokens,
    )
    row.answer_reasoning_effort = normalise_effort(
        payload.reasoning_effort or row.answer_reasoning_effort
    )
    db.commit()
    return _answer_payload(row)


def _voting_payload(user: User) -> dict:
    policy = user.vote_policy or get_agents_config().voting.default_policy
    return {
        "policy": policy,
        "policies": [
            {"key": key, "label": label} for key, label in POLICY_LABELS.items()
        ],
    }


@router.get("/voting")
def get_voting_settings(request: Request) -> dict:
    return _voting_payload(cast(User, request.state.user))


@router.put("/voting")
def put_voting_settings(
    payload: VotingPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    if not is_valid_policy(payload.policy):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_policy", "message": "未知的判定规则"},
        )

    user = cast(User, request.state.user)
    row = db.get(User, user.id)
    row.vote_policy = payload.policy
    db.commit()
    return _voting_payload(row)

# --- 用户自己的 API key ------------------------------------------------------

class CredentialPayload(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    api_key: str = Field(min_length=8, max_length=256)


def _provider_payload(db: Session, user: User) -> dict:
    config = get_agents_config()
    creds = user_credentials(db, user.id)
    rows = {row.provider: row for row in credential_rows(db, user.id)}
    providers = []
    for name, provider in config.providers.items():
        row = rows.get(name)
        providers.append(
            {
                "name": name,
                "label": provider.label or name,
                "configured": name in creds,
                "hint": row.hint if row else "",
                "is_default": name == config.classifier.provider,
            }
        )
    return {"providers": providers}


@router.get("/providers")
def get_providers(request: Request, db: Session = Depends(get_db)) -> dict:
    return _provider_payload(db, cast(User, request.state.user))


@router.put("/providers")
def put_provider(
    payload: CredentialPayload, request: Request, db: Session = Depends(get_db)
) -> dict:
    user = cast(User, request.state.user)
    config = get_agents_config()
    if payload.provider not in config.providers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "unknown_provider", "message": "未知的服务商"},
        )
    api_key = payload.api_key.strip()
    if len(api_key) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_key", "message": "API キーが短すぎます"},
        )
    set_credential(db, user.id, payload.provider, api_key)
    return _provider_payload(db, user)


@router.delete("/providers/{provider}")
def delete_provider(
    provider: str, request: Request, db: Session = Depends(get_db)
) -> dict:
    user = cast(User, request.state.user)
    delete_credential(db, user.id, provider)
    return _provider_payload(db, user)
