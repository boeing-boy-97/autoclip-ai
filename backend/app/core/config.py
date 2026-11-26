"""Application settings.

Design rules enforced here (see docs/ARCHITECTURE.md §11):
  * Required configuration has no silent default — a missing value raises at boot and names itself.
  * Placeholder-looking secrets are rejected outside development.
  * Optional integrations are represented explicitly (None) so the app can report
    "not configured" instead of silently substituting behaviour.
  * Production refuses unsafe development shortcuts (local storage backend).
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
StorageBackend = Literal["local", "s3"]
AIProvider = Literal["none", "openai", "anthropic", "google", "openai_compatible"]
ASRProvider = Literal["local", "openai", "deepgram", "assemblyai"]

PLACEHOLDER_MARKERS = ("REPLACE_ME", "changeme", "CHANGE_ME", "your-", "xxxx")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app
    environment: Environment = "development"
    app_version: str = "0.1.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"
    api_prefix: str = "/api/v1"
    public_api_url: str = "http://localhost:8000"
    public_web_url: str = "http://localhost:3000"
    cors_allowed_origins: str = "http://localhost:3000"

    # ---------------------------------------------------------------- security (required)
    # Length/structure is validated by _validate_key, which produces one actionable message
    # for every failure mode instead of a generic "too short" error.
    secret_key: str = Field(...)
    secret_encryption_key: str = Field(...)
    password_pepper: str = Field(..., min_length=16)
    session_cookie_name: str = "ac_session"
    session_ttl_hours: int = 168
    csrf_enabled: bool = True
    rate_limit_per_minute: int = 120
    rate_limit_upload_per_hour: int = 30
    max_upload_mb: int = 4096

    # ---------------------------------------------------------------- database (required)
    database_url: str = Field(..., min_length=1)
    database_url_migrations: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_echo: bool = False

    # ---------------------------------------------------------------- redis (required)
    redis_url: str = Field(..., min_length=1)
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_task_always_eager: bool = False

    # ---------------------------------------------------------------- storage
    storage_backend: StorageBackend = "local"
    local_storage_root: str = "./storage"
    s3_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_force_path_style: bool = True
    signed_url_ttl_seconds: int = 600
    min_free_disk_gb: float = 5.0

    # ---------------------------------------------------------------- media
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    audio_target_lufs: float = -14.0
    audio_true_peak_dbtp: float = -1.0
    # Path to the scoring weights. Relative to the process working directory (the repo root in
    # Docker, backend/ when running pytest from there).
    scoring_config_path: str = "../config/scoring.yaml"
    tmp_root: str = "/tmp/autoclip"  # noqa: S108 - per-job scratch dir, cleaned in finally
    render_target_width: int = 1080
    render_target_height: int = 1920
    max_source_duration_minutes: int = 360

    # ---------------------------------------------------------------- AI (optional)
    ai_provider: AIProvider = "none"
    ai_model_fast: str = "gpt-4o-mini"
    ai_model_quality: str = "gpt-4o"
    ai_model_vision: str = "gpt-4o-mini"
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_max_frames_per_video: int = 48
    ai_chunk_seconds: int = 240

    # ---------------------------------------------------------------- ASR
    asr_provider: ASRProvider = "local"
    asr_model: str = "large-v3"
    asr_device: str = "auto"
    asr_compute_type: str = "auto"
    asr_beam_size: int = 5
    asr_vad_filter: bool = True
    asr_model_dir: str = "./models/asr"  # where weights are cached; download happens once
    openai_api_key: str | None = None
    deepgram_api_key: str | None = None
    hf_token: str | None = None

    # ---------------------------------------------------------------- YouTube (optional)
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_redirect_uri: str = "http://localhost:8000/api/v1/youtube/oauth/callback"
    youtube_daily_quota_units: int = 10_000

    # ---------------------------------------------------------------- budget
    project_default_budget_usd: float = 5.0
    workspace_monthly_budget_usd: float = 100.0
    budget_warn_threshold: float = 0.80
    budget_hard_stop: bool = True

    # ---------------------------------------------------------------- frontend proxy
    api_internal_url: str = "http://localhost:8000"

    # ================================================================ validators
    @field_validator("secret_key", "secret_encryption_key")
    @classmethod
    def _validate_key(cls, v: str, info: ValidationInfo) -> str:
        """Keys must be base64 of exactly 32 bytes; placeholders are rejected outside dev."""
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:
            raise ValueError(
                f"{info.field_name} must be base64-encoded 32 bytes. "
                'Generate with: python -c "import os,base64;'
                'print(base64.b64encode(os.urandom(32)).decode())"'
            ) from exc
        if len(raw) != 32:
            raise ValueError(
                f"{info.field_name} must be base64 of exactly 32 bytes (AES-256); it decoded to "
                f"{len(raw)} bytes. Generate one with: python -c "
                '"import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
            )
        # Check the DECODED payload too: a placeholder is not visible in its base64 form.
        probe = (v + " " + raw.decode("utf-8", errors="ignore")).upper()
        if any(marker.upper() in probe for marker in PLACEHOLDER_MARKERS):
            raise ValueError(
                f"{info.field_name} contains a placeholder value. Generate a real key with: "
                'python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
            )
        return v

    @field_validator("password_pepper")
    @classmethod
    def _validate_pepper(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError(
                "password_pepper must be at least 16 characters "
                "(it is mixed into every password hash)."
            )
        return v

    @field_validator("youtube_redirect_uri")
    @classmethod
    def _validate_redirect(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("youtube_redirect_uri must be an absolute http(s) URL.")
        return v

    @model_validator(mode="after")
    def _validate_environment(self) -> Settings:
        if self.environment == "production":
            # Reject placeholders so a copy-pasted .env can never reach production.
            for field in ("secret_key", "secret_encryption_key", "password_pepper"):
                value = getattr(self, field)
                if any(marker in value for marker in PLACEHOLDER_MARKERS):
                    raise ValueError(f"{field} still contains a placeholder value in production.")
            if self.storage_backend == "local":
                raise ValueError(
                    "storage_backend=local is not permitted in production; configure S3-compatible "
                    "object storage (S3_BUCKET etc.)."
                )
            if self.celery_task_always_eager:
                raise ValueError("celery_task_always_eager must be false in production.")
        return self

    @model_validator(mode="after")
    def _default_urls(self) -> Settings:
        if self.celery_broker_url is None:
            object.__setattr__(self, "celery_broker_url", self.redis_url)
        if self.celery_result_backend is None:
            object.__setattr__(self, "celery_result_backend", self.redis_url)
        return self

    # ================================================================ derived helpers
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def youtube_configured(self) -> bool:
        """True only when a usable OAuth client is present. Never assume."""
        return bool(self.youtube_client_id and self.youtube_client_secret)

    @property
    def ai_configured(self) -> bool:
        return self.ai_provider != "none" and bool(self.ai_api_key)

    @property
    def storage_configured(self) -> bool:
        if self.storage_backend == "local":
            return True
        return bool(self.s3_bucket and self.s3_access_key_id and self.s3_secret_access_key)

    def integration_status(self) -> dict[str, dict[str, object]]:
        """Machine-readable integration state, surfaced by /health.

        Optional integrations report ``configured: false`` with the exact env vars to set —
        the product never substitutes fake behaviour for a missing integration.
        """
        return {
            "youtube": {
                "configured": self.youtube_configured,
                "missing_env": []
                if self.youtube_configured
                else ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"],
                "publishing_available": self.youtube_configured,
            },
            "ai_provider": {
                "configured": self.ai_configured,
                "provider": self.ai_provider,
                "missing_env": [] if self.ai_configured else ["AI_PROVIDER", "AI_API_KEY"],
                "heuristic_fallback_available": True,
            },
            "asr": {
                "configured": True,  # local faster-whisper needs no credentials
                "provider": self.asr_provider,
                "diarization_configured": bool(self.hf_token),
            },
            "storage": {
                "configured": self.storage_configured,
                "backend": self.storage_backend,
                "missing_env": []
                if self.storage_configured
                else ["S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"],
            },
        }


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor.

    Deliberately NOT instantiated at import time: requiring env at import would make modules
    untestable, hide which component needs which variable, and turn a config error into an
    opaque import failure. Callers obtain settings explicitly (FastAPI dependency or startup).
    """
    return Settings()
