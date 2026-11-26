"""SQLAlchemy ORM models.

Scope: Phase 1 entities (identity, workspace, project, source, media asset, stage journal,
processing jobs). Later phases add transcripts/clips/publishing tables; they already exist in
`docs/database/schema.sql` and are migrated, but are not modelled yet because nothing uses them.

Column types mirror the verified DDL exactly — including the CHECK constraints and partial
unique indexes that carry the product's integrity guarantees. Where SQLAlchemy can express a
constraint, it does; the Alembic migration is generated from these models and diffed against
`schema.sql` in CI so drift is impossible to merge.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.stage_graph import ProjectStage


class Base(DeclarativeBase):
    """Declarative base. `type_annotation_map` keeps JSON/JSONB identical across dialects."""

    # NOTE: `list` is deliberately NOT mapped here. Doing so silently turned the
    # `integer[]` column `preferred_durations_ms` into JSONB and failed at INSERT time.
    # Generic dicts are JSONB; array columns declare ARRAY(...) explicitly.
    type_annotation_map: ClassVar[dict] = {
        dict: JSONB,
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ----------------------------------------------------------------------------- identity
user_status_enum = Enum("active", "suspended", "deleted", name="user_status", create_type=False)
workspace_role_enum = Enum(
    "owner", "admin", "editor", "viewer", name="workspace_role", create_type=False
)
source_kind_enum = Enum(
    "upload", "youtube", "cloud_storage", name="source_kind", create_type=False
)
source_state_enum = Enum(
    "pending", "validating", "ready", "rejected", "deleted", name="source_state", create_type=False
)
media_asset_kind_enum = Enum(
    "source_video",
    "audio_master",
    "audio_asr",
    "proxy_preview",
    "scene_thumbnail",
    "frame_sample",
    "clip_video",
    "clip_thumbnail",
    "caption_ass",
    "caption_vtt",
    "thumbnail_image",
    "qa_frame",
    "render_log",
    name="media_asset_kind",
    create_type=False,
)
job_status_enum = Enum(
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "RETRYING",
    "CANCELLED",
    "DEAD_LETTER",
    name="job_status",
    create_type=False,
)
publish_mode_enum = Enum(
    "auto", "manual_approval", "private_review", name="publish_mode", create_type=False
)
notification_kind_enum = Enum(
    "analysis_complete", "clips_ready", "qa_failed", "approval_needed", "publish_success",
    "publish_failed", "auth_expired", "budget_warning", "storage_warning", "system",
    name="notification_kind", create_type=False,
)

project_status_enum = Enum(
    *(s.value for s in ProjectStage), name="project_status", create_type=False
)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(user_status_enum, nullable=False, server_default="active")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # `workspace_members` has two FKs to users (user_id, invited_by), so the join column must be
    # stated explicitly — otherwise SQLAlchemy raises AmbiguousForeignKeysError.
    memberships: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="WorkspaceMember.user_id",
    )


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    default_language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    target_audience: Mapped[str | None] = mapped_column(Text)
    budget_usd_monthly: Mapped[float | None] = mapped_column(Numeric(12, 6))
    storage_quota_mb: Mapped[int | None] = mapped_column(Integer)
    settings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(workspace_role_enum, nullable=False, server_default="editor")
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id", name="workspace_members_workspace_id_user_id_key"
        ),
        Index("ix_wm_user", "user_id"),
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(
        back_populates="memberships", foreign_keys=[user_id]
    )


class Session(Base):
    """Server-side session. Only the SHA-256 digest of the cookie token is stored."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True, nullable=False)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_sessions_user", "user_id", postgresql_where=text("revoked_at IS NULL")),
    )


