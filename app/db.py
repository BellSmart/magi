"""数据库引擎与会话。

SQLite 单机足够：开 WAL、限制 busy_timeout、外键约束。
换 Postgres 只需改 MAGI_DATABASE_URL，其余代码不用动。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        # 只建 URL 指向的目录。别无条件造 data/：生产 /opt/magi 只读，会崩。
        path = url.split("sqlite:///", 1)[-1]
        if path and path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        # SQLite 要允许跨线程（FastAPI 线程池用），其他数据库忽略。
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().database_url)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)
    return _factory


def init_db() -> None:
    """建表。v1 用 create_all；表结构稳定后换 Alembic 迁移。"""
    from app import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=get_engine())
    _migrate_sqlite()


def _migrate_sqlite() -> None:
    """create_all 不会给已有表加列，这里补几个幂等的 ALTER。

    只覆盖 SQLite（当前唯一的部署形态）；换 Postgres 时改用 Alembic。
    """
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return

    additions = {
        "questions": {
            "form": "VARCHAR(16)",
            "depth": "VARCHAR(16) NOT NULL DEFAULT 'standard'",
            "max_tokens": "INTEGER",
            "reasoning_effort": "VARCHAR(8)",
        },
        "agent_runs": {
            "cache_hit_tokens": "INTEGER NOT NULL DEFAULT 0",
            "cache_miss_tokens": "INTEGER NOT NULL DEFAULT 0",
        },
        "users": {
            "answer_depth": "VARCHAR(16) NOT NULL DEFAULT 'standard'",
            "answer_max_tokens": "INTEGER",
            "answer_reasoning_effort": "VARCHAR(8) NOT NULL DEFAULT 'low'",
            "vote_policy": "VARCHAR(16)",
        },
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            existing = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue
            for name, ddl in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def reset_engine() -> None:
    """测试用：丢弃当前引擎，下次访问按新配置重建。"""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本 / 中间件用的事务作用域。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
