"""FastAPI application factory.

Implemented here: settings fail-fast, structured logging, RFC 9457 problem+json errors,
dependency-aware health/readiness reporting, CORS, trusted-host enforcement, security headers,
and the API v1 routers (auth, projects, sources, analysis, jobs, clips, dashboard).

Health has three levels and they mean different things:

* `/health/livez` — the process is up. Never touches a dependency, so it must not be used as a
  readiness signal (a container with a dead database is still "live").
* `/health/readyz` — every *required* dependency answered, FFmpeg included. This is what container
  health checks and load balancers should poll.
* `/health` — the same probe plus the integration inventory, which is development/diagnostic
  output and is therefore omitted in production.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import redis
import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.core import preflight
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

PROBLEM_CT = "application/problem+json"

# Dependencies without which the product cannot do its job. FFmpeg belongs here: a deployment
# that cannot probe or render would accept uploads and fail them all.
REQUIRED_DEPENDENCIES = ("database", "redis", "storage", "ffmpeg")


def problem(
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str | None = None,
    trace_id: str | None = None,
    fields: list[dict[str, str]] | None = None,
) -> JSONResponse:
    """RFC 9457 problem document. `code` is the stable machine contract."""
    body: dict[str, Any] = {
        "type": f"https://docs.autoclip.ai/errors/{code}",
        "title": title,
        "status": status_code,
        "code": code,
        "trace_id": trace_id or "unavailable",
    }
    if detail:
        body["detail"] = detail
    if fields:
        body["fields"] = fields
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CT)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log = get_logger(__name__)
    s = get_settings()
    configure_logging(level=s.log_level, fmt=s.log_format)

    # Deployment self-check before the first request is served. Warnings are logged and surfaced by
    # /health/readyz; a fatal issue refuses the start, because accepting traffic that cannot upload
    # or render only converts a deploy-time misconfiguration into a user-visible outage.
    issues = preflight.check(s)
    role = preflight.process_role()
    for issue in issues:
        (log.error if issue.level == "error" else log.warning)(
            "app.preflight", process=role, code=issue.code, detail=issue.detail
        )
    blocking = preflight.fatal(issues)
    if blocking:
        joined = "; ".join(f"{issue.code}: {issue.detail}" for issue in blocking)
        raise RuntimeError(f"refusing to start: {joined}")

    log.info(
        "app.started",
        version=__version__,
        environment=s.environment,
        integrations={k: v["configured"] for k, v in s.integration_status().items()},
    )
    unconfigured = [k for k, v in s.integration_status().items() if not v["configured"]]
    if unconfigured:
        # Explicit, not fatal: the product runs, but says what is missing.
        log.warning("app.integrations_not_configured", integrations=unconfigured)
    yield
    log.info("app.stopped")


_UUID_IN_PATH = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _redact_path(path: str) -> str:
    """Replace path identifiers with `{id}` so logs carry the route, not the tenant's ids."""
    return _UUID_IN_PATH.sub("{id}", path)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="AutoClipper AI API",
        version=__version__,
        description=(
            "AI video clipping platform: authentication, projects, source ingestion, "
            "transcript analysis, clip generation and 9:16 rendering. Publishing is not "
            "implemented; endpoints that are specified but unbuilt are documented as such in "
            "docs/openapi.json."
        ),
        lifespan=lifespan,
        docs_url="/docs" if s.docs_enabled else None,
        redoc_url="/redoc" if s.docs_enabled else None,
        openapi_url="/openapi.json" if s.docs_enabled else None,
    )

    if s.trusted_host_list != ["*"]:
        # Only enforced when configured: the default keeps local dev and TestClient working,
        # while a deployment that sets TRUSTED_HOSTS gets Host-header validation.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=s.trusted_host_list)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
        expose_headers=["X-Trace-Id", "X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        """Cheap, unconditional hardening. None of these change API behaviour."""
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if s.session_cookie_secure:
            # Only advertise HSTS where the deployment is actually TLS-terminated.
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.middleware("http")
    async def trace_and_log(request: Request, call_next: Any) -> Any:
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        # Identifiers are redacted before anything is logged or bound into the log context: a
        # project or clip id in a log line is a tenant-scoped identifier, not a route.
        route = _redact_path(request.url.path)
        request.state.trace_id = trace_id
        with structlog.contextvars.bound_contextvars(trace_id=trace_id, path=route):
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
        get_logger("http").info(
            "http.request",
            method=request.method,
            route=route,
            status=response.status_code,
        )
        return response

    http_code_map = {
        400: ("bad_request", "Bad request"),
        401: ("unauthenticated", "Authentication required"),
        403: ("forbidden", "Forbidden"),
        404: ("not_found", "Resource not found"),
        405: ("method_not_allowed", "Method not allowed"),
        409: ("conflict", "Conflict"),
        413: ("payload_too_large", "Upload too large"),
        415: ("unsupported_media", "Unsupported media type"),
        429: ("rate_limited", "Too many requests"),
        501: ("not_implemented", "Not implemented"),
    }

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Framework-raised errors must use the same problem+json contract as our own.

        Without this, FastAPI's default handlers return {"detail": ...} as application/json,
        which silently breaks every documented error contract the frontend relies on.
        """
        code, title = http_code_map.get(exc.status_code, ("http_error", "Request failed"))
        return problem(
            status_code=exc.status_code,
            code=code,
            title=title,
            detail=str(exc.detail) if exc.detail and exc.status_code != 404 else None,
            trace_id=getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id"),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {
                "path": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
                "message": err.get("msg", "invalid"),
                "code": err.get("type", "validation_error"),
            }
            for err in exc.errors()
        ]
        return problem(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="validation_error",
            title="Request validation failed",
            detail="One or more fields are invalid.",
            trace_id=getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id"),
            fields=fields,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, _exc: Exception) -> JSONResponse:
        get_logger(__name__).exception("http.unhandled_error", route=request.url.path)
        # Internal detail is withheld; trace_id lets support correlate.
        return problem(
            status_code=500,
            code="internal_error",
            title="Internal server error",
            detail="An unexpected error occurred. Quote the trace id to support.",
            trace_id=getattr(request.state, "trace_id", None) or request.headers.get("X-Trace-Id"),
        )

    # ------------------------------------------------------------ API v1 routers
    from app.api.deps import ApiError

    # Routers are imported inside the factory so environment-dependent settings are read
    # first; noqa: PLC0415 is deliberate for the whole block.
    from app.api.v1.analysis import router as analysis_router
    from app.api.v1.auth import router as auth_router
    from app.api.v1.clips import router as clips_router
    from app.api.v1.jobs import router as jobs_router
    from app.api.v1.projects import router as projects_router
    from app.api.v1.sources import router as sources_router
    from app.api.v1.system import router as system_router

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        """Domain errors carry a stable `code`; the UI switches on it, not on prose."""
        response = problem(
            status_code=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            trace_id=getattr(request.state, "trace_id", None),
        )
        # Headers such as Retry-After are part of the contract (429 semantics).
        for name, value in exc.headers.items():
            response.headers[name] = value
        return response

    # ------------------------------------------------------------ repository errors
    # Repository errors are domain errors. Handlers translate them, but a dependency (for
    # example workspace membership resolution) can raise before the handler ever runs. Without
    # these handlers such a request became a 500 instead of the documented 404/409 — precisely
    # the path an attacker hits when probing another tenant's workspace ids.
    from app.db.repositories import ConflictError as _RepoConflict
    from app.db.repositories import NotFoundError as _RepoNotFound

    @app.exception_handler(_RepoNotFound)
    async def _repo_not_found_handler(request: Request, exc: _RepoNotFound) -> JSONResponse:
        code = f"{getattr(exc, 'resource', 'resource')}_not_found"
        return problem(
            status_code=status.HTTP_404_NOT_FOUND,
            code=code,
            title="Not found",
            # No identifier in the message: echoing it back would confirm whether a guessed id
            # exists in another tenant's workspace.
            detail=f"{getattr(exc, 'resource', 'resource')} not found",
            trace_id=getattr(request.state, "trace_id", None),
        )

    @app.exception_handler(_RepoConflict)
    async def _repo_conflict_handler(request: Request, exc: _RepoConflict) -> JSONResponse:
        return problem(
            status_code=status.HTTP_409_CONFLICT,
            code=getattr(exc, "code", None) or "conflict",
            title="Conflict",
            detail=str(exc),
            trace_id=getattr(request.state, "trace_id", None),
        )

    app.include_router(auth_router, prefix=s.api_prefix)
    app.include_router(system_router, prefix=s.api_prefix)
    app.include_router(projects_router, prefix=s.api_prefix)
    app.include_router(sources_router, prefix=s.api_prefix)
    app.include_router(analysis_router, prefix=s.api_prefix)
    app.include_router(jobs_router, prefix=s.api_prefix)
    app.include_router(clips_router, prefix=s.api_prefix)

    # ------------------------------------------------------------ health
    @app.get("/api/v1/health/livez", tags=["System"], summary="Liveness probe")
    def livez() -> dict[str, str]:
        """Process is up. Must not depend on external services."""
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/health/readyz", tags=["System"], summary="Readiness probe")
    def readyz() -> JSONResponse:
        """Every *required* dependency answers. Returns 503 otherwise.

        FFmpeg is in the required set: without it uploads, analysis and rendering all fail, so a
        deployment that cannot render must not be routed traffic.
        """
        s = get_settings()
        deps = _check_dependencies(s)
        required_ok = all(deps[name]["status"] == "ok" for name in REQUIRED_DEPENDENCIES)
        return JSONResponse(
            status_code=200 if required_ok else 503,
            content={"status": "ok" if required_ok else "degraded", "dependencies": deps},
        )

    @app.get("/api/v1/health", tags=["System"], summary="Health and integration report")
    def health() -> JSONResponse:
        """Readiness plus the integration inventory.

        The inventory says which third-party providers are configured, which is useful in
        development and is free reconnaissance in production — so it is omitted when
        `ENVIRONMENT=production`.
        """
        s = get_settings()
        deps = _check_dependencies(s)
        required_ok = all(deps[name]["status"] == "ok" for name in REQUIRED_DEPENDENCIES)
        payload: dict[str, Any] = {
            "status": "ok" if required_ok else "degraded",
            "version": __version__,
            "dependencies": deps,
        }
        if s.environment != "production":
            payload["environment"] = s.environment
            payload["integrations"] = s.integration_status()
            payload["notes"] = [
                (
                    "Optional integrations reporting configured=false are disabled by design, "
                    "not failing. Publishing requires YOUTUBE_CLIENT_ID/SECRET."
                ),
                (
                    "AI analysis falls back to deterministic heuristics when no provider is set; "
                    "output quality is lower and labelled as heuristic in the UI."
                ),
            ]
        return JSONResponse(status_code=200 if required_ok else 503, content=payload)

    return app


