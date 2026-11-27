"""Regression tests for the cloud (PaaS) deployment surface.

The deployment paths are where "works on my machine" hides: a worker command that names a module
that does not exist, a Dockerfile whose last stage is a --reload dev server, migrations racing each
other, and a scratch directory that the platform never made writable. Each is cheap to assert here
and expensive to discover in a deploy log.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"
SCRIPTS = BACKEND / "scripts"


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def settings_for(tmp_path: Path, **overrides):
    from app.core.config import Settings

    kwargs = {
        "secret_key": _key(),
        "secret_encryption_key": _key(),
        "password_pepper": "a-real-pepper-value-1234",
        "database_url": "postgresql+psycopg://u:p@127.0.0.1:1/db",
        "redis_url": "redis://127.0.0.1:1/0",
        "storage_backend": "local",
        "local_storage_root": str(tmp_path / "storage"),
        "tmp_root": str(tmp_path / "tmp"),
        "asr_provider": "openai",  # keeps the ASR-weight warning out of unrelated tests
        "celery_task_always_eager": False,  # the suite sets it globally; production rejects it
        **overrides,
    }
    return Settings(_env_file=None, **kwargs)


# --------------------------------------------------------------------------- preflight


class TestPreflight:
    def test_a_healthy_environment_has_no_errors(self, tmp_path, monkeypatch):
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/ffmpeg")
        issues = preflight.check(settings_for(tmp_path))
        assert preflight.fatal(issues) == [], issues

    def test_unwritable_scratch_directory_is_fatal(self, tmp_path, monkeypatch):
        """Uploads spool to TMP_ROOT: a read-only path turns every upload into a 500."""
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/ffmpeg")
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        issues = preflight.check(settings_for(tmp_path, tmp_root=str(blocked / "tmp")))
        codes = {issue.code for issue in preflight.fatal(issues)}
        assert "tmp_root_unusable" in codes

    def test_scratch_smaller_than_the_upload_limit_warns(self, tmp_path, monkeypatch):
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/ffmpeg")
        monkeypatch.setattr(
            preflight.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(1e6, 0, 100)
        )
        issues = preflight.check(settings_for(tmp_path, max_upload_mb=4096))
        assert "tmp_root_small" in {issue.code for issue in issues}
        assert preflight.fatal(issues) == []

    def test_missing_ffmpeg_is_a_warning_not_a_refusal(self, tmp_path, monkeypatch):
        """An API-only deploy must still boot; readiness reports the truth instead."""
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
        issues = preflight.check(settings_for(tmp_path))
        assert "media_tooling_missing" in {issue.code for issue in issues}
        assert preflight.fatal(issues) == []

    def test_empty_asr_model_dir_warns_about_redownloads(self, tmp_path, monkeypatch):
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/ffmpeg")
        (tmp_path / "models").mkdir()
        issues = preflight.check(
            settings_for(tmp_path, asr_provider="local", asr_model_dir=str(tmp_path / "models"))
        )
        assert "asr_model_cache_missing" in {issue.code for issue in issues}

    def test_populated_asr_model_dir_is_quiet(self, tmp_path, monkeypatch):
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/ffmpeg")
        models = tmp_path / "models"
        (models / "models--Systran--faster-whisper-large-v3").mkdir(parents=True)
        issues = preflight.check(
            settings_for(tmp_path, asr_provider="local", asr_model_dir=str(models))
        )
        assert "asr_model_cache_missing" not in {issue.code for issue in issues}

    def test_production_exposure_warnings(self, tmp_path, monkeypatch):
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/ffmpeg")
        issues = preflight.check(
            settings_for(
                tmp_path,
                environment="production",
                storage_backend="s3",
                s3_bucket="b",
                s3_access_key_id="k",
                s3_secret_access_key="s",
                session_cookie_secure=True,
                docs_enabled=True,
                trusted_hosts="*",
            )
        )
        codes = {issue.code for issue in issues}
        assert {"docs_enabled_in_production", "trusted_hosts_wildcard"} <= codes

    def test_disabled_rate_limits_are_reported(self, tmp_path, monkeypatch):
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: "/usr/bin/ffmpeg")
        issues = preflight.check(
            settings_for(
                tmp_path,
                rate_limit_per_minute=0,
                rate_limit_upload_per_hour=0,
                rate_limit_auth_per_minute=0,
                rate_limit_analysis_per_hour=0,
                rate_limit_render_per_minute=0,
            )
        )
        assert "rate_limiting_disabled" in {issue.code for issue in issues}

    def test_errors_are_reported_before_warnings(self, tmp_path, monkeypatch):
        from app.core import preflight

        monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
        blocked = tmp_path / "blocked"
        blocked.write_text("x")
        issues = preflight.check(settings_for(tmp_path, tmp_root=str(blocked / "t")))
        levels = [issue.level for issue in issues]
        assert levels == sorted(levels, key=lambda level: 0 if level == "error" else 1)


# --------------------------------------------------------------------------- migrations


class TestMigrations:
    def test_alembic_config_points_at_real_files(self):
        from app.scripts.migrate import _alembic_config

        cfg = _alembic_config()
        assert Path(cfg.config_file_name or "").exists()
        script_location = Path(cfg.get_main_option("script_location") or "")
        assert script_location.is_dir(), script_location
        assert (script_location / "versions" / "0001_baseline.py").exists()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
            ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
            ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ],
    )
    def test_urls_are_normalised_to_the_shipped_driver(self, raw, expected):
        from app.scripts.migrate import _sync_url

        assert _sync_url(raw) == expected

    def test_advisory_lock_key_is_a_stable_64_bit_integer(self):
        from app.scripts.migrate import ADVISORY_LOCK_KEY

        assert isinstance(ADVISORY_LOCK_KEY, int)
        assert 0 < ADVISORY_LOCK_KEY < 2**63

    def test_url_comes_from_the_environment_without_booting_the_whole_config(self, monkeypatch):
        """A release hook must not need Redis/S3 to be configured; only the database."""
        from app.core.config import get_settings
        from app.scripts.migrate import _database_url

        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://env:pw@db:5432/app")
        monkeypatch.delenv("DATABASE_URL_MIGRATIONS", raising=False)
        monkeypatch.setenv("REDIS_URL", "")
        get_settings.cache_clear()
        assert _database_url() == "postgresql+psycopg://env:pw@db:5432/app"
        get_settings.cache_clear()

    def test_failed_connection_returns_a_non_zero_exit_code(self, monkeypatch):
        """A deploy hook that cannot reach the database must fail the release, not lie."""
        from app.scripts import migrate

        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/db")
        monkeypatch.setenv("DATABASE_URL_MIGRATIONS", "postgresql+psycopg://u:p@127.0.0.1:1/db")
        from app.core.config import get_settings

        get_settings.cache_clear()
        assert migrate.main(["--check"]) == 1
        get_settings.cache_clear()


# --------------------------------------------------------------------------- entrypoints


class TestProcessEntrypoints:
    @pytest.mark.parametrize(
        "script", ["start_api.sh", "start_worker.sh", "start_beat.sh", "release.sh"]
    )
    def test_script_is_executable_and_syntactically_valid(self, script: str):
        path = SCRIPTS / script
        assert path.exists(), path
        assert os.access(path, os.X_OK), f"{path} is not executable"
        result = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    def test_api_script_binds_the_platform_port_and_trusts_the_proxy(self):
        body = (SCRIPTS / "start_api.sh").read_text()
        assert "PORT:-8000" in body
        assert "--proxy-headers" in body
        assert "FORWARDED_ALLOW_IPS" in body

    def test_api_script_keeps_idle_connections_open_past_the_proxy_timeout(self):
        """A keep-alive shorter than the proxy's idle timeout produces intermittent 502s."""
        body = (SCRIPTS / "start_api.sh").read_text()
        assert "timeout-keep-alive" in body
        assert '"${UVICORN_KEEP_ALIVE:-75}"' in body

    @pytest.mark.parametrize("script", ["start_api.sh", "start_worker.sh", "start_beat.sh"])
    def test_entrypoints_run_modules_not_console_scripts(self, script: str):
        """`uvicorn` / `celery` on PATH is not guaranteed; `python -m` always resolves."""
        body = (SCRIPTS / script).read_text()
        assert 'exec "${PYTHON}" -m ' in body
        assert "exec uvicorn" not in body
        assert "exec celery" not in body

    def test_worker_script_uses_the_real_celery_module(self):
        body = (SCRIPTS / "start_worker.sh").read_text()
        assert "app.worker.celery_app" in body
        assert "app.workers" not in body

    def test_worker_script_consumes_queues_that_have_tasks(self):
        from app.db.repositories import JobRepository

        body = (SCRIPTS / "start_worker.sh").read_text()
        assert "media,transcribe,analyze" in body
        for queue in ("media", "transcribe", "analyze"):
            assert queue in JobRepository.ACTIVE_QUEUES

    def test_beat_script_uses_a_configurable_schedule_path(self):
        body = (SCRIPTS / "start_beat.sh").read_text()
        assert "CELERY_BEAT_SCHEDULE" in body
        assert "--schedule" in body


