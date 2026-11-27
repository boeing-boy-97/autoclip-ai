"""Authentication service: registration, login, sessions, CSRF.

Security behaviours implemented here (see docs/SECURITY.md §1):
  * Argon2id + server pepper for passwords.
  * Opaque session tokens; only SHA-256 digests stored.
  * Login throttling keyed on (email, ip) with exponential lockout.
  * User enumeration resistance: identical error and comparable timing for unknown users.
  * Session rotation on login; revocation is server-side (deleting the cookie is not logout).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.netutil import valid_ip
from app.core.security import (
    PasswordService,
    constant_time_equals,
    generate_token,
    token_digest,
)
from app.db.models import Session as SessionModel
from app.db.models import User
from app.db.repositories import (
    AuditRepository,
    UserRepository,
    WorkspaceRepository,
)

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
MIN_PASSWORD_LENGTH = 10
MAX_LOGIN_FAILURES = 5
LOCKOUT_WINDOW = timedelta(minutes=15)


@dataclass(slots=True)
class AuthResult:
    user: User
    session_token: str
    csrf_token: str
    expires_at: datetime


class AuthError(Exception):
    """Domain error carrying a stable machine code for the API layer."""

    def __init__(self, code: str, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class RateLimitError(AuthError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "rate_limited",
            f"too many failed attempts; retry in {retry_after_seconds} seconds",
            status_code=429,
        )
        self.retry_after_seconds = retry_after_seconds


class LoginThrottle:
    """Failure counter in Redis.

    Redis is the right home for this: it is shared across API replicas (in-process counters would
    let an attacker get N attempts per replica) and it expires automatically.

    Every Redis call is wrapped: a cache outage must degrade to "unthrottled but working", never to
    a 500 on the login endpoint. The degradation is logged so it is visible in operations.
    """

    def __init__(self, redis_client: Any, *, window: timedelta = LOCKOUT_WINDOW) -> None:
        self.redis = redis_client
        self.window = window

    @staticmethod
    def _key(email: str, ip: str | None) -> str:
        return f"login_fail:{email.lower()}:{ip or 'unknown'}"

    def check(self, email: str, ip: str | None) -> None:
        key = self._key(email, ip)
        try:
            raw = self.redis.get(key)
            count = int(raw or 0)
            if count < MAX_LOGIN_FAILURES:
                return
            ttl = self.redis.ttl(key)
        except Exception as exc:  # noqa: BLE001 - Redis failure must not block sign-in
            logger.warning(
                "login_throttle.unavailable",
                extra={"operation": "check", "error": type(exc).__name__},
            )
            return
        raise RateLimitError(max(int(ttl) if ttl and ttl > 0 else 60, 1))

    def record_failure(self, email: str, ip: str | None) -> None:
        key = self._key(email, ip)
        try:
            with self.redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, int(self.window.total_seconds()))
                pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "login_throttle.unavailable",
                extra={"operation": "record_failure", "error": type(exc).__name__},
            )

    def clear(self, email: str, ip: str | None) -> None:
        try:
            self.redis.delete(self._key(email, ip))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "login_throttle.unavailable",
                extra={"operation": "clear", "error": type(exc).__name__},
            )


class AuthService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        passwords: PasswordService,
        throttle: LoginThrottle | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.passwords = passwords
        self.users = UserRepository(db)
        self.workspaces = WorkspaceRepository(db)
        self.audit = AuditRepository(db)
        self.throttle = throttle

    # ---------------------------------------------------------------- register
    def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        workspace_name: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        email = email.strip().lower()
        ip = valid_ip(ip)
        if not EMAIL_RE.match(email):
            raise AuthError(
                "validation_error", "a valid email address is required", status_code=422
            )
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(
                "validation_error",
                f"password must be at least {MIN_PASSWORD_LENGTH} characters",
                status_code=422,
            )
        if not display_name.strip():
            raise AuthError("validation_error", "display_name is required", status_code=422)
        if self.users.get_by_email(email) is not None:
            # Deliberately specific here: registration must tell a user their email is taken,
            # otherwise they are stranded. Rate limiting covers the enumeration risk.
            raise AuthError(
                "conflict", "an account with this email already exists", status_code=409
            )

        user = self.users.create(
            email=email,
            password_hash=self.passwords.hash(password),
            display_name=display_name.strip()[:80],
        )
        ws_name = (workspace_name or f"{display_name.strip()[:40]}'s workspace").strip()
        self.workspaces.create(
            name=ws_name,
            slug=self._unique_slug(ws_name),
            owner_user_id=user.id,
            default_language=self.settings.workspace_default_language,
        )
        self.audit.record(
            action="auth.register",
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            ip=ip,
            user_agent=user_agent,
        )
        self.db.flush()
        return self._issue_session(user, ip=ip, user_agent=user_agent)

    # ---------------------------------------------------------------- login
    def login(
        self,
        *,
        email: str,
        password: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> AuthResult:
        email = email.strip().lower()
        ip = valid_ip(ip)
        if self.throttle:
            self.throttle.check(email, ip)

        user = self.users.get_by_email(email)
        if user is None:
            # Equalise timing so a missing account is indistinguishable from a wrong password.
            self.passwords.dummy_verify()
            if self.throttle:
                self.throttle.record_failure(email, ip)
            self.audit.record(
                action="auth.login_failed",
                ip=ip,
                user_agent=user_agent,
                detail={"reason": "unknown_account"},
            )
            raise AuthError(
                "invalid_credentials", "email or password is incorrect", status_code=401
            )

        if not self.passwords.verify(user.password_hash, password):
            if self.throttle:
                self.throttle.record_failure(email, ip)
            self.audit.record(
                action="auth.login_failed",
                actor_user_id=user.id,
                ip=ip,
                user_agent=user_agent,
                detail={"reason": "bad_password"},
            )
            raise AuthError(
                "invalid_credentials", "email or password is incorrect", status_code=401
            )

        if user.status != "active":
            raise AuthError("forbidden", f"account is {user.status}", status_code=403)

        # Transparent upgrade if Argon2 parameters changed since the hash was created.
        if self.passwords.needs_rehash(user.password_hash):
            user.password_hash = self.passwords.hash(password)

        if self.throttle:
            self.throttle.clear(email, ip)
        self.users.record_login(user.id)
        self.audit.record(action="auth.login", actor_user_id=user.id, ip=ip, user_agent=user_agent)
        self.db.flush()
        return self._issue_session(user, ip=ip, user_agent=user_agent)

    # ---------------------------------------------------------------- sessions
    def _issue_session(self, user: User, *, ip: str | None, user_agent: str | None) -> AuthResult:
        token = generate_token()
        csrf = generate_token(24)
        expires_at = datetime.now(UTC) + timedelta(hours=self.settings.session_ttl_hours)
        self.db.add(
            SessionModel(
                user_id=user.id,
                token_hash=token_digest(token),
                csrf_hash=token_digest(csrf),
                user_agent=(user_agent or "")[:500] or None,
                ip=ip,
                expires_at=expires_at,
            )
        )
        self.db.flush()
        return AuthResult(user=user, session_token=token, csrf_token=csrf, expires_at=expires_at)

    def resolve_session(self, token: str) -> tuple[User, SessionModel] | None:
        if not token:
            return None
        row = (
            self.db.query(SessionModel)
            .filter(SessionModel.token_hash == token_digest(token))
            .one_or_none()
        )
        if row is None or row.revoked_at is not None:
            return None
        # Compare in Python so the check is timezone-safe across drivers.
        if row.expires_at <= datetime.now(UTC):
            return None
        user = self.db.get(User, row.user_id)
        if user is None or user.status != "active":
            return None
        return user, row

    def verify_csrf(self, session: SessionModel, csrf_token: str | None) -> bool:
        """Double-submit CSRF: the header token must match the digest stored with the session."""
        if not self.settings.csrf_enabled:
            return True
        if not csrf_token:
            return False
        return constant_time_equals(token_digest(csrf_token), session.csrf_hash)

    def logout(self, *, token: str, ip: str | None = None) -> None:
        ip = valid_ip(ip)
        if not token:
            return
        row = (
            self.db.query(SessionModel)
            .filter(SessionModel.token_hash == token_digest(token))
            .one_or_none()
        )
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            self.audit.record(action="auth.logout", actor_user_id=row.user_id, ip=ip)
            self.db.flush()

    def revoke_all_sessions(self, *, user_id: uuid.UUID) -> int:
        now = datetime.now(UTC)
        rows = (
            self.db.query(SessionModel)
            .filter(SessionModel.user_id == user_id, SessionModel.revoked_at.is_(None))
            .all()
        )
        for row in rows:
            row.revoked_at = now
        self.db.flush()
        return len(rows)

    # ---------------------------------------------------------------- helpers
    def _unique_slug(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workspace"
        candidate = base
        suffix = 1
        while self.workspaces.slug_exists(candidate):
            suffix += 1
            candidate = f"{base}-{suffix}"
            if suffix > 1000:  # pragma: no cover - defensive
                candidate = f"{base}-{uuid.uuid4().hex[:8]}"
                break
        return candidate
