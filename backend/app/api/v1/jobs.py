"""Job inspection and control: list, read, cancel, retry.

These endpoints exist because once work moved to a worker, "what is happening" stopped being
answerable from the HTTP response. Every job row is durable and owned by a workspace, so the
progress of background work is inspectable instead of guessed at from polling project status.

Cancellation and retry are deliberately narrow:
  - cancel refuses jobs that already finished (that is an error, not a no-op)
  - retry refuses SUCCEEDED jobs, because re-running good work is `/analyze?force=true`, which
    has different semantics and is audited differently
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    ApiError,
    AuthDep,
    DbDep,
    Role,
    WorkspaceContext,
    require_csrf,
    workspace_access,
)
from app.api.schemas import JobOut, JobPage
from app.core.config import get_settings
from app.db.repositories import ConflictError, JobRepository, NotFoundError, ProcessingJob
from app.worker.dispatch import dispatch_analysis

router = APIRouter(prefix="/workspaces/{workspace_id}/jobs", tags=["Jobs"])

# Only the pipeline task exists so far; anything else in `processing_jobs` cannot be restarted.
_RETRYABLE_TASKS = frozenset({"analysis.run"})


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


def _job(db, workspace_id: uuid.UUID, job_id: uuid.UUID) -> ProcessingJob:  # noqa: ANN001
    try:
        return JobRepository(db).get(workspace_id=workspace_id, job_id=job_id)
    except NotFoundError as exc:
        raise ApiError("not_found", "job not found", status_code=404) from exc


@router.get("", response_model=JobPage, summary="List Jobs")
def list_jobs(
    workspace_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    stage: Annotated[str | None, Query()] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
) -> JobPage:
    """Newest-first background jobs, filterable by status, stage and project."""
    page = JobRepository(db).list_cursor(
        workspace_id=workspace_id,
        status=status_filter,
        stage=stage,
        project_id=project_id,
        limit=limit,
        cursor=cursor,
    )
    return JobPage(
        items=[JobOut.model_validate(j) for j in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{job_id}", response_model=JobOut, summary="Get Job")
def get_job(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceViewer,  # noqa: ARG001
) -> JobOut:
    return JobOut.model_validate(_job(db, workspace_id, job_id))


@router.post(
    "/{job_id}/cancel",
    response_model=JobOut,
    status_code=status.HTTP_200_OK,
    summary="Cancel Job",
    dependencies=[Depends(require_csrf)],
)
def cancel_job(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceEditor,  # noqa: ARG001
) -> JobOut:
    """Mark an unfinished job cancelled.

    This stops the *record* from being picked up or retried. It cannot interrupt a task already
    executing inside a worker process — a CPU-bound FFmpeg or ASR call is not preemptible from
    here, and pretending otherwise would be a lie. A running task that finishes will overwrite
    this status on its own exit path.
    """
    try:
        job = JobRepository(db).mark_cancelled(workspace_id=workspace_id, job_id=job_id)
    except NotFoundError as exc:
        raise ApiError("not_found", "job not found", status_code=404) from exc
    except ConflictError as exc:
        raise ApiError(exc.code, exc.detail, status_code=409) from exc
    return JobOut.model_validate(job)


@router.post(
    "/{job_id}/retry",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry Job",
    dependencies=[Depends(require_csrf)],
)
def retry_job(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    db: DbDep,
    ctx: WorkspaceEditor,
) -> JobOut:
    """Re-queue a failed, dead-letter or cancelled job."""
    jobs = JobRepository(db)
    job = _job(db, workspace_id, job_id)

    if job.task_name not in _RETRYABLE_TASKS:
        raise ApiError(
            "job_not_retryable",
            f"there is no retry path for task {job.task_name!r}",
            status_code=409,
        )
    if job.project_id is None:
        raise ApiError("job_not_retryable", "job is not attached to a project", status_code=409)

    try:
        job = jobs.requeue(workspace_id=workspace_id, job_id=job_id)
    except ConflictError as exc:
        raise ApiError(exc.code, exc.detail, status_code=409) from exc

    project_id = job.project_id
    # Commit the reset before handing the message to the broker: a crash between the two must
    # leave a discoverable PENDING job rather than a message pointing at stale state.
    db.commit()

    # `force=True` is the point of a retry: redo the work rather than reusing a prior result.
    # Without it the task reuses the existing transcript, returns immediately, and the retry
    # looks like it did nothing.
    project_id = job.project_id
    assert project_id is not None  # a retryable analysis job always belongs to a project
    task_id = dispatch_analysis(
        get_settings(),
        workspace_id=workspace_id,
        project_id=project_id,
        user_id=ctx.user.id,
        job_id=job_id,
        force=True,
    )
    if task_id is not None:
        job = jobs.get(workspace_id=workspace_id, job_id=job_id)
        job.celery_task_id = task_id
        db.commit()
    return JobOut.model_validate(job)
