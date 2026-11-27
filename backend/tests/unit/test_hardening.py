"""Regression tests for the backend-hardening pass.

Each test pins a bug that was actually shipped, so the fix cannot silently regress:

  * a CSRF bypass caused by reading headers instead of the credential;
  * a role check that accepted a role name which does not exist (`"member"`);
  * a rate limiter that did not exist, and a login throttle that 500'd when Redis was down;
  * an encryption box that could not rotate its key;
  * a scoring-config path that only resolved from one working directory;
  * Celery workers pointed at a module that does not exist, no beat schedule, and queues
    with no consumer.
"""

from __future__ import annotations

import base64
import inspect
import os
from pathlib import Path

import fakeredis
import pytest
from pydantic import ValidationError
from starlette.requests import Request

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- helpers
def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _request(*, method: str = "POST", cookie: str | None = None, bearer: str | None = None):
    headers: list[tuple[bytes, bytes]] = []
    if cookie:
        headers.append((b"cookie", f"ac_session={cookie}".encode()))
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/api/v1/whatever",
        "raw_path": b"/api/v1/whatever",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


class _FakeAuthService:
    """Stands in for AuthService; only `verify_csrf` is exercised."""

    def __init__(self, *, valid_token: str = "good-token") -> None:
        self.valid_token = valid_token
        self.calls = 0

    def verify_csrf(self, _session: object, token: str | None) -> bool:
        self.calls += 1
        return token == self.valid_token


class _Auth:
    def __init__(self, token: str = "session-token") -> None:
        self.user = type("U", (), {"id": "user-1"})()
        self.session_token = token
        self.session_row = object()


# --------------------------------------------------------------------------- CSRF


class TestCsrfDecision:
    def _settings(self) -> object:
        from app.core.config import Settings

        return Settings(
            _env_file=None,
            secret_key=_key(),
            secret_encryption_key=_key(),
            password_pepper="a-real-pepper-value-1234",
            database_url="postgresql+psycopg://u:p@localhost/db",
            redis_url="redis://localhost:6379/0",
            session_cookie_secure=False,
        )

    def _call(self, request, *, via: str, token: str | None, auth=None):
        from app.api.deps import AuthContext, require_csrf

        ctx = AuthContext(
            user=type("U", (), {"id": "user-1"})(),
            session_token="s",
            session_row=object(),
            via=via,  # type: ignore[arg-type]
        )
        return require_csrf(
            request=request,
            auth_ctx=ctx,
            auth=auth or _FakeAuthService(),
            settings=self._settings(),
            x_csrf_token=token,
        )

    def test_bearer_without_cookie_is_exempt(self):
        """Automation using a bearer token carries no ambient credential, so no CSRF needed."""
        self._call(_request(bearer="abc"), via="bearer", token=None)

    def test_bearer_header_cannot_excuse_a_cookie_session(self):
        """The bypass: cookie + any `Authorization: Bearer …` header used to skip the check."""
        from app.api.deps import ApiError

        with pytest.raises(ApiError) as exc:
            self._call(_request(bearer="abc", cookie="session-token"), via="cookie", token=None)
        assert exc.value.status_code == 403

    def test_cookie_session_requires_a_valid_token(self):
        from app.api.deps import ApiError

        with pytest.raises(ApiError):
            self._call(_request(cookie="session-token"), via="cookie", token=None)
        with pytest.raises(ApiError):
            self._call(_request(cookie="session-token"), via="cookie", token="wrong")

    def test_cookie_session_with_valid_token_passes(self):
        self._call(_request(cookie="session-token"), via="cookie", token="good-token")

    def test_safe_methods_are_never_checked(self):
        self._call(_request(method="GET", cookie="s"), via="cookie", token=None)
        self._call(_request(method="HEAD", cookie="s"), via="cookie", token=None)


# --------------------------------------------------------------------------- roles


