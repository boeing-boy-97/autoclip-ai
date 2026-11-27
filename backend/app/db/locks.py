"""PostgreSQL project locks shared by API mutations and long-running workers.

Session locks use a dedicated connection: service commits/rollbacks cannot release them early.
Always unlock before returning the connection to the pool. These require a direct/session-pooled
PostgreSQL endpoint; transaction-pooling proxies cannot preserve session advisory locks.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.repositories import ConflictError
from app.db.session import get_engine


def project_lock_key(workspace_id: uuid.UUID, project_id: uuid.UUID) -> int:
    raw = hashlib.sha256(f"autoclip:project:{workspace_id}:{project_id}".encode()).digest()
    return int.from_bytes(raw[:8], "big", signed=True)


def guard_project(db: Session, workspace_id: uuid.UUID, project_id: uuid.UUID) -> None:
    """Nonblocking transaction guard for short API operations; released on commit/rollback."""
    acquired = db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": project_lock_key(workspace_id, project_id)},
    )
    if not acquired:
        raise ConflictError(
            "project_busy", "project work is still executing; try again after it stops"
        )


@contextmanager
def project_execution(workspace_id: uuid.UUID, project_id: uuid.UUID) -> Iterator[bool]:
    """Exclusive execution even across checkpoints; a dead connection releases its lock."""
    key = project_lock_key(workspace_id, project_id)
    with get_engine().connect() as conn:
        acquired = bool(conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    conn.commit()
                except Exception:
                    # A failed unlock must never leave a session lock on a pooled connection.
                    conn.invalidate()
                    raise