class TestDockerfile:
    @pytest.fixture(scope="class")
    @classmethod
    def body(cls) -> str:
        return (BACKEND / "Dockerfile").read_text()

    def test_the_last_stage_is_production(self, body: str):
        """While `dev` was last, a PaaS build without an explicit target deployed --reload."""
        stages = [line for line in body.splitlines() if line.startswith("FROM ")]
        assert "AS production" in stages[-1], stages[-1]

    def test_production_starts_the_platform_entrypoint(self, body: str):
        assert 'CMD ["/app/scripts/start_api.sh"]' in body

    def test_ffmpeg_is_installed_in_the_image(self, body: str):
        assert "ffmpeg" in body

    def test_dev_stage_remains_available_for_compose(self, body: str):
        assert "AS dev" in body
        assert "--reload" in body


class TestFlyBlueprint:
    def test_example_defines_three_processes_and_a_release_command(self):
        data = tomllib.loads((REPO_ROOT / "deploy" / "fly.toml.example").read_text())
        assert set(data["processes"]) == {"app", "worker", "beat"}
        assert data["deploy"]["release_command"].endswith("release.sh")
        assert data["http_service"]["internal_port"] == 8000
        assert data["http_service"]["checks"][0]["path"] == "/api/v1/health/readyz"

    def test_worker_process_warns_that_beat_must_not_scale(self):
        body = (REPO_ROOT / "deploy" / "fly.toml.example").read_text()
        assert "beat" in body
        assert "never" in body.lower()


class TestDeployDocs:
    def test_deploy_guide_covers_the_failure_modes_that_matter(self):
        body = (REPO_ROOT / "docs" / "DEPLOY.md").read_text()
        for needle in (
            "/api/v1/health/readyz",
            "TMP_ROOT",
            "ASR_MODEL_DIR",
            "release.sh",
            "start_worker.sh",
            "FORWARDED_ALLOW_IPS",
            "pgvector",
        ):
            assert needle in body, needle

    def test_readme_points_at_the_deploy_guide(self):
        body = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert "docs/DEPLOY.md" in body


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
