"""Analysis endpoints: run the pipeline, read the transcript, inspect ranked moments.

These exist so the pipeline's output is *inspectable*. A ranked list with no transcript and no
per-criterion evidence would be unauditable, so every score can be traced back to the segments
that produced it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

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
    AnalysisStatusOut,
    MomentOut,
    MomentPage,
    TranscriptOut,
)
from app.core.config import get_settings
from app.db.models import (
    CandidateMoment,
    CandidateScore,
    Project,
    SourceVideo,
    Transcript,
    TranscriptSegment,
)
from app.db.repositories import JobRepository
from app.services.analysis import AnalysisError, AnalysisService
from app.services.storage import StorageError, get_storage
from app.services.transcription import AsrFailedError, AsrUnavailableError
from app.worker.dispatch import BrokerUnavailableError, dispatch_analysis

router = APIRouter(prefix="/workspaces/{workspace_id}/projects/{project_id}", tags=["Analysis"])

DISCLAIMER = (
    "Scores estimate content characteristics (hook strength, narrative completeness, clarity) "
    "from the transcript. They are not predictions of views, reach or revenue."
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
WorkspaceEditor = Annotated[WorkspaceContext, Depends(_ws_dep("editor"))]

# Transcription is the most expensive operation in the product; one user cannot queue it at will.
_ANALYSIS_RATE_LIMIT = UserRateLimit(
    "analysis_run", limit_setting="rate_limit_analysis_per_hour", window_seconds=3600
)


def _project(db, workspace_id: uuid.UUID, project_id: uuid.UUID) -> Project:  # noqa: ANN001
    project = db.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.workspace_id == workspace_id,
            Project.deleted_at.is_(None),
        )
    )
    if project is None:
        raise ApiError("project_not_found", "project not found", status_code=404)
    return project


def _latest_transcript(db, project_id: uuid.UUID) -> Transcript | None:  # noqa: ANN001
    return db.scalar(
        select(Transcript)
        .where(Transcript.project_id == project_id)
        .order_by(Transcript.created_at.desc())
    )


@router.post(
    "/analyze",
    response_model=AnalysisStatusOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf), Depends(_ANALYSIS_RATE_LIMIT)],
    summary="Analyse Project",
)
def analyse_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    auth: AuthDep,
    db: DbDep,
    ctx: WorkspaceEditor,  # noqa: ARG001
    force: Annotated[bool, Query(description="Re-run even if an identical run succeeded")] = False,
) -> AnalysisStatusOut:
    """Queue audio extraction → transcription → candidate detection → scoring.

    The work runs in a Celery worker, not in this request. The response is the *current* analysis
    status: `status` reflects the project's stage, and `job_id` identifies the queued job so the
    caller can poll `GET /jobs/{job_id}` for progress.

    If an identical successful run already exists, nothing is queued and the existing result is
    reported immediately (`reused=true`) — polling a finished analysis should never be a way to
    accidentally pay for it twice.
    """
    project = _project(db, workspace_id, project_id)
    settings = get_settings()
    storage = _storage()

    # Resolve idempotency and legality synchronously, before touching the broker: a request that
    # cannot legally run must fail here with a precise error, not enqueue and fail later.
    service = AnalysisService(db, settings, storage)
    try:
        plan = service.plan(
            workspace_id=workspace_id,
            project_id=project_id,
            force=force,
        )
    except AnalysisError as exc:
        raise ApiError(exc.code, exc.detail, status_code=exc.status_code) from exc

    if plan.reused:
        return _status(db, project, transcript_id=plan.transcript_id)
    job_id = plan.job_id
    assert job_id is not None  # a non-reused plan always reserved a job

    try:
        task_id = dispatch_analysis(
            settings,
            workspace_id=workspace_id,
            project_id=project_id,
            user_id=auth.user.id,
            job_id=job_id,
            force=force,
        )
    except AnalysisError as exc:
        # Eager mode runs the task in-request, so a pipeline failure arrives here as the same
        # AnalysisError the synchronous implementation raised. Translate it back to the documented
        # HTTP contract instead of letting it become a 500.
        raise ApiError(exc.code, exc.detail, status_code=exc.status_code) from exc
    except AsrUnavailableError as exc:
        raise ApiError(exc.code, exc.detail, status_code=503) from exc
    except AsrFailedError as exc:
        raise ApiError(exc.code, exc.detail, status_code=422) from exc
    except BrokerUnavailableError as exc:
        # The job row is committed and PENDING, so the work is discoverable and retryable once
        # the broker is back. Say that plainly instead of pretending the run started.
        raise ApiError("queue_unavailable", str(exc), status_code=503) from exc

    if task_id is not None:
        jobs = JobRepository(db)
        job = jobs.get(workspace_id=workspace_id, job_id=job_id)
        job.celery_task_id = task_id
        db.commit()

    out = _status(db, project, transcript_id=None)
    return out.model_copy(update={"job_id": job_id, "queued": True})


@router.get("/analysis", response_model=AnalysisStatusOut, summary="Get Analysis Status")
def get_analysis(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
) -> AnalysisStatusOut:
    project = _project(db, workspace_id, project_id)
    return _status(db, project)


@router.get("/transcript", response_model=TranscriptOut, summary="Get Transcript")
def get_transcript(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
    include_words: Annotated[bool, Query()] = False,
) -> TranscriptOut:
    project = _project(db, workspace_id, project_id)
    transcript = _latest_transcript(db, project.id)
    if transcript is None:
        raise ApiError(
            "transcript_not_found",
            "this project has not been transcribed yet; POST /analyze first",
            status_code=404,
        )
    segments = list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == transcript.id)
            .order_by(TranscriptSegment.idx)
        )
    )
    source = db.scalar(select(SourceVideo.duration_ms).where(SourceVideo.project_id == project.id))
    # Never mutate the ORM objects to shape the response: a managed object that is dirtied here
    # would be written back on commit, silently erasing stored word timings.
    return TranscriptOut.build(
        transcript, segments, duration_ms=source, include_words=include_words
    )


@router.get("/moments", response_model=MomentPage, summary="List Candidate Moments")
def list_moments(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    selected_only: Annotated[bool, Query()] = False,
    category: Annotated[str | None, Query()] = None,
) -> MomentPage:
    project = _project(db, workspace_id, project_id)
    stmt = select(CandidateMoment).where(CandidateMoment.project_id == project.id)
    if selected_only:
        stmt = stmt.where(CandidateMoment.selected.is_(True))
    if category:
        stmt = stmt.where(CandidateMoment.category == category)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    selected_count = (
        db.scalar(
            select(func.count())
            .select_from(CandidateMoment)
            .where(
                CandidateMoment.project_id == project.id,
                CandidateMoment.selected.is_(True),
            )
        )
        or 0
    )

    rows = list(db.scalars(stmt.order_by(CandidateMoment.start_ms).offset(offset).limit(limit)))
    scores = (
        {
            s.candidate_id: s
            for s in db.scalars(
                select(CandidateScore).where(CandidateScore.candidate_id.in_([r.id for r in rows]))
            )
        }
        if rows
        else {}
    )

    # Present strongest first: the ranking is the product, but the suppression reason is kept so
    # a reviewer can see what was considered and rejected.
    items = [MomentOut.build(m, scores.get(m.id)) for m in rows]
    items.sort(key=lambda m: m.score.overall if m.score else 0, reverse=True)
    return MomentPage(
        items=items,
        next_cursor=None if offset + len(items) >= total else str(offset + len(items)),
        total=total,
        selected_count=selected_count,
        disclaimer=DISCLAIMER,
    )


def _status(db, project: Project, *, transcript_id: uuid.UUID | None = None) -> AnalysisStatusOut:  # noqa: ANN001
    transcript = (
        db.scalar(select(Transcript).where(Transcript.id == transcript_id))
        if transcript_id
        else _latest_transcript(db, project.id)
    )
    settings = get_settings()
    if transcript is None:
        return AnalysisStatusOut(
            project_id=project.id,
            status=project.status,
            has_transcript=False,
            asr_configured=settings.asr_provider == "local" or bool(settings.openai_api_key),
        )

    segment_count = (
        db.scalar(
            select(func.count())
            .select_from(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == transcript.id)
        )
        or 0
    )
    word_count = _approx_word_count(db, transcript.id)
    candidate_count = (
        db.scalar(
            select(func.count())
            .select_from(CandidateMoment)
            .where(CandidateMoment.project_id == project.id)
        )
        or 0
    )
    selected_count = (
        db.scalar(
            select(func.count())
            .select_from(CandidateMoment)
            .where(
                CandidateMoment.project_id == project.id,
                CandidateMoment.selected.is_(True),
            )
        )
        or 0
    )
    weights_version = db.scalar(
        select(CandidateScore.weights_version)
        .join(CandidateMoment, CandidateMoment.id == CandidateScore.candidate_id)
        .where(CandidateMoment.project_id == project.id)
        .limit(1)
    )

    return AnalysisStatusOut(
        project_id=project.id,
        status=project.status,
        has_transcript=True,
        transcript_id=transcript.id,
        provider=transcript.provider,
        model=transcript.model,
        language=transcript.language,
        segment_count=segment_count,
        word_count=word_count,
        candidate_count=candidate_count,
        selected_count=selected_count,
        weights_version=weights_version,
        method=_scoring_method(db, project.id),
        asr_configured=True,
    )


def _scoring_method(db, project_id: uuid.UUID) -> str | None:  # noqa: ANN001
    """Report how candidates were scored, if any exist."""
    row = db.scalar(
        select(CandidateMoment.rationale).where(CandidateMoment.project_id == project_id).limit(1)
    )
    if not row:
        return None
    if "heuristic" in row.lower():
        return "heuristic"
    return "model"


def _approx_word_count(db, transcript_id: uuid.UUID) -> int:  # noqa: ANN001
    """Count words from the stored segments (split on whitespace)."""
    texts = db.scalars(
        select(TranscriptSegment.text).where(TranscriptSegment.transcript_id == transcript_id)
    )
    return sum(len((t or "").split()) for t in texts)


def _storage():  # noqa: ANN202
    settings = get_settings()
    try:
        return get_storage(settings)
    except StorageError as exc:
        raise ApiError(
            "storage_unavailable",
            f"object storage is not usable: {exc}",
            status_code=503,
        ) from exc
