"""Integration fixtures: real PostgreSQL + Redis, migrations applied, clean state per test.

Deliberate choices
------------------
* **PostgreSQL, never SQLite.** The product's integrity guarantees — partial unique indexes,
  enum types, `integer[]` columns, `inet`, CHECK constraints, transactional DDL — do not exist in
  SQLite, so testing there would prove nothing about shipped behaviour.
* **Migrations are the setup path**, not `create_all()`. The test suite therefore also verifies
  that `alembic upgrade head` works.
* **One environment fixture, created first.** Fixture ordering bugs (an actor built before the
  database was prepared, or a Redis flush landing after a session was created) are a classic
  source of tests that pass alone and fail together. A single autouse fixture prepares everything
  in a fixed order, and every other fixture depends on it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql+psycopg://autoclip:autoclip@127.0.0.1:5432/postgres"
)
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")

DOMAIN_TABLES = [
    "audit_logs",
    "usage_records",
    "notifications",
    "processing_jobs",
    "analytics_snapshots",
    "publishing_records",
    "publishing_jobs",
    "oauth_states",
    "oauth_credentials",
    "youtube_channels",
    "repair_attempts",
    "quality_reports",
    "captions",
    "clip_edit_settings",
    "clip_versions",
    "clips",
    "candidate_scores",
    "candidate_moments",
    "important_moments",
    "topics",
    "video_analysis",
    "scenes",
    "transcript_segments",
    "speakers",
    "transcripts",
    "media_assets",
    "source_videos",
    "project_stage_events",
    "projects",
    "workspace_members",
    "sessions",
    "workspaces",
    "users",
]


def _admin_engine():
    return create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")


# --------------------------------------------------------------------- session scope


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Create a throwaway database for the session, migrate it, drop it at the end."""
    name = f"autoclip_it_{uuid.uuid4().hex[:10]}"
    try:
        with _admin_engine().connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("TEST_REQUIRE_SERVICES") == "1":
            pytest.fail(
                f"PostgreSQL unavailable for required integration tests: {type(exc).__name__}"
            )
        pytest.skip(f"PostgreSQL unavailable for integration tests: {type(exc).__name__}")

    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{name}"
    os.environ["DATABASE_URL"] = url

    from app.core.config import get_settings
    from app.db.session import reset_engine

    get_settings.cache_clear()
    reset_engine()

    backend_dir = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")

    yield url

    from app.db.session import reset_engine as _reset

    _reset()
    with _admin_engine().connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def redis_available() -> str:
    try:
        import redis

        client = redis.from_url(REDIS_URL)
        client.ping()
        client.close()
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("TEST_REQUIRE_SERVICES") == "1":
            pytest.fail(f"Redis unavailable for required integration tests: {type(exc).__name__}")
        pytest.skip(f"Redis unavailable for integration tests: {type(exc).__name__}")
    return REDIS_URL


# --------------------------------------------------------------------- per test


@pytest.fixture(autouse=True)
def prepared_env(database_url: str, redis_available: str, monkeypatch) -> Iterator[None]:
    """Prepare a clean environment for every test, before anything else is constructed.

    Order matters and is fixed here:
      1. point settings at the throwaway database (and clear caches)
      2. truncate every domain table
      3. flush the Redis index used by the suite
    """
    import redis as redis_lib

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", redis_available)
    monkeypatch.setenv("CELERY_BROKER_URL", redis_available)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", redis_available)

    from app.core.config import get_settings
    from app.db.session import get_engine, reset_engine

    get_settings.cache_clear()
    reset_engine()

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(DOMAIN_TABLES)} RESTART IDENTITY CASCADE"))

    client = redis_lib.from_url(redis_available)
    client.flushdb()
    client.close()

    yield

    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def db_engine(prepared_env):
    from app.db.session import get_engine

    return get_engine()


@pytest.fixture
def redis_client(redis_available: str, prepared_env):
    import redis

    client = redis.from_url(redis_available)
    yield client
    client.close()


@pytest.fixture
def app(prepared_env):
    from app.main import create_app

    return create_app()


def new_client(app) -> TestClient:
    """A client with its own cookie jar — i.e. an independent browser session."""
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Origin": "http://localhost:3000"})
    return client


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    c = new_client(app)
    yield c
    c.close()


# --------------------------------------------------------------------- actors


class Actor:
    """A registered user with a live session, workspace, and CSRF token."""

    def __init__(self, client: TestClient, email: str, password: str, workspace: str) -> None:
        self.client = client
        self.email = email
        self.password = password
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": password,
                "display_name": email.split("@", maxsplit=1)[0].title(),
                "workspace_name": workspace,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        self.user_id = body["user"]["id"]
        self.csrf = body["csrf_token"]
        self.workspace_id = body["user"]["workspaces"][0]["id"]

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    def create_project(self, **overrides) -> dict:
        payload = {"name": "Test Project", **overrides}
        resp = self.client.post(
            f"/api/v1/workspaces/{self.workspace_id}/projects",
            json=payload,
            headers=self.headers,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()


@pytest.fixture
def alice(app) -> Iterator[Actor]:
    c = new_client(app)
    yield Actor(c, "alice@example.com", "alice-strong-password", "Alice Studio")
    c.close()


@pytest.fixture
def bob(app) -> Iterator[Actor]:
    c = new_client(app)
    yield Actor(c, "bob@example.com", "bob-strong-password", "Bob Studio")
    c.close()
