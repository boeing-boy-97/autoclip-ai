"""Clip endpoints: generate, list, read, render, inspect versions and captions.

The route shapes follow the designed contract in `docs/openapi.json` — project-scoped paths for
generation and listing, and bare `/clips/{clip_id}` for a single clip. Workspace ownership is
resolved from the clip row itself, so a clip id alone is never enough to read someone else's work.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

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
from app.api.schemas import (
    CaptionCueOut,
    CaptionsOut,
    ClipGenerationOut,
    ClipOut,
    ClipPage,
    ClipRejectIn,
    ClipRenderOut,
    ClipRestoreOut,
    ClipVersionOut,
)
from app.core.config import get_settings
from app.db.models import (
    Clip,
    ClipVersion,
    MediaAsset,
    Transcript,
)
from app.db.models import (
    TranscriptSegment as _SegmentView,
)
from app.db.repositories import ClipRepository, NotFoundError
from app.services.captions import build_track, to_webvtt
from app.services.clips import ClipError, ClipService
from app.services.render import Template
from app.services.storage import get_storage

router = APIRouter(tags=["Clips"])


def _ws_dep(min_role: Role):  # noqa: ANN202 - returns a FastAPI dependency callable
    def dependency(
        workspace_id: uuid.UUID,
        auth_ctx: AuthDep,
        db: DbDep,
    ) -> WorkspaceContext:
        return workspace_access(workspace_id, auth_ctx, db, min_role=min_role)

    return dependency


WorkspaceViewer = Annotated[WorkspaceContext, Depends(_ws_dep("viewer"))]
WorkspaceEditor = Annotated[WorkspaceContext, Depends(_ws_dep("editor"))]

# A render pins a CPU for minutes; the queue is per user, not per clip.
_RENDER_RATE_LIMIT = UserRateLimit(
    "clip_render", limit_setting="rate_limit_render_per_minute", window_seconds=60
)


def _service(db) -> ClipService:  # noqa: ANN001
    settings = get_settings()
    return ClipService(db, settings, get_storage(settings))


def _project(db, workspace_id: uuid.UUID, project_id: uuid.UUID):  # noqa: ANN001, ANN202
    from app.db.repositories import ProjectRepository  # noqa: PLC0415 - avoids a cycle

    try:
        return ProjectRepository(db).get(workspace_id=workspace_id, project_id=project_id)
    except NotFoundError as exc:
        raise ApiError("not_found", "project not found", status_code=404) from exc


def _clip(db, workspace_id: uuid.UUID, clip_id: uuid.UUID) -> Clip:  # noqa: ANN001
    try:
        return ClipRepository(db).get(workspace_id=workspace_id, clip_id=clip_id)
    except NotFoundError as exc:
        raise ApiError("not_found", "clip not found", status_code=404) from exc


# --------------------------------------------------------------------------- generate & list


@router.post(
    "/workspaces/{workspace_id}/projects/{project_id}/generate-clips",
    response_model=ClipGenerationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Generate Clips",
    dependencies=[Depends(require_csrf)],
)
def generate_clips(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceEditor,
) -> ClipGenerationOut:
    """Turn the selected candidate moments into clips. Does not render them.

    Cheap and idempotent: calling it twice creates no duplicates, so it is safe to retry. Rendering
    is a separate, explicit step because it costs minutes of CPU per clip.
    """
    _project(db, workspace_id, project_id)
    try:
        outcome = _service(db).generate(
            workspace_id=workspace_id, project_id=project_id, user_id=ctx.user.id
        )
    except ClipError as exc:
        raise ApiError(exc.code, exc.detail, status_code=exc.status_code) from exc

    selected = outcome.created + outcome.existing
    return ClipGenerationOut(
        created=outcome.created,
        existing=outcome.existing,
        clip_ids=outcome.clip_ids,
        candidates_selected=selected,
    )


@router.get(
    "/workspaces/{workspace_id}/projects/{project_id}/clips",
    response_model=ClipPage,
    summary="List Project Clips",
)
def list_project_clips(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    publishable: Annotated[bool | None, Query()] = None,
) -> ClipPage:
    _project(db, workspace_id, project_id)
    page = ClipRepository(db).list_for_project(
        workspace_id=workspace_id,
        project_id=project_id,
        state=state,
        limit=limit,
        cursor=cursor,
    )
    items = [ClipOut.model_validate(c) for c in page.items]
    if publishable is not None:
        items = [c for c in items if c.publishable is publishable]
    return ClipPage(items=items, next_cursor=page.next_cursor)


# --------------------------------------------------------------------------- single clip


@router.get(
    "/workspaces/{workspace_id}/clips/{clip_id}", response_model=ClipOut, summary="Get Clip"
)
def get_clip(
    workspace_id: uuid.UUID,
    clip_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
) -> ClipOut:
    return ClipOut.model_validate(_clip(db, workspace_id, clip_id))


@router.post(
    "/workspaces/{workspace_id}/clips/{clip_id}/render",
    response_model=ClipRenderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Render Clip",
    dependencies=[Depends(require_csrf), Depends(_RENDER_RATE_LIMIT)],
)
def render_clip(
    workspace_id: uuid.UUID,
    clip_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceEditor,
    template_key: Annotated[str | None, Query()] = None,
) -> ClipRenderOut:
    """Cut, reframe and caption one clip into a new immutable version.

    Runs in-request today (the render is bounded and seconds-long for short clips). The job row is
    still written, so progress and failure are inspectable through `GET /jobs`.
    """
    clip = _clip(db, workspace_id, clip_id)
    try:
        outcome = _service(db).render(
            workspace_id=workspace_id,
            clip_id=clip_id,
            user_id=ctx.user.id,
            template_key=template_key,
        )
    except ClipError as exc:
        raise ApiError(exc.code, exc.detail, status_code=exc.status_code) from exc

    fresh = db.get(Clip, clip.id)
    return ClipRenderOut(
        clip_id=outcome.clip_id,
        version_id=outcome.version_id,
        version_no=outcome.version_no,
        state=fresh.state if fresh else clip.state,
        width=outcome.width,
        height=outcome.height,
        duration_ms=outcome.duration_ms,
        bytes=outcome.bytes,
        has_captions=outcome.has_captions,
        cue_count=outcome.cue_count,
        render_seconds=outcome.render_seconds,
    )


@router.get(
    "/workspaces/{workspace_id}/clips/{clip_id}/versions",
    response_model=list[ClipVersionOut],
    summary="List Clip Versions",
)
def list_clip_versions(
    workspace_id: uuid.UUID,
    clip_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
) -> list[ClipVersionOut]:
    _clip(db, workspace_id, clip_id)
    return [ClipVersionOut.build(v) for v in ClipRepository(db).versions(clip_id=clip_id)]


@router.get(
    "/workspaces/{workspace_id}/clips/{clip_id}/render-plan",
    response_model=dict,
    summary="Get Clip Render Plan",
)
def get_render_plan(
    workspace_id: uuid.UUID,
    clip_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
) -> dict:
    """The exact recipe used for the current version.

    Exposed because a rendered file whose recipe is unknown cannot be explained, reproduced, or
    compared after a fix. Includes the caption coverage caveats recorded at render time.
    """
    clip = _clip(db, workspace_id, clip_id)
    if clip.current_version_id is None:
        raise ApiError("clip_not_rendered", "this clip has not been rendered yet", status_code=409)
    version = db.get(ClipVersion, clip.current_version_id)
    if version is None:
        raise ApiError("clip_not_rendered", "the current version is missing", status_code=409)
    return {
        "version_no": version.version_no,
        "render_plan_version": version.render_plan_version,
        "render_plan": version.render_plan,
        "captions": (clip.meta or {}).get("captions"),
    }


@router.get(
    "/workspaces/{workspace_id}/clips/{clip_id}/captions",
    response_model=CaptionsOut,
    summary="Get Captions",
)
def get_captions(
    workspace_id: uuid.UUID,
    clip_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
) -> CaptionsOut:
    """Caption cues for this clip window, rebuilt from the stored word timings.

    Rebuilding rather than reading a cached file means the response is always consistent with the
    transcript that exists now, and the coverage numbers are computed from real data instead of
    being asserted. `words_without_timing` is reported, not hidden.
    """
    clip = _clip(db, workspace_id, clip_id)
    template = Template.load(clip.template_key, get_settings().template_dir)
    transcript = db.scalar(
        select(Transcript)
        .where(Transcript.source_video_id == clip.source_video_id)
        .order_by(Transcript.created_at.desc())
        .limit(1)
    )
    segments: list[dict] = []
    language = None
    if transcript is not None:
        language = transcript.language
        rows = db.scalars(
            select(_SegmentView)
            .where(
                _SegmentView.transcript_id == transcript.id,
                _SegmentView.end_ms > clip.start_ms,
                _SegmentView.start_ms < clip.end_ms,
            )
            .order_by(_SegmentView.idx)
        )
        segments = [
            {"idx": r.idx, "start_ms": r.start_ms, "end_ms": r.end_ms, "words": r.words}
            for r in rows
        ]

    track = build_track(
        segments,
        clip_start_ms=clip.start_ms,
        clip_end_ms=clip.end_ms,
        max_words_per_cue=template.max_words_per_cue,
        max_lines=template.max_lines,
    )
    return CaptionsOut(
        language=language,
        style_key=template.caption_style.get("style_key"),
        source=track.source,
        cue_count=len(track.cues),
        coverage=round(track.coverage, 4),
        words_in_range=track.words_in_range,
        words_captioned=track.words_captioned,
        words_without_timing=track.words_without_timing,
        truncated_at_clip_end=track.truncated_at_clip_end,
        warnings=track.warnings,
        cues=[
            CaptionCueOut(
                start_ms=c.start_ms, end_ms=c.end_ms, text=c.text, word_count=c.word_count
            )
            for c in track.cues
        ],
        vtt=to_webvtt(track.cues),
    )


@router.get("/workspaces/{workspace_id}/clips/{clip_id}/download")
def download_clip(
    workspace_id: uuid.UUID,
    clip_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
) -> dict:
    """Where the rendered file lives.

    The local storage backend cannot mint a signed URL and does not pretend to; it returns the
    storage key so the caller knows exactly what exists. A real URL only appears once an
    S3-compatible backend is configured.
    """
    clip = _clip(db, workspace_id, clip_id)
    if clip.current_version_id is None:
        raise ApiError("clip_not_rendered", "this clip has not been rendered yet", status_code=409)
    version = db.get(ClipVersion, clip.current_version_id)
    if version is None:
        raise ApiError("clip_not_rendered", "the rendered version is missing", status_code=409)
    asset = db.get(MediaAsset, version.video_asset_id) if version.video_asset_id else None
    if asset is None:
        raise ApiError("clip_not_rendered", "the rendered file is missing", status_code=409)
    return {
        "clip_id": str(clip_id),
        "version_no": version.version_no,
        "storage_key": asset.storage_key,
        "bytes": asset.byte_size,
        "width": asset.width,
        "height": asset.height,
        "duration_ms": asset.duration_ms,
        "download_url": None,
        "note": "local storage backend: no signed URL is available; fetch the storage key directly",
    }


@router.post(
    "/workspaces/{workspace_id}/clips/{clip_id}/reject",
    response_model=ClipOut,
    summary="Reject Clip",
)
def reject_clip(
    workspace_id: uuid.UUID,
    clip_id: uuid.UUID,
    payload: ClipRejectIn,
    db: DbDep,
    ctx: WorkspaceEditor,
) -> ClipOut:
    """Refuse a rendered clip, with a reason, and allow it to be rendered again.

    Rejecting is the only way a reviewed clip becomes re-renderable. That is deliberate: an
    approved or simply unreviewed file is never overwritten, so the version a person judged is
    always still there afterwards.
    """
    service = _service(db)
    try:
        clip = service.reject(
            workspace_id=workspace_id,
            clip_id=clip_id,
            reason=payload.reason,
            user_id=ctx.user.id,
        )
    except ClipError as exc:
        raise ApiError(exc.code, exc.detail, status_code=exc.status_code) from exc
    db.commit()
    db.refresh(clip)
    return ClipOut.model_validate(clip)


@router.post(
    "/workspaces/{workspace_id}/clips/{clip_id}/versions/{version_id}/restore",
    response_model=ClipRestoreOut,
    summary="Restore Clip Version",
)
def restore_clip_version(
    workspace_id: uuid.UUID,
    clip_id: uuid.UUID,
    version_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceEditor,
) -> ClipRestoreOut:
    """Make an earlier render the current one again.

    No re-encode: every version is immutable and its file is still in storage, so this is a
    pointer move. Restoring does not delete the version it replaces.
    """
    service = _service(db)
    previous = service.clips.get(workspace_id=workspace_id, clip_id=clip_id).current_version_id
    try:
        clip = service.restore_version(
            workspace_id=workspace_id,
            clip_id=clip_id,
            version_id=version_id,
            user_id=ctx.user.id,
        )
    except ClipError as exc:
        raise ApiError(exc.code, exc.detail, status_code=exc.status_code) from exc
    db.commit()
    db.refresh(clip)
    return ClipRestoreOut(
        clip=ClipOut.model_validate(clip),
        restored_version_id=version_id,
        previous_version_id=previous,
    )
