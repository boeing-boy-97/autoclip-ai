"""Startup self-check for a deployment's environment.

Everything here fails *late and confusingly* if it is left unchecked: a read-only `TMP_ROOT` makes
every upload a 500, a missing FFmpeg makes `/health/readyz` 503 forever, and an ephemeral
`ASR_MODEL_DIR` re-downloads several gigabytes of Whisper weights on every restart. On a PaaS those
conditions are the normal state of a misconfigured service, so the app says so at boot, by name.

Two severities:

* ``error`` — the process refuses to start. Only used where serving requests would be dishonest:
  a scratch directory that cannot hold a file, or storage that cannot store one.
* ``warning`` — the process starts, logs loudly, and `/health/readyz` tells the truth. FFmpeg is a
  warning rather than an error so an API-only deployment can still boot (its readiness stays 503
  until FFmpeg is installed, which is exactly the signal an operator needs).

The check is deliberately side-effect-light: it creates the scratch/storage directories (a process
that must write there may as well ensure they exist) but never deletes anything.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.config import Settings

Level = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Issue:
    level: Level
    code: str
    detail: str


def _usable_dir(path: Path) -> int | None:
    """Create `path` if needed and prove it is writable. Returns free bytes, or None."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".autoclip-write-probe"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return None
    try:
        return shutil.disk_usage(path).free
    except OSError:  # pragma: no cover - platform specific
        return None


def check(settings: Settings) -> list[Issue]:
    """Return every environment problem worth reporting, most severe first."""
    issues: list[Issue] = []

    # ---------------------------------------------------------------- scratch space
    tmp_root = Path(settings.tmp_root)
    free = _usable_dir(tmp_root)
    if free is None:
        issues.append(
            Issue(
                "error",
                "tmp_root_unusable",
                f"TMP_ROOT '{tmp_root}' is not writable. Uploads spool here and renders build "
                "their output here; mount a disk or point TMP_ROOT at a writable path.",
            )
        )
    else:
        needed = settings.max_upload_mb * 1024 * 1024
        if free < needed:
            issues.append(
                Issue(
                    "warning",
                    "tmp_root_small",
                    f"TMP_ROOT '{tmp_root}' has {free / 1e9:.1f} GB free but MAX_UPLOAD_MB is "
                    f"{settings.max_upload_mb} MB. An upload near the limit will fail with ENOSPC: "
                    "lower MAX_UPLOAD_MB or mount a bigger disk.",
                )
            )

    # ---------------------------------------------------------------- object storage
    if settings.storage_backend == "local":
        if _usable_dir(Path(settings.local_storage_root)) is None:
            issues.append(
                Issue(
                    "error",
                    "storage_root_unusable",
                    f"LOCAL_STORAGE_ROOT '{settings.local_storage_root}' is not writable.",
                )
            )
        if settings.environment == "production":
            issues.append(
                Issue(
                    "error",
                    "local_storage_in_production",
                    "STORAGE_BACKEND=local in production: media would live on the instance's "
                    "ephemeral disk and disappear on the next deploy. Configure S3 "
                    "(S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY).",
                )
            )
    elif not settings.storage_configured:
        issues.append(
            Issue(
                "error",
                "s3_not_configured",
                "STORAGE_BACKEND=s3 needs S3_BUCKET, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY.",
            )
        )

    # ---------------------------------------------------------------- media tooling
    missing = [
        f"{name}={binary}"
        for name, binary in (("ffmpeg", settings.ffmpeg_bin), ("ffprobe", settings.ffprobe_bin))
        if shutil.which(binary) is None
    ]
    if missing:
        issues.append(
            Issue(
                "warning",
                "media_tooling_missing",
                "FFmpeg is not on PATH ("
                + ", ".join(missing)
                + "). Uploads, analysis and rendering will fail; /health/readyz reports this as "
                "503. On a PaaS deploy the Docker image (which installs FFmpeg) instead of a "
                "buildpack.",
            )
        )

    # ---------------------------------------------------------------- ASR weights
    if settings.asr_provider == "local":
        model_dir = Path(settings.asr_model_dir)
        populated = False
        try:
            populated = model_dir.exists() and next(model_dir.iterdir(), None) is not None
        except OSError:  # pragma: no cover - unreadable directory
            populated = False
        if not populated:
            issues.append(
                Issue(
                    "warning",
                    "asr_model_cache_missing",
                    f"ASR_MODEL_DIR '{model_dir}' is empty: the Whisper weights are downloaded on "
                    "first use (several GB). If that path is on an ephemeral disk the download "
                    "repeats on every deploy — mount a persistent disk or set ASR_PROVIDER=openai.",
                )
            )

    # ---------------------------------------------------------------- exposure
    if settings.environment == "production":
        if settings.docs_enabled:
            issues.append(
                Issue(
                    "warning",
                    "docs_enabled_in_production",
                    "/docs and /openapi.json are public: a complete map of the API. Set "
                    "DOCS_ENABLED=false unless you mean to publish the schema.",
                )
            )
        if "*" in settings.trusted_host_list:
            issues.append(
                Issue(
                    "warning",
                    "trusted_hosts_wildcard",
                    "TRUSTED_HOSTS=* accepts any Host header. Set your API domain(s), e.g. "
                    "TRUSTED_HOSTS=api.example.com.",
                )
            )
        if not settings.session_cookie_secure:
            issues.append(
                Issue(
                    "warning",
                    "insecure_session_cookie",
                    "SESSION_COOKIE_SECURE=false: the session cookie would travel over plaintext. "
                    "Settings already refuses this in production — this is a reminder that the "
                    "value must also be correct on the web tier.",
                )
            )

    # ---------------------------------------------------------------- limits
    limits = (
        settings.rate_limit_per_minute,
        settings.rate_limit_upload_per_hour,
        settings.rate_limit_auth_per_minute,
        settings.rate_limit_analysis_per_hour,
        settings.rate_limit_render_per_minute,
    )
    if not any(limits):
        issues.append(
            Issue(
                "warning",
                "rate_limiting_disabled",
                "every RATE_LIMIT_* value is 0, so nothing is rate limited. On a public "
                "deployment that allows unbounded uploads and login attempts.",
            )
        )

    # errors first: the caller stops at the first fatal one and the log order should reflect that
    return sorted(issues, key=lambda issue: 0 if issue.level == "error" else 1)


def fatal(issues: list[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.level == "error"]


__all__ = ["Issue", "check", "fatal"]
