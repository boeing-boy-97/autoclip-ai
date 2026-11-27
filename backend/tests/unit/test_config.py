"""Phase 0 tests: settings fail-fast rules, integration honesty, and log redaction.

These encode the product rules from the master spec:
  * Missing configuration is reported by name, never defaulted silently.
  * Placeholder secrets cannot reach production.
  * Optional integrations are reported as not configured — never faked.
"""

from __future__ import annotations

import base64
import os

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import REDACTED, redaction_processor

pytestmark = pytest.mark.unit


def real_key() -> str:
    """A genuinely random 32-byte base64 key (not the 'xxxx' test pattern the guard rejects)."""
    return base64.b64encode(os.urandom(32)).decode()


MINIMAL = {
    "password_pepper": "a-real-pepper-value-1234",
    "database_url": "postgresql+psycopg://u:p@localhost:5432/db",
    "redis_url": "redis://localhost:6379/0",
    # Pinned so a `CELERY_TASK_ALWAYS_EAGER` in the ambient environment (conftest sets it true so
    # the suite runs without a broker) cannot leak into a production-settings test.
    "celery_task_always_eager": False,
}


def make(**overrides) -> Settings:
    """Build settings in isolation from the ambient environment used by other tests."""
    kwargs = {
        **MINIMAL,
        "environment": "development",
        "secret_key": real_key(),
        "secret_encryption_key": real_key(),
        **overrides,
    }
    return Settings(_env_file=None, **kwargs)


class TestRequiredConfiguration:
    def test_missing_required_fields_are_named(self, monkeypatch):
        # conftest.py populates env for the suite; remove it so "missing" is genuinely missing.
        for var in (
            "SECRET_KEY",
            "SECRET_ENCRYPTION_KEY",
            "PASSWORD_PEPPER",
            "DATABASE_URL",
            "REDIS_URL",
        ):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValidationError) as exc:
            Settings(_env_file=None)
        message = str(exc.value)
        for field in (
            "secret_key",
            "secret_encryption_key",
            "password_pepper",
            "database_url",
            "redis_url",
        ):
            assert field in message, f"{field} must be named in the error"

    def test_valid_minimal_config_loads(self):
        s = make()
        assert s.environment == "development"
        assert s.session_cookie_name == "ac_session"

    @pytest.mark.parametrize("bad_key", ["not-base64!!", "short", ""])
    def test_non_base64_key_rejected_with_guidance(self, bad_key):
        with pytest.raises(ValidationError) as exc:
            make(secret_key=bad_key)
        assert "base64" in str(exc.value).lower()

    def test_key_must_decode_to_exactly_32_bytes(self):
        with pytest.raises(ValidationError) as exc:
            make(secret_key=base64.b64encode(b"k" * 31).decode())
        assert "32" in str(exc.value)

    def test_placeholder_key_rejected_even_when_base64_wrapped(self):
        """A marker is invisible after encoding — detection must inspect the decoded bytes."""
        placeholder = base64.b64encode(b"REPLACE_ME".ljust(32, b"_")).decode()
        with pytest.raises(ValidationError) as exc:
            make(secret_encryption_key=placeholder)
        assert "placeholder" in str(exc.value).lower()

    def test_env_example_placeholder_values_are_rejected(self):
        with pytest.raises(ValidationError):
            make(secret_key="REPLACE_ME_base64_32_bytes")

    def test_weak_pepper_rejected(self):
        with pytest.raises(ValidationError) as exc:
            make(password_pepper="short")
        assert "16" in str(exc.value)


