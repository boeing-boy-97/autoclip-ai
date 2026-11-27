"""FastAPI dependencies: database, current user, workspace authorization, CSRF, rate limits.

Authorization is resolved once, here, and every route declares the minimum role it needs. The
repository layer additionally requires `workspace_id` for every query, so a route that forgets to
authorize still cannot read another workspace's rows.

Two rules are enforced by construction rather than by reviewer attention:

* the minimum role is a `Literal` taken from `ROLE_RANK`, so a non-existent role (`"member"`, say)
  is a type error and can never silently degrade the check to "anything goes";
* CSRF is decided from the *credential that actually authenticated the request*, not from whatever
  headers happen to be present, so adding `Authorization: Bearer …` to a cookie-authenticated
  request cannot skip the token check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Literal

import redis
from fastapi import Cookie, Depends, Header, Request
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.core.ratelimit import FixedWindowLimiter
from app.core.security import PasswordService, password_service
from app.db.models import User, Workspace
from app.db.repositories import NotFoundError, WorkspaceRepository
from app.db.session import get_db
from app.services.auth import AuthService, LoginThrottle

Role = Literal["viewer", "editor", "admin", "owner"]
# Keyed by plain `str` so a lookup with a role read from the database type-checks; the `Role`
# literal on `workspace_access` is what keeps call sites honest.
ROLE_RANK: dict[str, int] = {"viewer": 1, "editor": 2, "admin": 3, "owner": 4}

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiError(Exception):
    """Raised by dependencies/routes; rendered as problem+json by the app's handler."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        status_code: int = 400,
        title: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.title = title or code.replace("_", " ").capitalize()
        self.headers = headers or {}


@dataclass(slots=True)
class AuthContext:
    user: User
    session_token: str
    session_row: object
    # Which credential authenticated this request. Drives the CSRF decision in `require_csrf`.
    via: Literal["cookie", "bearer"] = "cookie"


@dataclass(slots=True)
class WorkspaceContext:
    workspace: Workspace
    role: str
    user: User


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]
DbDep = Annotated[DbSession, Depends(get_db)]


def get_redis(settings: SettingsDep) -> Iterator[redis.Redis]:
    client = redis.from_url(settings.redis_url, decode_responses=False)
    try:
        yield client
    finally:
        client.close()


RedisDep = Annotated[redis.Redis, Depends(get_redis)]


def get_passwords(settings: SettingsDep) -> PasswordService:
    return password_service(settings)


PasswordsDep = Annotated[PasswordService, Depends(get_passwords)]


