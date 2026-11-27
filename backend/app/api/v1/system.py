"""Dashboard, workspace, and job-inspection endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import (
    ApiError,
    AuthContext,
    AuthDep,
    DbDep,
    Role,
    WorkspaceContext,
    current_auth,
    workspace_access,
)
from app.api.schemas import DashboardSummary, ProjectOut, WorkspaceOut
from app.db.repositories import (
    DashboardRepository,
    NotFoundError,
    WorkspaceRepository,
)

router = APIRouter(tags=["Dashboard"])


def _ws(min_role: Role):  # noqa: ANN202
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


WorkspaceViewer = Annotated[WorkspaceContext, Depends(_ws("viewer"))]


@router.get(
    "/workspaces", response_model=list[WorkspaceOut], tags=["Workspaces"], summary="List Workspaces"
)
def list_workspaces(auth_ctx: AuthDep, db: DbDep) -> list[WorkspaceOut]:
    pairs = WorkspaceRepository(db).list_for_user(user_id=auth_ctx.user.id)
    return [
        WorkspaceOut(
            id=ws.id,
            name=ws.name,
            slug=ws.slug,
            default_language=ws.default_language,
            role=role,
            created_at=ws.created_at,
        )
        for ws, role in pairs
    ]


@router.get(
    "/workspaces/{workspace_id}/dashboard",
    response_model=DashboardSummary,
    tags=["Dashboard"],
    summary="Get Dashboard Summary",
)
def dashboard_summary(ctx: WorkspaceViewer, db: DbDep) -> DashboardSummary:
    """Every value is a real query result. An empty workspace returns zeros and `is_empty`."""
    data = DashboardRepository(db).summary(workspace_id=ctx.workspace.id)
    return DashboardSummary(
        projects_total=data["projects_total"],
        projects_by_status=data["projects_by_status"],
        sources_processed=data["sources_processed"],
        failed_jobs=data["failed_jobs"],
        queue_depth=data["queue_depth"],
        storage_mb=data["storage_mb"],
        unread_notifications=data["unread_notifications"],
        recent_projects=[ProjectOut.from_model(p) for p in data["recent_projects"]],
        recent_activity=data["recent_activity"],
        is_empty=data["is_empty"],
    )