# ----------------------------------------------------------------------------- projects
class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        project_status_enum, nullable=False, server_default="CREATED"
    )
    target_platform: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="youtube_shorts"
    )
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    clips_requested: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    min_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="15000")
    max_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="65000")
    preferred_durations_ms: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{15000,30000,45000}'")
    )
    template_key: Mapped[str] = mapped_column(Text, nullable=False, server_default="podcast")
    caption_style_key: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="bold_center"
    )
    branding: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    publishing_mode: Mapped[str] = mapped_column(
        publish_mode_enum, nullable=False, server_default="manual_approval"
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    schedule_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    budget_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    progress: Mapped[int | None] = mapped_column(SmallInteger)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("clips_requested BETWEEN 1 AND 30", name="clips_requested_range"),
        CheckConstraint("min_duration_ms >= 3000", name="min_duration_floor"),
        CheckConstraint("max_duration_ms <= 180000", name="max_duration_ceiling"),
        CheckConstraint("min_duration_ms < max_duration_ms", name="ck_project_durations"),
        Index(
            "ix_projects_ws_status",
            "workspace_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_projects_created",
            "workspace_id",
            text("created_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    source: Mapped[SourceVideo | None] = relationship(
        back_populates="project", uselist=False, cascade="all, delete-orphan"
    )
    stage_events: Mapped[list[ProjectStageEvent]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectStageEvent.id"
    )


class ProjectStageEvent(Base):
    """Append-only journal of every state transition."""

    __tablename__ = "project_stage_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(project_status_enum)
    to_status: Mapped[str] = mapped_column(project_status_enum, nullable=False)
    task_name: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    progress: Mapped[int | None] = mapped_column(SmallInteger)
    error_code: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_pse_project_at", "project_id", text("at DESC")),)

    project: Mapped[Project] = relationship(back_populates="stage_events")


class SourceVideo(Base, TimestampMixin):
    __tablename__ = "source_videos"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(source_kind_enum, nullable=False)
    state: Mapped[str] = mapped_column(source_state_enum, nullable=False, server_default="pending")
    original_filename: Mapped[str | None] = mapped_column(Text)
    detected_mime: Mapped[str | None] = mapped_column(Text)
    # NOTE: the attribute is `byte_size` (not `bytes`) on purpose — see the comment on
    # MediaAsset.byte_size. The database column is still named `bytes`.
    byte_size: Mapped[int | None] = mapped_column("bytes", BigInteger)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    storage_key: Mapped[str | None] = mapped_column(Text)
    youtube_video_id: Mapped[str | None] = mapped_column(Text)
    youtube_metadata: Mapped[dict | None] = mapped_column(JSONB)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Numeric(8, 3))
    video_codec: Mapped[str | None] = mapped_column(Text)
    audio_codec: Mapped[str | None] = mapped_column(Text)
    audio_channels: Mapped[int | None] = mapped_column(SmallInteger)
    audio_sample_rate: Mapped[int | None] = mapped_column(Integer)
    probe_json: Mapped[dict | None] = mapped_column(JSONB)

    # Compliance: required before ingestion proceeds (enforced by RightsDeclaration + service).
    rights_basis: Mapped[str | None] = mapped_column(Text)
    rights_statement: Mapped[str | None] = mapped_column(Text)
    rights_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rights_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "kind <> 'youtube' OR youtube_video_id IS NOT NULL", name="ck_source_youtube"
        ),
        CheckConstraint("kind <> 'upload' OR storage_key IS NOT NULL", name="ck_source_upload"),
        UniqueConstraint("id", "project_id", name="uq_source_id_project"),
        Index("ix_sources_project", "project_id"),
        Index(
            "ux_sources_sha_project",
            "project_id",
            "sha256",
            unique=True,
            postgresql_where=text("sha256 IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    project: Mapped[Project] = relationship(back_populates="source")


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    clip_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(media_asset_kind_enum, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # The DB column is `bytes`, but a Python attribute of that name shadows the builtin
    # `bytes` while annotations are evaluated, so `Mapped[bytes | None]` would resolve the
    # builtin to a SQLAlchemy type. Attribute renamed; column name preserved.
    byte_size: Mapped[int | None] = mapped_column("bytes", BigInteger)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    mime: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_assets_project_kind", "project_id", "kind"),)


class ProcessingJob(Base, TimestampMixin):
    """Durable index of background work; written before a task is enqueued."""

    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    clip_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    queue: Mapped[str] = mapped_column(Text, nullable=False)
    task_name: Mapped[str] = mapped_column(Text, nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(job_status_enum, nullable=False, server_default="PENDING")
    progress: Mapped[int | None] = mapped_column(SmallInteger)
    progress_note: Mapped[str | None] = mapped_column(Text)
    input_fingerprint: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="3")
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_jobs_project", "project_id", text("created_at DESC")),
        Index("ix_jobs_status", "status", "next_retry_at"),
        Index(
            "ux_jobs_idempotent",
            "project_id",
            "stage",
            "input_fingerprint",
            unique=True,
            postgresql_where=text(
                "input_fingerprint IS NOT NULL AND status IN "
                "('PENDING','RUNNING','RETRYING','SUCCEEDED')"
            ),
        ),
        Index(
            "ix_jobs_heartbeat",
            "status",
            "heartbeat_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )


class AuditLog(Base):
    """Append-only. No UPDATE/DELETE grants for the application role in production."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_audit_ws_at", "workspace_id", text("at DESC")),
        Index("ix_audit_action", "action"),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(notification_kind_enum, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    clip_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_notif_user_unread",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("read_at IS NULL"),
        ),
    )


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    clip_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL")
    )
    category: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[float] = mapped_column(Numeric(16, 4), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    unit_cost_usd: Mapped[float] = mapped_column(Numeric(12, 8), nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_usage_ws_at", "workspace_id", text("at DESC")),
        Index("ix_usage_project", "project_id"),
    )
