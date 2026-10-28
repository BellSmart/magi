"""数据模型。时间统一存 UTC，展示层再转本地时区。

时间统一用 UTC 存储；展示层再转本地时区。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    # 用 timezone.utc：datetime.UTC 要 3.11，而 22.04 上是 3.10。
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 已废弃（每日上限取消），列留着只为兼容旧库。
    daily_quota: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 答申の既定値：1 票あたりの出力上限と思考強度。
    answer_max_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    answer_reasoning_effort: Mapped[str] = mapped_column(
        String(8), default="low"
    )
    # veto / majority；NULL なら設定側の既定値。
    vote_policy: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # 用户选的检索后端（对应 config/agents.yaml 的 search.options[].key）
    search_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    """服务端会话。只存令牌的 SHA-256，泄库也拿不到可用 cookie。"""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class LoginAttempt(Base):
    """登录失败记录，用于按 账号+IP 做退避锁定。"""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(160), index=True)
    username: Mapped[str] = mapped_column(String(64))
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class UserAgentSetting(Base):
    """每个用户对每个人格单独选择的 provider/model。

    只存"选择"。用户在 config/agents.yaml 的白名单里挑，避免任意 base_url。
    """

    __tablename__ = "user_agent_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "agent_id", name="uq_user_agent_setting"),
    )


class UserCredential(Base):
    """用户自己的厂商 API key，加密后存库。

    服务端不保存任何厂商密钥：key 由用户填、按用户存，调用时解出来
    拼进一份临时配置。换 MAGI_SECRET_KEY 会让旧密文失效，等同于没配。
    """

    __tablename__ = "user_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[str] = mapped_column(Text)
    # 界面上只回显这个，让人知道"填过了"
    hint: Mapped[str] = mapped_column(String(24), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_credential_user_provider"),
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    is_yes_no: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # question / proposition / none
    form: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # 旧档位，已废弃，留着兼容旧数据。
    depth: Mapped[str] = mapped_column(String(16), default="standard")
    # 1 票あたりの出力上限と思考強度。
    max_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reasoning_effort: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # processing / info / yes / no / conditional / error
    status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 为空表示未分享。
    share_token: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True
    )

    runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="question", cascade="all, delete-orphan", order_by="AgentRun.id"
    )

    __table_args__ = (Index("ix_questions_user_created", "user_id", "created_at"),)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    # yes / no / conditional / abstain / error / info
    status: Mapped[str] = mapped_column(String(16), default="pending")
    vote: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    conditions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_miss_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_yuan: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    question: Mapped[Question] = relationship(back_populates="runs")

    __table_args__ = (UniqueConstraint("question_id", "agent_id", name="uq_run_agent"),)


class SearchCall(Base):
    __tablename__ = "search_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    agent_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    # shared（共享检索）或 agent_id（该 agent 的追加检索）
    scope: Mapped[str] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(32))
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    credits: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sources: Mapped[list["Source"]] = relationship(
        back_populates="search_call", cascade="all, delete-orphan", order_by="Source.rank"
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_call_id: Mapped[int] = mapped_column(
        ForeignKey("search_calls.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)

    search_call: Mapped[SearchCall] = relationship(back_populates="sources")


class QuestionEvent(Base):
    """SSE 的事件日志。

    刻意落库而不是只放内存：uvicorn 多 worker 时，SSE 连接与执行任务的
    worker 可能不是同一个，内存广播会丢事件；轮询这张表则天然成立，
    顺带还能支持断线重连（Last-Event-ID）。
    """

    __tablename__ = "question_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