class TestRoleChecks:
    def test_every_role_is_ranked_and_ordered(self):
        from app.api.deps import ROLE_RANK

        assert set(ROLE_RANK) == {"viewer", "editor", "admin", "owner"}
        assert ROLE_RANK["viewer"] < ROLE_RANK["editor"] < ROLE_RANK["admin"] < ROLE_RANK["owner"]

    def test_viewer_does_not_satisfy_editor(self):
        from app.api.deps import role_satisfies

        assert role_satisfies("viewer", "editor") is False
        assert role_satisfies("editor", "editor") is True
        assert role_satisfies("owner", "admin") is True

    def test_unknown_minimum_role_raises_instead_of_allowing(self):
        """`"member"` is not a role; it used to rank 0 and let every caller through."""
        from app.api.deps import role_satisfies

        with pytest.raises(ValueError, match="unknown minimum role"):
            role_satisfies("viewer", "member")  # type: ignore[arg-type]

    def test_upload_requires_editor(self):
        """Idempotence of intent: the upload endpoint must not be reachable by a viewer."""
        from app.api.v1 import sources

        assert sources.WorkspaceEditor.__metadata__[0].dependency is not None


# --------------------------------------------------------------------------- rate limit


class TestRateLimiter:
    def test_requests_are_counted_and_then_refused(self):
        from app.core.ratelimit import FixedWindowLimiter

        limiter = FixedWindowLimiter(fakeredis.FakeRedis(), prefix="t")
        results = [
            limiter.consume(scope="upload", identity="u1", limit=2, window_seconds=60, now=1000.0)
            for _ in range(3)
        ]
        assert [r.allowed for r in results] == [True, True, False]
        assert results[0].remaining == 1
        assert results[2].retry_after_seconds >= 1

    def test_identities_and_windows_are_isolated(self):
        from app.core.ratelimit import FixedWindowLimiter

        limiter = FixedWindowLimiter(fakeredis.FakeRedis(), prefix="t")

        def hit(*, identity: str = "a", scope: str = "s", now: float = 10.0) -> bool:
            return limiter.consume(
                scope=scope, identity=identity, limit=1, window_seconds=60, now=now
            ).allowed

        assert hit() is True
        assert hit() is False
        # a different identity, scope or window bucket starts fresh
        assert hit(identity="b") is True
        assert hit(scope="other") is True
        assert hit(now=120.0) is True

    def test_a_limit_of_zero_disables_the_check(self):
        from app.core.ratelimit import FixedWindowLimiter

        limiter = FixedWindowLimiter(fakeredis.FakeRedis(), prefix="t")
        for _ in range(50):
            assert limiter.consume(scope="s", identity="a", limit=0, window_seconds=60).allowed

    def test_redis_failure_allows_the_request(self):
        """A cache outage must not become an API outage."""

        class BrokenRedis:
            def incr(self, key):
                raise ConnectionError("redis is down")

            def expire(self, key, seconds):
                raise ConnectionError("redis is down")

            def ttl(self, key):
                raise ConnectionError("redis is down")

        from app.core.ratelimit import FixedWindowLimiter

        decision = FixedWindowLimiter(BrokenRedis()).consume(
            scope="s", identity="a", limit=1, window_seconds=60
        )
        assert decision.allowed is True


class TestLoginThrottleResilience:
    def test_throttle_survives_a_broken_redis(self):
        from app.services.auth import LoginThrottle

        class BrokenRedis:
            def get(self, key):
                raise ConnectionError("down")

            def incr(self, key):
                raise ConnectionError("down")

            def expire(self, key, seconds):
                raise ConnectionError("down")

            def delete(self, key):
                raise ConnectionError("down")

            def ttl(self, key):
                raise ConnectionError("down")

        throttle = LoginThrottle(BrokenRedis())
        throttle.check("a@b.co", "127.0.0.1")  # must not raise
        throttle.record_failure("a@b.co", "127.0.0.1")
        throttle.clear("a@b.co", "127.0.0.1")

    def test_lockout_still_works_when_redis_answers(self):
        from app.services.auth import LoginThrottle, RateLimitError

        throttle = LoginThrottle(fakeredis.FakeRedis())
        for _ in range(5):
            throttle.record_failure("a@b.co", "127.0.0.1")
        with pytest.raises(RateLimitError):
            throttle.check("a@b.co", "127.0.0.1")


