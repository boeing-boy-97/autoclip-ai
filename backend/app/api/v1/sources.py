"""Source endpoints: upload a video, list and inspect sources."""

from __future__ import annotations

import contextlib
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status

from app.api.deps import (
    ApiError,
    AuthDep,
    DbDep,
    Role,
    UserRateLimit,
    WorkspaceContext,
    require_csrf,
    workspace_access,
)
from app.api.schemas import SourceOut
from app.core.config import get_settings
from app.db.repositories import ConflictError, NotFoundError, SourceRepository
from app.services.ingest import IngestError, IngestService
from app.services.media_probe import ffmpeg_available
from app.services.storage import StorageError, get_storage

router = APIRouter(
    prefix="/workspaces/{workspace_id}/projects/{project_id}/sources",
    tags=["sources"],
)

_CHUNK = 1024 * 1024  # 1 MiB streaming reads — a video is never held in memory

# Uploads are the expensive, abusable operation (CPU for probing, bytes for storage). One limit per
# user per hour; 0 disables it.
_UPLOAD_RATE_LIMIT = UserRateLimit(
    "source_upload", limit_setting="rate_limit_upload_per_hour", window_seconds=3600
)


def _ws_dep(min_role: Role):  # noqa: ANN202 - returns a FastAPI dependency callable
    def dependency(
        workspace_id: uuid.UUID,
        auth_ctx: AuthDep,
        db: DbDep,
    ) -> WorkspaceContext:
        return workspace_access(workspace_id, auth_ctx, db, min_role=min_role)

    return dependency


WorkspaceViewer = Annotated[WorkspaceContext, Depends(_ws_dep("viewer"))]
# Uploading writes data and consumes processing capacity: `editor`, never `viewer`.
WorkspaceEditor = Annotated[WorkspaceContext, Depends(_ws_dep("editor"))]


def _storage():  # noqa: ANN202
    settings = get_settings()
    try:
        return get_storage(settings)
    except StorageError as exc:
        raise ApiError("storage_unavailable", str(exc), status_code=503) from exc


@router.post(
    "",
    response_model=SourceOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf), Depends(_UPLOAD_RATE_LIMIT)],
)
def upload_source(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    auth: AuthDep,
    db: DbDep,
    file: Annotated[UploadFile, File(description="Video file to ingest")],
    rights_basis: Annotated[str, Form()],
    rights_statement: Annotated[str, Form()],
    ctx: WorkspaceEditor,  # noqa: ARG001 - authorization by declaration
) -> SourceOut:
    """Upload a source video.

    Declared `def`, not `async def`, on purpose: spooling, `ffprobe` and the object-store upload are
    all blocking calls. As a sync handler FastAPI runs it in the threadpool, so one large upload
    cannot stall every other request on the worker's event loop.

    Order of checks: CSRF → rate limit → workspace membership → rights declaration →
    extension/MIME/magic bytes → real `ffprobe` inspection. The file is spooled to a temp file on
    the configured scratch volume (bounded by `MAX_UPLOAD_MB`), validated, then streamed into
    storage, so memory use stays constant. On any rejection the stored object is removed — no
    orphaned bytes, no half-ingested project.
    """
    settings = get_settings()
    if not ffmpeg_available(settings.ffprobe_bin):
        # Report the missing dependency; never pretend the file passed inspection.
        raise ApiError(
            "media_tooling_unavailable",
            f"ffprobe ('{settings.ffprobe_bin}') is not installed on this host, so media cannot be "
            "inspected. Install FFmpeg or set FFPROBE_BIN.",
            status_code=503,
        )

    max_bytes = settings.max_upload_mb * 1024 * 1024
    declared_length = request.headers.get("content-length", "")
    if declared_length.isdigit() and int(declared_length) > max_bytes + 2 * 1024 * 1024:
        raise ApiError(
            "payload_too_large",
            f"upload exceeds the {settings.max_upload_mb} MiB limit",
            status_code=413,
        )

    spool_dir = Path(settings.tmp_root)
    spool_dir.mkdir(parents=True, exist_ok=True)

    tmp_path: str | None = None
    try:
        # Spool to disk while enforcing the cap: the cap is authoritative, not Content-Length.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".upload", dir=spool_dir) as spool:
            tmp_path = spool.name
            total = 0
            while True:
                chunk = file.file.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ApiError(
                        "payload_too_large",
                        f"upload exceeds the {settings.max_upload_mb} MiB limit",
                        status_code=413,
                    )
                spool.write(chunk)

        if total == 0:
            raise ApiError("unsupported_media", "uploaded file is empty", status_code=415)

        try:
            result = IngestService(db, settings, _storage()).ingest_upload(
                workspace_id=workspace_id,
                project_id=project_id,
                user_id=auth.user.id,
                filename=file.filename or "",
                declared_mime=file.content_type,
                spool_path=tmp_path,
                rights_basis=rights_basis,
                rights_statement=rights_statement,
            )
        except IngestError as exc:
            raise ApiError(exc.code, exc.detail, status_code=exc.status_code) from exc
        except NotFoundError as exc:
            raise ApiError("project_not_found", "project not found", status_code=404) from exc
        except ConflictError as exc:
            raise ApiError(exc.code, str(exc), status_code=409) from exc

        return SourceOut.from_model(result.source)
    finally:
        if tmp_path:
            # Cleanup failure must never mask the real response.
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()


@router.get("", response_model=list[SourceOut])
def list_sources(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001 - authorization by declaration
) -> list[SourceOut]:
    source = SourceRepository(db).for_project(workspace_id=workspace_id, project_id=project_id)
    return [SourceOut.from_model(source)] if source else []


@router.get("/{source_id}", response_model=SourceOut)
def get_source(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001 - authorization by declaration
) -> SourceOut:
    try:
        source = SourceRepository(db).get(workspace_id=workspace_id, source_id=source_id)
    except NotFoundError as exc:
        raise ApiError("source_not_found", "source not found", status_code=404) from exc
    if source.project_id != project_id:
        raise ApiError("source_not_found", "source not found", status_code=404)
    return SourceOut.from_model(source)
