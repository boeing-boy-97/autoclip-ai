"""Authentication endpoints (docs/openapi.json: /auth/*)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import ApiError, AuthDep, AuthServiceDep, CSRFDep, SettingsDep
from app.api.schemas import UserOut, workspace_memberships
from app.db.repositories import WorkspaceRepository
from app.services.auth import AuthError

router = APIRouter(prefix="/auth", tags=["Auth"])

SESSION_COOKIE = "ac_session"
# The session row stores only a digest of the CSRF token, so the server cannot re-issue a token it
# has already forgotten. A second, *readable* cookie carries the token itself, which makes the
# double-submit pattern survive a page reload. The cookie is not an authority: it is never trusted
# on its own, it is only ever compared against the header and against the stored digest.
CSRF_COOKIE = "ac_csrf"


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    workspace_name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class SessionResponse(BaseModel):
    user: UserOut
    csrf_token: str
    expires_at: str


def _set_session_cookie(response: Response, settings, token: str) -> None:  # noqa: ANN001
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_hours * 3600,
    )


def _set_csrf_cookie(response: Response, settings, token: str) -> None:  # noqa: ANN001
    """Publish the CSRF token so a reloaded page can keep sending it.

    Deliberately not HttpOnly: the browser must be able to read it. That is safe because the token
    grants nothing by itself — a request without it fails, and a request with it from another origin
    cannot read this value under the same-origin policy.
    """
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        httponly=False,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_hours * 3600,
    )


def _session_payload(auth: AuthServiceDep, result) -> SessionResponse:  # noqa: ANN001
    memberships = workspace_memberships(
        WorkspaceRepository(auth.db).list_for_user(user_id=result.user.id)
    )
    return SessionResponse(
        user=UserOut.from_model(result.user, memberships),
        csrf_token=result.csrf_token,
        expires_at=result.expires_at.isoformat(),
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionResponse,
    summary="Register",
)
def register_user(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    auth: AuthServiceDep,
    settings: SettingsDep,
) -> SessionResponse:
    """Create an account and its first workspace, then start a session."""
    try:
        result = auth.register(
            email=str(payload.email),
            password=payload.password,
            display_name=payload.display_name,
            workspace_name=payload.workspace_name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        auth.db.commit()  # persist the audit entry even on failure
        raise ApiError(exc.code, exc.detail, status_code=exc.status_code) from exc

    auth.db.commit()
    _set_session_cookie(response, settings, result.session_token)
    _set_csrf_cookie(response, settings, result.csrf_token)
    return _session_payload(auth, result)


@router.post("/login", response_model=SessionResponse, summary="Login")
def login_user(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth: AuthServiceDep,
    settings: SettingsDep,
) -> SessionResponse:
    try:
        result = auth.login(
            email=str(payload.email),
            password=payload.password,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        auth.db.commit()  # persist the failed-login audit entry
        headers: dict[str, str] = {}
        retry_after = getattr(exc, "retry_after_seconds", None)
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        raise ApiError(exc.code, exc.detail, status_code=exc.status_code, headers=headers) from exc

    auth.db.commit()
    _set_session_cookie(response, settings, result.session_token)
    _set_csrf_cookie(response, settings, result.csrf_token)
    return _session_payload(auth, result)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Logout")
def logout_user(
    request: Request,
    response: Response,
    auth: AuthServiceDep,
    auth_ctx: AuthDep,
    _csrf: CSRFDep,
) -> Response:
    """Revoke the session server-side. Clearing the cookie alone is not logout."""
    auth.logout(token=auth_ctx.session_token, ip=request.client.host if request.client else None)
    auth.db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserOut, summary="Get Me")
def get_me(
    request: Request,
    response: Response,
    auth_ctx: AuthDep,
    auth: AuthServiceDep,
    settings: SettingsDep,
) -> UserOut:
    """The signed-in user, refreshing the readable CSRF cookie on the way past.

    `/auth/me` is what a reloaded page calls to find out who it is. The server keeps only a digest
    of the CSRF token, so it cannot hand the token back — the readable cookie carries it instead,
    and this refresh keeps that cookie's lifetime in step with the session's.
    """
    memberships = workspace_memberships(
        WorkspaceRepository(auth.db).list_for_user(user_id=auth_ctx.user.id)
    )
    existing = request.cookies.get(CSRF_COOKIE)
    if existing and settings.csrf_enabled:
        _set_csrf_cookie(response, settings, existing)
    return UserOut.from_model(auth_ctx.user, memberships)
