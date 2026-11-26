"""FastAPI dependencies: database, current user, workspace authorization, CSRF.

Authorization is resolved once, here, and every route declares the minimum role it needs. The
repository layer additionally requires `workspace_id` for every query, so a route that forgets to
authorize still cannot read another workspace's rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

import redis
from fastapi import Cookie, Depends, Header, Request
from sqlalchemy.orm import Session as DbSession

from app.core.config import Settings, get_settings
from app.core.security import PasswordService, password_service
from app.db.models import User, Workspace
from app.db.repositories import NotFoundError, WorkspaceRepository
from app.db.session import get_db
from app.services.auth import AuthService, LoginThrottle

ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3, "owner": 4}


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
    settings: SettingsDep,  # noqa: ARG001 - kept so tests can override settings via DI
    ac_session: Annotated[str | None, Cookie(alias="ac_session")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    """Authenticate via session cookie or bearer token.

    Bearer is supported for tests/automation. Both resolve to the same opaque-session mechanism;
    there is no second, weaker auth path.
    """
    token = ac_session
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise ApiError(
            "unauthenticated", "authentication required", status_code=401,
            headers={"WWW-Authenticate": "Cookie, Bearer"},
        )
    resolved = auth.resolve_session(token)
    if resolved is None:
        raise ApiError(
            "unauthenticated", "session is invalid or expired", status_code=401,
            headers={"WWW-Authenticate": "Cookie, Bearer"},
        )
    user, row = resolved
    return AuthContext(user=user, session_token=token, session_row=row)


AuthDep = Annotated[AuthContext, Depends(current_auth)]


def require_csrf(
    request: Request,
    auth_ctx: AuthDep,
    auth: AuthServiceDep,
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> None:
    """Enforce double-submit CSRF on mutating requests.

    Bearer-authenticated calls are exempt: they do not rely on ambient cookies, so they are not
    vulnerable to CSRF by construction.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if x_csrf_token and auth_ctx.session_token:
        if not auth.verify_csrf(auth_ctx.session_row, x_csrf_token):  # type: ignore[arg-type]
            raise ApiError("forbidden", "CSRF token invalid or missing", status_code=403)
        return
    authorization = request.headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        return
    # Only enforce when a cookie session is actually in play.
    if request.cookies.get("ac_session") and get_settings().csrf_enabled:
        raise ApiError(
            "forbidden", "CSRF token required for cookie-authenticated requests", status_code=403
        )


CSRFDep = Annotated[None, Depends(require_csrf)]


def workspace_access(
    workspace_id: uuid.UUID,
    auth_ctx: AuthDep,
    db: DbDep,
    min_role: str = "viewer",
) -> WorkspaceContext:
    """Resolve membership and enforce the minimum role.

    Non-members get 404 (not 403) so the API does not confirm that a workspace exists.
    """
    repo = WorkspaceRepository(db)
    workspace = repo.get_for_user(workspace_id=workspace_id, user_id=auth_ctx.user.id)
    if workspace is None:
        raise NotFoundError("workspace", workspace_id)
    role = repo.role_of(workspace_id=workspace_id, user_id=auth_ctx.user.id) or "viewer"
    if ROLE_RANK.get(role, 0) < ROLE_RANK.get(min_role, 0):
        raise ApiError(
            "forbidden",
            f"this action requires the '{min_role}' role (you are '{role}')",
            status_code=403,
        )
    return WorkspaceContext(workspace=workspace, role=role, user=auth_ctx.user)
