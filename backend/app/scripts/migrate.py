"""Apply database migrations exactly once, safely, from a deploy hook.

Why this is not just `alembic upgrade head` in a shell command:

* **Concurrency.** A PaaS release step can run while the previous instance is still serving, and a
  rolling deploy can start several instances at once. Two concurrent `upgrade head` runs interleave
  DDL and deadlock. A PostgreSQL advisory lock makes the second waiter block until the first
  finishes, then find nothing to do.
* **One URL.** The migration URL comes from `Settings.database_url_migrations` (falling back to
  `database_url`), the same resolution `alembic/env.py` uses, so a deploy can migrate with a role
  that has DDL rights while the app runs with one that does not.
* **Exit codes.** A failed migration must fail the release, loudly, with the error visible in the
  deploy log — not after the new instance starts answering requests against a half-migrated schema.

Usage:
    python -m app.scripts.migrate                # upgrade to head
    python -m app.scripts.migrate --check        # print current vs head, change nothing
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.core.config import get_settings

logger = logging.getLogger("autoclip.migrate")

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Any constant works as long as every deployment uses the same one. Derived from "autoclip" so a
# stray lock in a shared database is recognisable.
ADVISORY_LOCK_KEY = 0x6175746F636C6970


def _alembic_config() -> Config:
    """Alembic config with explicit paths, so it works from any working directory."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
    return cfg


def _database_url() -> str:
    """The migration URL, resolved without requiring the rest of the app config.

    A release hook runs before the app does and needs nothing but a database: reading the
    environment first means a missing REDIS_URL or S3 bucket fails the *web* deploy, with a
    message about that, instead of failing here as a confusing migration error.
    """
    for name in ("DATABASE_URL_MIGRATIONS", "DATABASE_URL_UNPOOLED", "DATABASE_URL"):
        value = os.environ.get(name)
        if value:
            return value

    # Not in the environment: `.env` files and defaults still apply.
    settings = get_settings()
    return settings.database_url_migrations or settings.database_url


def _sync_url(url: str) -> str:
    """alembic/sqlalchemy URL for the psycopg (v3) driver we actually ship."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _current_and_head(cfg: Config, url: str) -> tuple[str | None, str | None]:
    engine = create_engine(_sync_url(url), poolclass=None)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    return current, ScriptDirectory.from_config(cfg).get_current_head()


def _upgrade(cfg: Config, url: str) -> None:
    if not url.startswith("postgresql"):
        # SQLite (local dev) has no advisory locks; a single developer process does not need one.
        logger.warning("migrate.no_advisory_lock", extra={"url_scheme": url.split(":", 1)[0]})
        command.upgrade(cfg, "head")
        return

    import psycopg  # noqa: PLC0415 - only needed for the locking path

    dsn = _sync_url(url).replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn, autocommit=True) as conn:
        logger.info("migrate.waiting_for_lock", extra={"key": ADVISORY_LOCK_KEY})
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            command.upgrade(cfg, "head")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply database migrations (idempotent).")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the current and head revision without changing anything",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = _alembic_config()
    url = _database_url()

    try:
        if args.check:
            current, head = _current_and_head(cfg, url)
            print(f"current={current or '(none)'} head={head or '(none)'}")
            return 0 if current == head else 1

        _upgrade(cfg, url)
        current, head = _current_and_head(cfg, url)
        print(f"migrated: current={current or '(none)'} head={head or '(none)'}")
        return 0
    except Exception as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        logger.exception("migrate.failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
