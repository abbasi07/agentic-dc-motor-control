"""SQLAlchemy 2.0 (sync) engine + session plumbing for E2 persistence.

One synchronous engine is shared by the FastAPI process and the RQ worker process;
because design runs are CPU-heavy blocking work offloaded to the worker (E2.3), a sync
stack keeps the code simple and avoids async/greenlet foot-guns. Postgres is the
production target (psycopg3); SQLite is used by the OpenAI-free test-suite.

``Base`` is the declarative registry every ORM model in ``saas.models`` hangs off of;
Alembic imports it (via ``saas.models``) for autogeneration.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all Control Design Copilot ORM models."""


def make_engine(url: str, *, echo: bool = False) -> Engine:
    """Create an Engine, applying the SQLite-only args the test-suite needs."""
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"echo": echo, "future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # Allow cross-thread use (TestClient / RQ SimpleWorker share the connection)
        # and keep an in-memory DB alive for the whole process via a static pool.
        connect_args["check_same_thread"] = False
        if ":memory:" in url or url in {"sqlite://", "sqlite:///:memory:"}:
            from sqlalchemy.pool import StaticPool

            engine_kwargs["poolclass"] = StaticPool
        # pool_pre_ping is meaningless for SQLite and noisy with StaticPool.
        engine_kwargs.pop("pool_pre_ping", None)
    return create_engine(url, connect_args=connect_args, **engine_kwargs)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return make_engine(settings.database_url, echo=settings.db_echo)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def init_db(engine: Engine | None = None) -> None:
    """Create all tables (dev/test convenience; Alembic owns prod migrations)."""
    import saas.models  # noqa: F401 — register mappers on Base.metadata

    Base.metadata.create_all(engine or get_engine())


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transactional session scope: commit on success, rollback on error."""
    session = (factory or get_session_factory())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "Base",
    "get_engine",
    "get_session_factory",
    "init_db",
    "make_engine",
    "session_scope",
]