def _check_dependencies(s: Any) -> dict[str, dict[str, str]]:
    """Probe each dependency. Honest results only — no optimistic defaults.

    Deliberately synchronous: the probes below (psycopg, redis-py) are blocking clients, so this
    function is called from sync route handlers and therefore runs in FastAPI's threadpool. An
    `async def` version called them directly and blocked the event loop for the duration of a
    connection timeout on every probe.
    """
    deps: dict[str, dict[str, str]] = {}

    # database
    try:
        dsn = s.database_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        deps["database"] = {"status": "ok", "detail": "reachable"}
    except Exception as exc:  # noqa: BLE001
        deps["database"] = {"status": "down", "detail": type(exc).__name__}

    # redis
    client = None
    try:
        client = redis.from_url(
            s.redis_url,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        client.ping()
        deps["redis"] = {"status": "ok", "detail": "reachable"}
    except Exception as exc:  # noqa: BLE001
        deps["redis"] = {"status": "down", "detail": type(exc).__name__}
    finally:
        if client is not None:
            # A probe that leaks a connection per call exhausts the server's client limit.
            with contextlib.suppress(Exception):
                client.close()

    # storage
    if s.storage_backend == "local":
        root = Path(s.local_storage_root)
        try:
            # No mkdir: a health check must not create directories. If the root is absent, the
            # deployment is misconfigured and the finding is the point.
            exists = root.exists()
            ok = exists and os.access(root, os.W_OK)
            free_gb = shutil.disk_usage(root).free / 1e9 if exists else 0.0
            deps["storage"] = {
                "status": "ok" if ok else "down",
                "detail": (
                    f"local root exists={exists}, writable={ok}, free={free_gb:.1f}GB"
                    + ("" if free_gb >= s.min_free_disk_gb else " (below MIN_FREE_DISK_GB)")
                ),
            }
        except Exception as exc:  # noqa: BLE001
            deps["storage"] = {"status": "down", "detail": type(exc).__name__}
    else:
        deps["storage"] = {
            "status": "ok" if s.storage_configured else "down",
            "detail": "s3 configured" if s.storage_configured else "S3 credentials missing",
        }

    # ffmpeg / ffprobe (required for any video work)
    ffmpeg = shutil.which(s.ffmpeg_bin)
    ffprobe = shutil.which(s.ffprobe_bin)
    deps["ffmpeg"] = {
        "status": "ok" if (ffmpeg and ffprobe) else "down",
        "detail": f"ffmpeg={ffmpeg or 'MISSING'}, ffprobe={ffprobe or 'MISSING'}",
    }

    return deps


app = create_app()