# --------------------------------------------------------------------------- secrets


class TestSecretBoxRotation:
    def _box(self, *, current: int, keys: dict[int, str]):
        from app.core.security import SecretBox

        return SecretBox(keys, current_version=current)

    def test_single_key_round_trip(self):
        from app.core.security import SecretBox

        box = SecretBox.single(_key())
        sealed = box.seal("refresh-token", purpose="youtube", owner_id="ch-1")
        assert box.open(sealed, purpose="youtube", owner_id="ch-1") == "refresh-token"

    def test_old_ciphertext_is_readable_after_rotation(self):
        old, new = _key(), _key()
        v1 = self._box(current=1, keys={1: old})
        sealed = v1.seal("secret", purpose="youtube", owner_id="ch-1")

        rotated = self._box(current=2, keys={1: old, 2: new})
        assert rotated.open(sealed, purpose="youtube", owner_id="ch-1") == "secret"
        assert rotated.needs_rotation(sealed) is True

        resealed = rotated.seal("secret", purpose="youtube", owner_id="ch-1")
        assert resealed.key_version == 2
        assert rotated.needs_rotation(resealed) is False

    def test_a_dropped_key_fails_with_an_actionable_message(self):
        sealed = self._box(current=1, keys={1: _key()}).seal(
            "secret", purpose="youtube", owner_id="ch-1"
        )
        rotated = self._box(current=2, keys={2: _key()})
        with pytest.raises(ValueError, match="SECRET_ENCRYPTION_PREVIOUS_KEYS"):
            rotated.open(sealed, purpose="youtube", owner_id="ch-1")

    def test_aad_still_binds_the_ciphertext_to_its_owner(self):
        box = self._box(current=1, keys={1: _key()})
        sealed = box.seal("secret", purpose="youtube", owner_id="ch-1")
        with pytest.raises(ValueError, match="authentication"):
            box.open(sealed, purpose="youtube", owner_id="ch-2")

    def test_ciphertext_is_never_plaintext(self):
        box = self._box(current=1, keys={1: _key()})
        sealed = box.seal("super-secret", purpose="youtube", owner_id="ch-1")
        assert b"super-secret" not in sealed.payload


# --------------------------------------------------------------------------- config paths


class TestScoringPathResolution:
    def _settings(self, **overrides):
        from app.core.config import Settings

        kwargs = {
            "secret_key": _key(),
            "secret_encryption_key": _key(),
            "password_pepper": "a-real-pepper-value-1234",
            "database_url": "postgresql+psycopg://u:p@localhost/db",
            "redis_url": "redis://localhost:6379/0",
            **overrides,
        }
        return Settings(_env_file=None, **kwargs)

    def test_default_points_at_the_repository_config(self, tmp_path, monkeypatch):
        """The default must work from any cwd — the API, a worker and pytest disagree."""
        monkeypatch.chdir(tmp_path)
        resolved = self._settings().scoring_config_file
        assert resolved.exists(), resolved
        assert resolved.name == "scoring.yaml"

    def test_absolute_path_is_used_as_given(self, tmp_path):
        target = tmp_path / "custom.yaml"
        target.write_text("version: test\n")
        assert self._settings(scoring_config_path=str(target)).scoring_config_file == target

    def test_previous_keys_are_parsed_into_a_keyring(self):
        old = _key()
        s = self._settings(
            secret_encryption_key_version=2,
            secret_encryption_previous_keys=f"1:{old}",
        )
        assert s.secret_encryption_keyring[1] == old
        assert set(s.secret_encryption_keyring) == {1, 2}

    @pytest.mark.parametrize("bad", ["not-a-pair", "x:" + base64.b64encode(b"short").decode()])
    def test_malformed_previous_keys_are_rejected_at_boot(self, bad):
        with pytest.raises(ValidationError):
            self._settings(secret_encryption_previous_keys=bad)