def get_auth_service(
    db: DbDep,
    settings: SettingsDep,
    passwords: PasswordsDep,
    rds: RedisDep,
) -> AuthService:
    throttle = LoginThrottle(rds) if settings.rate_limit_per_minute else None
    return AuthService(db, settings, passwords, throttle=throttle)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def current_auth(
    auth: AuthServiceDep,
    ac_session: Annotated[str | None, Cookie(alias="ac_session")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """Authenticate via session cookie or bearer token.

    Bearer is supported for tests/automation. Both resolve to the same opaque-session mechanism;
    there is no second, weaker auth path. When both are sent the *cookie* wins, and the context
    records that — a request authenticated by an ambient credential must pass CSRF regardless of
    any extra header it carries.
    """
    token: str | None = None
    via: Literal["cookie", "bearer"] = "cookie"
    if ac_session:
        token, via = ac_session, "cookie"
    elif authorization and authorization.lower().startswith("bearer "):
        token, via = authorization[7:].strip(), "bearer"
    if not token:
        raise ApiError(
            "unauthenticated",
            "authentication required",
            status_code=401,
            headers={"WWW-Authenticate": "Cookie, Bearer"},
        )
    resolved = auth.resolve_session(token)
    if resolved is None:
        raise ApiError(
            "unauthenticated",
            "session is invalid or expired",
            status_code=401,
            headers={"WWW-Authenticate": "Cookie, Bearer"},
        )
    user, row = resolved
    return AuthContext(user=user, session_token=token, session_row=row, via=via)


AuthDep = Annotated[AuthContext, Depends(current_auth)]


def require_csrf(
    request: Request,
    auth_ctx: AuthDep,
    auth: AuthServiceDep,
    settings: SettingsDep,
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> None:
    """Enforce double-submit CSRF on mutating requests.

    The decision follows the credential, not the headers:

    * a request authenticated by a **bearer** token, and carrying no session cookie, is exempt —
      there is no ambient credential for another origin to borrow;
    * anything else (cookie-authenticated, or a bearer header sent *alongside* a session cookie)
      must present a token that matches the digest stored with the session.

    The previous implementation exempted any request with a bearer header, which let a cross-site
    request attach `Authorization: Bearer x` to a cookie-authenticated call and skip CSRF entirely.
    """
    if request.method in SAFE_METHODS or not settings.csrf_enabled:
        return
    cookie_present = bool(request.cookies.get(settings.session_cookie_name))
    if auth_ctx.via == "bearer" and not cookie_present:
        return
    if not x_csrf_token:
        raise ApiError(
            "forbidden", "CSRF token required for cookie-authenticated requests", status_code=403
        )
    if not auth.verify_csrf(auth_ctx.session_row, x_csrf_token):  # type: ignore[arg-type]
        raise ApiError("forbidden", "CSRF token invalid or missing", status_code=403)


CSRFDep = Annotated[None, Depends(require_csrf)]


# --------------------------------------------------------------------------- rate limiting


class _RateLimit:
    """Base for the two identity strategies; subclasses supply the key and the limit."""

    scope = "request"
    limit_setting = "rate_limit_per_minute"
    window_seconds = 60

    def _enforce(self, rds: redis.Redis, settings: Settings, identity: str) -> None:
        limit = int(getattr(settings, self.limit_setting, 0))
        decision = FixedWindowLimiter(rds).consume(
            scope=self.scope,
            identity=identity,
            limit=limit,
            window_seconds=self.window_seconds,
        )
        if not decision.allowed:
            raise ApiError(
                "rate_limited",
                f"too many requests; retry in {decision.retry_after_seconds} seconds",
                status_code=429,
                headers=decision.as_headers(),
            )


class UserRateLimit(_RateLimit):
    """Limit a mutating route per authenticated user (survives IP changes, unlike IP limits)."""

    def __init__(self, scope: str, *, limit_setting: str, window_seconds: int) -> None:
        self.scope = scope
        self.limit_setting = limit_setting
        self.window_seconds = window_seconds

    def __call__(self, auth_ctx: AuthDep, rds: RedisDep, settings: SettingsDep) -> None:
        self._enforce(rds, settings, str(auth_ctx.user.id))


class IpRateLimit(_RateLimit):
    """Limit an unauthenticated route per client IP (login, registration)."""

    def __init__(self, scope: str, *, limit_setting: str, window_seconds: int) -> None:
        self.scope = scope
        self.limit_setting = limit_setting
        self.window_seconds = window_seconds

    def __call__(self, request: Request, rds: RedisDep, settings: SettingsDep) -> None:
        identity = request.client.host if request.client else "unknown"
        self._enforce(rds, settings, identity)


# --------------------------------------------------------------------------- authorization


def role_satisfies(role: str, min_role: Role) -> bool:
    """Whether `role` meets `min_role`.

    An unknown minimum role is a programming error and raises rather than resolving to rank 0
    (which would silently let every member through).
    """
    if min_role not in ROLE_RANK:
        raise ValueError(f"unknown minimum role {min_role!r}; expected one of {sorted(ROLE_RANK)}")
    return ROLE_RANK.get(role, 0) >= ROLE_RANK[min_role]


def workspace_access(
    workspace_id: uuid.UUID,
    auth_ctx: AuthDep,
    db: DbDep,
    min_role: Role = "viewer",
) -> WorkspaceContext:
    """Resolve membership and enforce the minimum role.

    Non-members get 404 (not 403) so the API does not confirm that a workspace exists. A member
    whose role cannot be resolved is denied rather than defaulted to `viewer`: default-allow is
    how an authorization bug becomes a breach.
    """
    repo = WorkspaceRepository(db)
    workspace = repo.get_for_user(workspace_id=workspace_id, user_id=auth_ctx.user.id)
    if workspace is None:
        raise NotFoundError("workspace", workspace_id)
    role = repo.role_of(workspace_id=workspace_id, user_id=auth_ctx.user.id)
    if not role:
        raise ApiError("forbidden", "workspace membership could not be resolved", status_code=403)
    if not role_satisfies(role, min_role):
        raise ApiError(
            "forbidden",
            f"this action requires the '{min_role}' role (you are '{role}')",
            status_code=403,
        )
    return WorkspaceContext(workspace=workspace, role=role, user=auth_ctx.user)
