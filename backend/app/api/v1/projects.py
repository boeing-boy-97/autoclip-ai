"""Project endpoints (docs/openapi.json: /projects/*).

Routing shape: `/workspaces/{workspace_id}/projects...`.

Rationale for nesting under the workspace rather than a flat `/projects` path (the spec's initial
contract): the workspace is the authorization boundary, so making it part of the URL means the
client cannot submit a request whose authorization context is ambiguous. The spec explicitly
permits refining endpoint shapes where implementation requires it; this refinement is recorded in
`docs/API.md` §Changelog. A flat alias can be added later without breaking this contract.

Every handler resolves authorization via `require_ws_*` (which raises 404 for non-members), then
passes `workspace_id` into the repository — so a forgotten filter is impossible.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    ApiError,
    AuthContext,
    CSRFDep,
    DbDep,
    Role,
    WorkspaceContext,
    current_auth,
    workspace_access,
)
from app.api.schemas import (
    ProjectCreate,
    ProjectDetail,
    ProjectOut,
    ProjectPage,
    ProjectUpdate,
    StageEventOut,
)
from app.db.locks import guard_project
from app.db.repositories import (
    ConflictError,
    NotFoundError,
    ProjectRepository,
    SourceRepository,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/projects", tags=["Projects"])


# --------------------------------------------------------------------- dependencies
def _ws_dep(min_role: Role):  # noqa: ANN202 - returns a FastAPI dependency callable
    def _resolver(
        workspace_id: uuid.UUID,
        auth_ctx: Annotated[AuthContext, Depends(current_auth)],
        db: DbDep,
    ) -> WorkspaceContext:
        try:
            return workspace_access(workspace_id, auth_ctx, db, min_role=min_role)
        except NotFoundError as exc:
            raise ApiError("workspace_not_found", str(exc), status_code=404) from exc

    return _resolver


WorkspaceViewer = Annotated[WorkspaceContext, Depends(_ws_dep("viewer"))]
WorkspaceEditor = Annotated[WorkspaceContext, Depends(_ws_dep("editor"))]
WorkspaceAdmin = Annotated[WorkspaceContext, Depends(_ws_dep("admin"))]


def _translate(exc: Exception) -> ApiError:
    if isinstance(exc, NotFoundError):
        return ApiError(f"{exc.resource}_not_found", str(exc), status_code=404)
    if isinstance(exc, ConflictError):
        return ApiError(exc.code, exc.detail, status_code=409)
    raise exc


# --------------------------------------------------------------------- endpoints
@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectOut,
    summary="Create Project",
)
def create_project(
    payload: ProjectCreate,
    ctx: WorkspaceEditor,
    db: DbDep,
    _csrf: CSRFDep,
) -> ProjectOut:
    """Create a project in CREATED state. The stage journal records the first transition."""
    repo = ProjectRepository(db)
    project = repo.create(
        workspace_id=ctx.workspace.id,
        created_by=ctx.user.id,
        name=payload.name,
        target_platform=payload.target_platform,
        language=payload.language,
        clips_requested=payload.clips_requested,
        min_duration_ms=payload.min_duration_ms,
        max_duration_ms=payload.max_duration_ms,
        preferred_durations_ms=payload.preferred_durations_ms,
        template_key=payload.template_key,
        caption_style_key=payload.caption_style_key,
        publishing_mode=payload.publishing_mode,
        budget_usd=payload.budget_usd,
    )
    db.commit()
    db.refresh(project)
    return ProjectOut.from_model(project)


@router.get("", response_model=ProjectPage, summary="List Projects")
def list_projects(
    ctx: WorkspaceViewer,
    db: DbDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query()] = None,
) -> ProjectPage:
    repo = ProjectRepository(db)
    try:
        page = repo.list(
            workspace_id=ctx.workspace.id, status=status_filter, query=q, limit=limit, cursor=cursor
        )
    except ConflictError as exc:
        raise _translate(exc) from exc
    return ProjectPage(
        # Not `model_validate`: a project that has a source carries bytes for sha256, which the
        # schema would refuse. This was a live 500 on the project list until it was fixed.
        items=[ProjectOut.from_model(p) for p in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{project_id}", response_model=ProjectDetail, summary="Get Project")
def get_project(project_id: uuid.UUID, ctx: WorkspaceViewer, db: DbDep) -> ProjectDetail:
    repo = ProjectRepository(db)
    try:
        project = repo.get(workspace_id=ctx.workspace.id, project_id=project_id)
        events = repo.stage_events(workspace_id=ctx.workspace.id, project_id=project_id)
    except (NotFoundError, ConflictError) as exc:
        raise _translate(exc) from exc

    source = SourceRepository(db).for_project(workspace_id=ctx.workspace.id, project_id=project_id)
    return ProjectDetail.build(project, events, source)


@router.patch("/{project_id}", response_model=ProjectOut, summary="Update Project")
def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    ctx: WorkspaceEditor,
    db: DbDep,
    _csrf: CSRFDep,
) -> ProjectOut:
    """Changing settings never mutates the source file or any existing clip version."""
    repo = ProjectRepository(db)
    try:
        guard_project(db, ctx.workspace.id, project_id)
        project = repo.update(
            workspace_id=ctx.workspace.id,
            project_id=project_id,
            **payload.model_dump(exclude_unset=True),
        )
    except (NotFoundError, ConflictError) as exc:
        raise _translate(exc) from exc
    db.commit()
    db.refresh(project)
    return ProjectOut.from_model(project)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Project",
)
def delete_project(
    project_id: uuid.UUID,
    ctx: WorkspaceAdmin,
    db: DbDep,
    _csrf: CSRFDep,
) -> Response:
    """Soft delete. Refused while the project has active jobs (409 project_busy)."""
    repo = ProjectRepository(db)
    try:
        guard_project(db, ctx.workspace.id, project_id)
        repo.soft_delete(workspace_id=ctx.workspace.id, project_id=project_id)
    except (NotFoundError, ConflictError) as exc:
        db.rollback()
        raise _translate(exc) from exc
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{project_id}/stages",
    response_model=list[StageEventOut],
    summary="List Project Stages",
)
def list_stages(project_id: uuid.UUID, ctx: WorkspaceViewer, db: DbDep) -> list[StageEventOut]:
    """Append-only state-machine journal — the audit trail of every transition."""
    repo = ProjectRepository(db)
    try:
        events = repo.stage_events(workspace_id=ctx.workspace.id, project_id=project_id)
    except (NotFoundError, ConflictError) as exc:
        raise _translate(exc) from exc
    return [StageEventOut.model_validate(e) for e in events]
