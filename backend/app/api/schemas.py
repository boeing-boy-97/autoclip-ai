"""Pydantic response/request schemas for API v1.

These are the wire contract. They are deliberately separate from the ORM models: an ORM change
must never silently alter the public API, and secrets (password_hash, token hashes) can never be
serialised by accident.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import User
from app.db.models import Workspace as WorkspaceModel


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- identity


class WorkspaceRef(BaseModel):
    id: uuid.UUID
    name: str
    role: str


def workspace_memberships(pairs: list[tuple[WorkspaceModel, str]]) -> list[WorkspaceRef]:
    return [WorkspaceRef(id=ws.id, name=ws.name, role=role) for ws, role in pairs]


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    display_name: str
    locale: str = "en"
    status: str = "active"
    created_at: datetime | None = None
    workspaces: list[WorkspaceRef] = Field(default_factory=list)

    @classmethod
    def from_model(cls, user: User, memberships: list[WorkspaceRef] | None = None) -> UserOut:
        out = cls.model_validate(user)
        out.workspaces = memberships or []
        return out


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    default_language: str
    role: str | None = None
    created_at: datetime


# --------------------------------------------------------------------------- sources


class RightsDeclaration(BaseModel):
    """Compliance gate. A YouTube URL alone is never treated as a media grant."""

    basis: str = Field(
        description="original | licensed | permission | creative_commons | owned_channel"
    )
    statement: str = Field(min_length=10, max_length=2000)
    licence_reference: str | None = None

    @model_validator(mode="after")
    def _valid_basis(self) -> RightsDeclaration:
        allowed = {"original", "licensed", "permission", "creative_commons", "owned_channel"}
        if self.basis not in allowed:
            raise ValueError(f"basis must be one of {sorted(allowed)}")
        if not self.statement.strip():
            raise ValueError("statement must describe your rights to this media")
        return self


class SourceOut(ORMModel):
    id: uuid.UUID
    kind: str
    state: str
    original_filename: str | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    byte_size: int | None = None
    sha256: str | None = None
    rights_basis: str | None = None
    rights_confirmed_at: datetime | None = None
    probe: dict[str, Any] | None = None

    @classmethod
    def from_model(cls, source) -> SourceOut:  # noqa: ANN001
        return cls.model_validate(
            {
                **{
                    k: getattr(source, k)
                    for k in cls.model_fields
                    if k not in {"sha256", "probe"}
                },
                "sha256": source.sha256.hex() if source.sha256 else None,
                "probe": source.probe_json,
            }
        )


# --------------------------------------------------------------------------- projects


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_platform: str = "youtube_shorts"
    language: str = "en"
    clips_requested: int = Field(default=5, ge=1, le=30)
    min_duration_ms: int = Field(default=15000, ge=3000)
    max_duration_ms: int = Field(default=65000, le=180000)
    preferred_durations_ms: list[int] = Field(default_factory=lambda: [15000, 30000, 45000])
    template_key: str = "podcast"
    caption_style_key: str = "bold_center"
    publishing_mode: str = "manual_approval"
    budget_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_durations(self) -> ProjectCreate:
        if self.min_duration_ms >= self.max_duration_ms:
            raise ValueError("min_duration_ms must be less than max_duration_ms")
        if self.publishing_mode not in {"auto", "manual_approval", "private_review"}:
            raise ValueError("publishing_mode must be auto, manual_approval or private_review")
        return self


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    clips_requested: int | None = Field(default=None, ge=1, le=30)
    min_duration_ms: int | None = Field(default=None, ge=3000)
    max_duration_ms: int | None = Field(default=None, le=180000)
    template_key: str | None = None
    caption_style_key: str | None = None
    publishing_mode: str | None = None
    budget_usd: float | None = Field(default=None, ge=0)


class ProjectCounts(BaseModel):
    sources: int = 0
    jobs_active: int = 0


class ProjectOut(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    status: str
    progress: int | None = None
    language: str
    target_platform: str
    clips_requested: int
    min_duration_ms: int
    max_duration_ms: int
    template_key: str
    caption_style_key: str
    publishing_mode: str
    budget_usd: float | None = None
    cost_usd: float = 0
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime
    updated_at: datetime
    source: SourceOut | None = None


class ProjectDetail(ProjectOut):
    stage_events: list[StageEventOut] = Field(default_factory=list)

    @classmethod
    def build(
        cls, project, events, source=None  # noqa: ANN001
    ) -> ProjectDetail:
        """Assemble from ORM objects, normalising the nested source.

        `ProjectOut.source` is a `SourceOut`, and the ORM's `sha256` is bytes. Validating the
        ORM object wholesale therefore fails at serialisation time — a bug that stays invisible
        until a project actually has a source. Construction goes through `SourceOut.from_model`.
        """
        detail = cls.model_validate(
            {
                **{
                    name: getattr(project, name)
                    for name in ProjectOut.model_fields
                    if name != "source"
                },
                "source": SourceOut.from_model(source) if source is not None else None,
            }
        )
        detail.stage_events = [StageEventOut.model_validate(e) for e in events]
        return detail


class StageEventOut(ORMModel):
    from_status: str | None = None
    to_status: str
    task_name: str | None = None
    job_id: uuid.UUID | None = None
    progress: int | None = None
    error_code: str | None = None
    detail: str | None = None
    at: datetime


class ProjectPage(BaseModel):
    items: list[ProjectOut]
    next_cursor: str | None = None
    total_estimate: int | None = None


# --------------------------------------------------------------------------- jobs


class JobOut(ORMModel):
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    stage: str
    queue: str
    task_name: str
    status: str
    progress: int | None = None
    attempts: int = 0
    max_attempts: int = 3
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime | None = None


# --------------------------------------------------------------------------- dashboard


class ActivityOut(ORMModel):
    action: str
    target_type: str | None = None
    at: datetime


class DashboardSummary(BaseModel):
    projects_total: int
    projects_by_status: dict[str, int]
    sources_processed: int
    failed_jobs: int
    queue_depth: int
    storage_mb: float
    unread_notifications: int
    recent_projects: list[ProjectOut]
    recent_activity: list[ActivityOut]
    is_empty: bool = False