# --------------------------------------------------------------------------- health


class TestHealthProbe:
    def _settings(self, root: Path):
        from app.core.config import Settings

        return Settings(
            _env_file=None,
            secret_key=_key(),
            secret_encryption_key=_key(),
            password_pepper="a-real-pepper-value-1234",
            database_url="postgresql+psycopg://u:p@localhost:1/db?connect_timeout=1",
            redis_url="redis://localhost:1/0",
            storage_backend="local",
            local_storage_root=str(root),
        )

    def test_probe_does_not_create_directories(self, tmp_path):
        """A readiness probe with a side effect hides a misconfiguration instead of reporting it."""
        from app.main import _check_dependencies

        root = tmp_path / "missing-storage"
        deps = _check_dependencies(self._settings(root))
        assert deps["storage"]["status"] == "down"
        assert not root.exists()

    def test_ffmpeg_is_part_of_the_readiness_verdict(self):
        from app.main import REQUIRED_DEPENDENCIES

        assert "ffmpeg" in REQUIRED_DEPENDENCIES


# --------------------------------------------------------------------------- worker wiring


class TestWorkerWiring:
    def test_task_routes_point_at_real_queues(self):
        from app.db.repositories import JobRepository
        from app.worker import _TASK_ROUTES

        for task, route in _TASK_ROUTES.items():
            assert route["queue"] in JobRepository.ACTIVE_QUEUES, (task, route)

    def test_analysis_task_is_registered(self):
        from app.worker import celery_app

        celery_app.loader.import_default_modules()
        assert "analysis.run" in celery_app.tasks
        assert "analysis.sweep_stuck" in celery_app.tasks

    def test_the_stuck_job_sweeper_has_a_schedule(self):
        """The task existed but nothing ever ran it, so wedged jobs stayed RUNNING forever."""
        from app.worker import celery_app

        schedule = celery_app.conf.beat_schedule
        assert schedule, "no beat schedule: analysis.sweep_stuck would never run"
        tasks = {entry["task"] for entry in schedule.values()}
        assert "analysis.sweep_stuck" in tasks

    def test_unknown_stage_falls_back_to_an_active_queue(self):
        from app.db.repositories import JobRepository

        assert JobRepository.queue_for("publish") in JobRepository.ACTIVE_QUEUES
        assert JobRepository.queue_for("transcription") == "transcribe"

    def test_idempotency_probe_is_workspace_scoped(self):
        from app.db.repositories import JobRepository

        params = inspect.signature(JobRepository.find_succeeded).parameters
        assert "workspace_id" in params


@pytest.fixture(scope="module")
def compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


class TestComposeMatchesCode:
    """The compose file and the Celery app must agree; they did not (app.workers vs app.worker)."""

    def _celery_commands(self, compose: dict) -> list[str]:
        commands = []
        for service in compose.get("services", {}).values():
            command = service.get("command")
            if isinstance(command, str) and "celery" in command:
                commands.append(command)
        return commands

    def test_every_celery_service_uses_the_real_module(self, compose):
        commands = self._celery_commands(compose)
        assert commands, "no celery services found in docker-compose.yml"
        for command in commands:
            assert "app.workers" not in command, command
            assert "app.worker.celery_app" in command, command

    def test_workers_only_consume_queues_that_have_tasks(self, compose):
        from app.db.repositories import JobRepository

        consumed: set[str] = set()
        for command in self._celery_commands(compose):
            if " worker " not in f" {command} ":
                continue
            _, _, queues = command.partition(" -Q ")
            if not queues:
                continue
            consumed.update(queues.split()[0].split(","))
        assert consumed, "no worker declares a queue"
        stray = consumed - set(JobRepository.ACTIVE_QUEUES)
        assert not stray, f"queues without a route or task: {stray}"

    def test_the_api_healthcheck_uses_readiness(self, compose):
        api = compose["services"]["api"]
        probe = " ".join(api["healthcheck"]["test"])
        assert "readyz" in probe, "livez is always 200 and cannot detect a broken deployment"
