"""Shared test configuration.

Environment is configured here, once, before any app module imports settings. Tests must never
rely on the developer's local `.env` — CI has none, and a test that passes only on one machine
is worse than no test.
"""

from __future__ import annotations

import base64
import os

import pytest


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


# --- environment (set before app imports) -------------------------------------------
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "SECRET_KEY": _key(),
        "SECRET_ENCRYPTION_KEY": _key(),
        "PASSWORD_PEPPER": "test-pepper-value-123456",
        "DATABASE_URL": os.environ.get(
            "TEST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/autoclip_test"
        ),
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15"),
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_ROOT": "/tmp/autoclip-test-storage",
        "LOG_FORMAT": "console",
        "LOG_LEVEL": "WARNING",
        "AI_PROVIDER": "none",
        "YOUTUBE_CLIENT_ID": "",
        "YOUTUBE_CLIENT_SECRET": "",
        # No broker in CI. Eager mode runs the real task body in-process, so the pipeline,
        # the database writes and the error contract are all still exercised. That a message
        # reaches a *separate* worker process is verified manually, not claimed here.
        "CELERY_TASK_ALWAYS_EAGER": "true",
    }
)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Settings are cached; clear between tests so env overrides take effect."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def ffmpeg_available() -> bool:
    import shutil

    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture
def require_ffmpeg(ffmpeg_available: bool) -> None:
    if not ffmpeg_available:
        pytest.skip("ffmpeg/ffprobe not available on this host")