class TestProductionGuards:
    def test_production_rejects_local_storage(self):
        with pytest.raises(ValidationError) as exc:
            make(environment="production", storage_backend="local")
        assert "storage_backend" in str(exc.value)

    def test_production_rejects_eager_celery(self):
        with pytest.raises(ValidationError) as exc:
            make(
                environment="production",
                storage_backend="s3",
                s3_bucket="b",
                celery_task_always_eager=True,
            )
        assert "always_eager" in str(exc.value)

    def test_production_accepts_s3_storage(self):
        s = make(
            environment="production",
            storage_backend="s3",
            session_cookie_secure=True,
            s3_bucket="bucket",
            s3_access_key_id="key",
            s3_secret_access_key="secret",
        )
        assert s.storage_configured is True

    def test_production_rejects_insecure_session_cookie(self):
        """A production cookie that is not `Secure` would travel over plaintext connections."""
        with pytest.raises(ValidationError) as exc:
            make(
                environment="production",
                storage_backend="s3",
                s3_bucket="b",
                session_cookie_secure=False,
            )
        assert "session_cookie_secure" in str(exc.value)


class TestNeonEnvironment:
    def test_neon_postgres_and_object_storage_variables_are_supported(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://u:p@db.example/test")
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://storage.example")
        monkeypatch.setenv("AWS_REGION", "us-east-2")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

        settings = make(
            database_url="postgresql://u:p@db.example/app",
            storage_backend="s3",
            s3_bucket="uploads",
        )

        assert settings.database_url == "postgresql+psycopg://u:p@db.example/app"
        assert settings.database_url_migrations == "postgresql+psycopg://u:p@db.example/test"
        assert settings.s3_endpoint_url == "https://storage.example"
        assert settings.s3_region == "us-east-2"
        assert settings.storage_configured is True


class TestIntegrationHonesty:
    def test_youtube_unconfigured_is_reported_not_faked(self):
        s = make()
        yt = s.integration_status()["youtube"]
        assert yt["configured"] is False
        assert yt["publishing_available"] is False
        assert "YOUTUBE_CLIENT_ID" in yt["missing_env"]

    def test_youtube_configured_only_with_both_credentials(self):
        assert make(youtube_client_id="id").youtube_configured is False
        assert make(youtube_client_id="id", youtube_client_secret="s").youtube_configured is True

    def test_ai_unconfigured_reports_heuristic_fallback(self):
        ai = make().integration_status()["ai_provider"]
        assert ai["configured"] is False
        assert ai["heuristic_fallback_available"] is True
        assert "AI_API_KEY" in ai["missing_env"]

    def test_ai_provider_without_key_is_not_configured(self):
        assert make(ai_provider="openai").ai_configured is False
        assert make(ai_provider="openai", ai_api_key="k").ai_configured is True

    def test_local_storage_always_configured_dev(self):
        assert make(storage_backend="local").storage_configured is True

    def test_cors_origins_parsed(self):
        s = make(cors_allowed_origins="http://a.test, http://b.test ,")
        assert s.cors_origins == ["http://a.test", "http://b.test"]

    def test_broker_defaults_to_redis_url(self):
        s = make()
        assert s.celery_broker_url == s.redis_url
        assert s.celery_result_backend == s.redis_url


class TestLogRedaction:
    def _run(self, payload: dict) -> dict:
        return redaction_processor(None, None, dict(payload))

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "api_key",
            "authorization",
            "cookie",
            "refresh_token",
            "access_token",
            "client_secret",
            "SECRET_KEY",
            "pepper",
            "X-Amz-Signature",
        ],
    )
    def test_sensitive_keys_redacted(self, key):
        assert self._run({key: "super-secret"})[key] == REDACTED

    def test_signed_url_query_stripped(self):
        out = self._run(
            {"url": "https://b.s3.amazonaws.com/v.mp4?X-Amz-Signature=abc123&X-Amz-Expires=600"}
        )
        assert "abc123" not in out["url"]
        assert out["url"].startswith("https://b.s3.amazonaws.com/v.mp4")

    def test_email_pseudonymised(self):
        out = self._run({"email": "alice.smith@example.com"})
        assert out["email"] == "a***@example.com"

    def test_non_sensitive_values_untouched(self):
        out = self._run({"event": "stage.succeeded", "duration_ms": 1200, "queue": "render"})
        assert out == {"event": "stage.succeeded", "duration_ms": 1200, "queue": "render"}
