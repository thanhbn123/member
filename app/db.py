"""Database engine / session management (SQLite for local dev, PostgreSQL for production)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine(database_url: str, echo: bool = False) -> Engine:
    settings = get_settings()
    kwargs: dict = {"echo": echo, "future": True, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in database_url or "mode=memory" in database_url:
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    engine = create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):
        # SQLite needs FK enforcement turned on explicitly; PostgreSQL always enforces them.
        @event.listens_for(engine, "connect")
        def _fk_pragma(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = _build_engine(settings.database_url, echo=settings.db_echo)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts / background jobs."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """Drop cached engine/session factory (tests, or after changing DATABASE_URL)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
