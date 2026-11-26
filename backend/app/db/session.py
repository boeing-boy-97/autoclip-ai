"""Database engine and session management.

Sync SQLAlchemy is used deliberately: FastAPI runs sync route handlers in a threadpool, and the
Celery workers, Alembic and the media pipeline are all sync. Mixing async DB access with
CPU-bound FFmpeg work adds complexity without improving throughput — the bottleneck is video
encoding, not the API's SQL.

Lint note: this module owns the process-wide engine/session singletons (so it must reassign
module globals) and registers a SQLAlchemy event listener whose signature is fixed by the
library. Both are intentional and ignored at file level below.
"""

from __future__ import annotations

# ruff: noqa: PLW0603, PLC0415, ARG001, ANN202
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine(settings: Settings) -> Engine:
    url = settings.database_url
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url,
        pool_pre_ping=True,
        echo=settings.db_echo,
        future=True,
        **(
            {}
            if is_sqlite
            else {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout": settings.db_pool_timeout,
                "pool_recycle": 1800,
                "connect_args": {"application_name": "autoclip-api"},
            }
        ),
    )

    # Tests fail loudly if a workspace filter is forgotten: an unfiltered query inside a
    # workspace context is a security bug, not a performance problem.
    @event.listens_for(engine, "before_cursor_execute")
    def _log_slow_query(
conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if settings.log_level == "DEBUG":  # pragma: no cover - diagnostic only
            import time

            context._query_start = time.perf_counter()

    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine(settings or get_settings())
    return _engine


def get_session_factory(
    settings: Settings | None = None,
) -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings),
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
    return _session_factory


def reset_engine() -> None:
    """Drop cached engine/factory (tests switching databases, or settings changed)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for workers and scripts: commit on success, rollback on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one transaction per request, with an explicit boundary.

    Committing here (rather than trusting every handler to call `commit()`) removes a class of
    bug where an endpoint returns 201 while its writes are silently rolled back at session close.
    Anything raised out of the handler rolls the whole unit of work back. Handlers that must
    persist something *despite* a later error (e.g. a failed-login audit entry) call `commit()`
    themselves first; a subsequent rollback is then a no-op.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
