"""FastAPI application factory — Phase 0 infrastructure only.

Implemented here: settings fail-fast, structured logging, RFC 9457 problem+json errors,
dependency-aware health/integration reporting, CORS.
Not implemented here (Phase 1+): auth, projects, uploads, analysis, clips, publishing.
Routes for those are specified in docs/openapi.json and deliberately return 501 until built,
so the contract is explicit rather than silently absent.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

PROBLEM_CT = "application/problem+json"


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


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="AutoClipper AI API",
        version=f"{__version__}-phase0",
        description=(
            "AI video clipping and YouTube publishing platform. Phase 0: infrastructure "
            "and health. "
            "Domain endpoints are specified in docs/openapi.json and land in Phases 1-6."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
        expose_headers=["X-Trace-Id"],
    )

    @app.middleware("http")
    async def trace_and_log(request: Request, call_next: Any) -> Any:
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        # Route template, not raw path, to avoid leaking identifiers into logs.
        route = request.url.path
        # Expose the trace id to exception handlers via request.state as well as the header,
        # so client-supplied ids are not required for correlation.
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

    # ------------------------------------------------------------ health
    @app.get("/api/v1/health/livez", tags=["System"], summary="Liveness probe")
    async def livez() -> dict[str, str]:
        """Process is up. Must not depend on external services."""
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/health/readyz", tags=["System"], summary="Readiness probe")
    async def readyz() -> JSONResponse:
        """Dependencies are reachable. Returns 503 when a required dependency is down."""
        s = get_settings()
        deps = await _check_dependencies(s)
        required_ok = all(deps[k]["status"] == "ok" for k in ("database", "redis", "storage"))
        return JSONResponse(
            status_code=200 if required_ok else 503,
            content={"status": "ok" if required_ok else "degraded", "dependencies": deps},
        )

    @app.get("/api/v1/health", tags=["System"], summary="Full health and integration report")
    async def health() -> JSONResponse:
        s = get_settings()
        deps = await _check_dependencies(s)
        integrations = s.integration_status()
        required_ok = all(deps[k]["status"] == "ok" for k in ("database", "redis", "storage"))
        payload = {
            "status": "ok" if required_ok else "degraded",
            "version": __version__,
            "environment": s.environment,
            "dependencies": deps,
            "integrations": integrations,
            "notes": [
                (
                    "Optional integrations reporting configured=false are disabled by design, "
                    "not failing. Publishing requires YOUTUBE_CLIENT_ID/SECRET."
                ),
                (
                    "AI analysis falls back to deterministic heuristics when no provider is set; "
                    "output quality is lower and labelled as heuristic in the UI."
                ),
            ],
        }
        return JSONResponse(status_code=200 if required_ok else 503, content=payload)

    return app


async def _check_dependencies(s: Any) -> dict[str, dict[str, str]]:
    """Probe each dependency. Honest results only — no optimistic defaults.

    Imports are top-level so a missing optional driver surfaces as a clear dependency
    failure rather than an ImportError inside a probe.
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
    try:
        client = aioredis.from_url(s.redis_url, socket_connect_timeout=3)
        await client.ping()
        await client.aclose()
        deps["redis"] = {"status": "ok", "detail": "reachable"}
    except Exception as exc:  # noqa: BLE001
        deps["redis"] = {"status": "down", "detail": type(exc).__name__}

    # storage
    if s.storage_backend == "local":
        root = Path(s.local_storage_root)
        try:
            root.mkdir(parents=True, exist_ok=True)
            ok = root.exists() and os.access(root, os.W_OK)
            free_gb = shutil.disk_usage(root).free / 1e9
            deps["storage"] = {
                "status": "ok" if ok else "degraded",
                "detail": f"local root writable={ok}, free={free_gb:.1f}GB"
                + ("" if free_gb >= s.min_free_disk_gb else " (below MIN_FREE_DISK_GB)"),
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
