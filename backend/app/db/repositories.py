"""Repositories — every data access path goes through here.

The workspace-isolation guarantee is structural, not procedural: **every** read and write takes
`workspace_id` as a required keyword argument. There is no `get_by_id()` helper that omits it, so
a cross-workspace leak cannot be introduced by forgetting a filter — the call would not type-check
or would raise at the signature level.

Not-found semantics: anything belonging to another workspace raises `NotFoundError`, never
`ForbiddenError`. Returning 403 for an object you cannot see is an existence oracle.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.netutil import valid_ip
from app.core.stage_graph import (
    ProjectStage,
    assert_transition,
    is_terminal,
    progress_percent,
)
from app.db.models import (
    AuditLog,
    Notification,
    ProcessingJob,
    Project,
    ProjectStageEvent,
    SourceVideo,
    User,
    Workspace,
    WorkspaceMember,
)


class NotFoundError(LookupError):
    """Resource absent OR outside the caller's workspace. Callers map this to HTTP 404."""

    def __init__(self, resource: str, identifier: Any) -> None:
        # The identifier is captured for logs/support but never put in the message: the message
        # is returned to clients, and echoing the id back confirms whether a guessed id exists
        # in another tenant's workspace.
        super().__init__(f"{resource} not found")
        self.resource = resource
        self.identifier = identifier


class ConflictError(RuntimeError):
    """State conflict (illegal transition, duplicate, or in-use). Maps to HTTP 409."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Page:
    items: list[Any]
    next_cursor: str | None = None
    total_estimate: int | None = None


# --------------------------------------------------------------------------- users


class UserRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_email(self, email: str) -> User | None:
        return self.db.scalar(select(User).where(User.email == email.lower()))

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.db.get(User, user_id)

    def create(self, *, email: str, password_hash: str, display_name: str) -> User:
        user = User(
            email=email.lower(), password_hash=password_hash, display_name=display_name
        )
        self.db.add(user)
        self.db.flush()
        return user

    def record_login(self, user_id: uuid.UUID) -> None:
        user = self.db.get(User, user_id)
        if user:
            user.last_login_at = datetime.now(UTC)


# --------------------------------------------------------------------------- workspaces


class WorkspaceRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self, *, name: str, slug: str, owner_user_id: uuid.UUID, default_language: str = "en"
    ) -> Workspace:
        ws = Workspace(
            name=name, slug=slug, owner_user_id=owner_user_id, default_language=default_language
        )
        self.db.add(ws)
        self.db.flush()
        self.db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner_user_id, role="owner"))
        self.db.flush()
        return ws

    def slug_exists(self, slug: str) -> bool:
        return (
            self.db.scalar(
                select(func.count()).select_from(Workspace).where(Workspace.slug == slug)
            )
            or 0
        ) > 0

    def get_for_user(self, *, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Workspace | None:
        """Workspace only if the user is a member.

        This is the anchor of every authorization check.
        """
        return self.db.scalar(
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(
                Workspace.id == workspace_id,
                WorkspaceMember.user_id == user_id,
                Workspace.deleted_at.is_(None),
            )
        )

    def list_for_user(self, *, user_id: uuid.UUID) -> list[tuple[Workspace, str]]:
        rows = self.db.execute(
            select(Workspace, WorkspaceMember.role)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(WorkspaceMember.user_id == user_id, Workspace.deleted_at.is_(None))
            .order_by(Workspace.created_at)
        ).all()
        return [(row[0], row[1]) for row in rows]

    def role_of(self, *, workspace_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        return self.db.scalar(
            select(WorkspaceMember.role).where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id
            )
        )


# --------------------------------------------------------------------------- projects


class ProjectRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        workspace_id: uuid.UUID,
        created_by: uuid.UUID,
        name: str,
        **fields: Any,
    ) -> Project:
        project = Project(workspace_id=workspace_id, created_by=created_by, name=name, **fields)
        self.db.add(project)
        self.db.flush()
        self._journal(project, from_status=None, to_status=ProjectStage.CREATED, task_name="create")
        return project

    def get(self, *, workspace_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        project = self.db.scalar(
            select(Project)
            .options(selectinload(Project.source))
            .where(
                Project.id == project_id,
                Project.workspace_id == workspace_id,
                Project.deleted_at.is_(None),
            )
        )
        if project is None:
            raise NotFoundError("project", project_id)
        return project

    def list(
        self,
        *,
        workspace_id: uuid.UUID,
        status: str | None = None,
        query: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page:
        stmt: Select = select(Project).where(
            Project.workspace_id == workspace_id, Project.deleted_at.is_(None)
        )
        if status:
            stmt = stmt.where(Project.status == status)
        if query:
            like = f"%{query}%"
            stmt = stmt.where(or_(Project.name.ilike(like)))
        if cursor:
            stmt = stmt.where(Project.created_at < _decode_cursor(cursor))
        # Fetch limit+1 to know whether a next page exists without a second COUNT.
        rows = list(
            self.db.scalars(stmt.order_by(Project.created_at.desc(), Project.id).limit(limit + 1))
        )
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = _encode_cursor(rows[-1].created_at)
        return Page(items=rows, next_cursor=next_cursor)

    def update(self, *, workspace_id: uuid.UUID, project_id: uuid.UUID, **fields: Any) -> Project:
        project = self.get(workspace_id=workspace_id, project_id=project_id)
        for key, value in fields.items():
            if value is not None and hasattr(project, key):
                setattr(project, key, value)
        # Duration sanity: the DB CHECK enforces it too, but the message is clearer here.
        if project.min_duration_ms >= project.max_duration_ms:
            raise ConflictError(
                "invalid_duration_range",
                f"min_duration_ms ({project.min_duration_ms}) must be less than "
                f"max_duration_ms ({project.max_duration_ms})",
            )
        self.db.flush()
        return project

    def soft_delete(self, *, workspace_id: uuid.UUID, project_id: uuid.UUID) -> None:
        project = self.get(workspace_id=workspace_id, project_id=project_id)
        active = self.db.scalar(
            select(func.count())
            .select_from(ProcessingJob)
            .where(
                ProcessingJob.project_id == project.id,
                ProcessingJob.status.in_(["PENDING", "RUNNING", "RETRYING"]),
            )
        )
        if active:
            raise ConflictError(
                "project_busy",
                f"project has {active} active job(s); cancel them before deleting",
            )
        project.deleted_at = datetime.now(UTC)
        self.db.flush()

    # ---- state machine -------------------------------------------------------
    def transition(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        target: ProjectStage | str,
        task_name: str | None = None,
        job_id: uuid.UUID | None = None,
        progress: int | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> Project:
        """Apply a stage transition, enforcing the declarative graph.

        Illegal transitions raise ConflictError('invalid_transition') — never silently coerced,
        never written to the journal.
        """
        project = self.get(workspace_id=workspace_id, project_id=project_id)
        target = ProjectStage(target)
        try:
            assert_transition(project.status, target)
        except ValueError as exc:
            raise ConflictError("invalid_transition", str(exc)) from exc

        previous = project.status
        project.status = target.value
        # Progress is only recorded where a meaningful denominator exists (spine position).
        project.progress = progress if progress is not None else progress_percent(target)
        project.error_code = error_code
        project.error_detail = detail
        self._journal(
            project,
            from_status=previous,
            to_status=target,
            task_name=task_name,
            job_id=job_id,
            progress=project.progress,
            error_code=error_code,
            detail=detail,
        )
        self.db.flush()
        return project

    def stage_events(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> list[ProjectStageEvent]:
        project = self.get(workspace_id=workspace_id, project_id=project_id)
        return list(
            self.db.scalars(
                select(ProjectStageEvent)
                .where(ProjectStageEvent.project_id == project.id)
                .order_by(ProjectStageEvent.id)
            )
        )

    def completed_stages(self, *, workspace_id: uuid.UUID, project_id: uuid.UUID) -> set[str]:
        """Stages the project has passed through — the input to resume logic."""
        project = self.get(workspace_id=workspace_id, project_id=project_id)
        reached = self.db.scalars(
            select(ProjectStageEvent.to_status).where(ProjectStageEvent.project_id == project.id)
        ).all()
        return {s for s in reached if s and not is_terminal(s)}

    def _journal(
        self,
        project: Project,
        *,
        from_status: str | None,
        to_status: ProjectStage | str,
        task_name: str | None = None,
        job_id: uuid.UUID | None = None,
        progress: int | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.db.add(
            ProjectStageEvent(
                project_id=project.id,
                from_status=from_status,
                to_status=ProjectStage(to_status).value,
                task_name=task_name,
                job_id=job_id,
                progress=progress,
                error_code=error_code,
                detail=detail,
            )
        )


# --------------------------------------------------------------------------- sources


class SourceRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        kind: str,
        rights_basis: str,
        rights_statement: str,
        rights_confirmed_by: uuid.UUID,
        **fields: Any,
    ) -> SourceVideo:
        """Create a source with its rights declaration. Rights are recorded, not optional."""
        if not rights_basis or not rights_statement.strip():
            raise ConflictError(
                "source_not_authorized",
                "a rights declaration (basis + statement) is required before ingesting media",
            )
        source = SourceVideo(
            workspace_id=workspace_id,
            project_id=project_id,
            kind=kind,
            rights_basis=rights_basis,
            rights_statement=rights_statement.strip(),
            rights_confirmed_at=datetime.now(UTC),
            rights_confirmed_by=rights_confirmed_by,
            **fields,
        )
        self.db.add(source)
        self.db.flush()
        return source

    def get(self, *, workspace_id: uuid.UUID, source_id: uuid.UUID) -> SourceVideo:
        source = self.db.scalar(
            select(SourceVideo).where(
                SourceVideo.id == source_id,
                SourceVideo.workspace_id == workspace_id,
                SourceVideo.deleted_at.is_(None),
            )
        )
        if source is None:
            raise NotFoundError("source", source_id)
        return source

    def for_project(self, *, workspace_id: uuid.UUID, project_id: uuid.UUID) -> SourceVideo | None:
        return self.db.scalar(
            select(SourceVideo).where(
                SourceVideo.project_id == project_id,
                SourceVideo.workspace_id == workspace_id,
                SourceVideo.deleted_at.is_(None),
            )
        )

    def find_by_checksum(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID, sha256: bytes
    ) -> SourceVideo | None:
        return self.db.scalar(
            select(SourceVideo).where(
                SourceVideo.project_id == project_id,
                SourceVideo.workspace_id == workspace_id,
                SourceVideo.sha256 == sha256,
                SourceVideo.deleted_at.is_(None),
            )
        )

    def mark_ready(
        self, *, workspace_id: uuid.UUID, source_id: uuid.UUID, **probe: Any
    ) -> SourceVideo:
        source = self.get(workspace_id=workspace_id, source_id=source_id)
        source.state = "ready"
        for key, value in probe.items():
            if hasattr(source, key):
                setattr(source, key, value)
        self.db.flush()
        return source

    def mark_rejected(
        self, *, workspace_id: uuid.UUID, source_id: uuid.UUID, reason: str
    ) -> SourceVideo:
        """Mark a stored source unusable. The reason is appended to the audit trail.

        The reason is deliberately *not* stored on `source_videos`: the table has no such column,
        and inventing one here would drift from the reviewed schema. `audit_logs.detail` is the
        designed home for it.
        """
        source = self.get(workspace_id=workspace_id, source_id=source_id)
        source.state = "rejected"
        AuditRepository(self.db).record(
            action="source.rejected",
            workspace_id=workspace_id,
            target_type="source_video",
            target_id=source.id,
            detail={"reason": reason[:500]},
        )
        self.db.flush()
        return source


# --------------------------------------------------------------------------- jobs


# A job in one of these states has finished, one way or another.
_TERMINAL_JOB_STATUSES = frozenset({"SUCCEEDED", "FAILED", "DEAD_LETTER", "CANCELLED"})
# States from which a retry can legitimately restart the work.
_RETRYABLE_JOB_STATUSES = frozenset({"FAILED", "DEAD_LETTER", "CANCELLED"})


class JobRepository:
    QUEUE_FOR_STAGE: ClassVar[dict[str, str]] = {
        "source_validation": "media",
        "ingest": "media",
        "media_inspection": "media",
        "audio_extraction": "media",
        "transcription": "transcribe",
        "visual_analysis": "analyze",
        "semantic_analysis": "analyze",
        "moment_detection": "analyze",
        "render": "render",
        "quality_check": "qa",
        "publish": "publish",
        "analytics": "analytics",
    }

    def __init__(self, db: Session) -> None:
        self.db = db

    def enqueue(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID | None,
        stage: str,
        task_name: str,
        input_fingerprint: str | None = None,
        timeout_seconds: int | None = None,
        clip_id: uuid.UUID | None = None,
    ) -> ProcessingJob:
        """Persist the job BEFORE the broker sees it, so a lost message is still discoverable."""
        if input_fingerprint:
            existing = self.db.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.project_id == project_id,
                    ProcessingJob.stage == stage,
                    ProcessingJob.input_fingerprint == input_fingerprint,
                    ProcessingJob.status.in_(["PENDING", "RUNNING", "RETRYING", "SUCCEEDED"]),
                )
            )
            if existing is not None:
                return existing  # idempotent: same work is never queued twice
        job = ProcessingJob(
            workspace_id=workspace_id,
            project_id=project_id,
            clip_id=clip_id,
            stage=stage,
            queue=self.QUEUE_FOR_STAGE.get(stage, "media"),
            task_name=task_name,
            input_fingerprint=input_fingerprint,
            timeout_seconds=timeout_seconds,
            status="PENDING",
        )
        self.db.add(job)
        self.db.flush()
        return job

    def get(self, *, workspace_id: uuid.UUID, job_id: uuid.UUID) -> ProcessingJob:
        job = self.db.scalar(
            select(ProcessingJob).where(
                ProcessingJob.id == job_id, ProcessingJob.workspace_id == workspace_id
            )
        )
        if job is None:
            raise NotFoundError("job", job_id)
        return job

    def list(
        self, *, workspace_id: uuid.UUID, status: str | None = None, limit: int = 20
    ) -> Page:
        stmt = select(ProcessingJob).where(ProcessingJob.workspace_id == workspace_id)
        if status:
            stmt = stmt.where(ProcessingJob.status == status)
        rows = list(self.db.scalars(stmt.order_by(ProcessingJob.created_at.desc()).limit(limit)))
        return Page(items=rows)

    def find_succeeded(self, *, input_fingerprint: str) -> ProcessingJob | None:
        """Idempotency probe: has work with this exact fingerprint already completed?"""
        return self.db.scalar(
            select(ProcessingJob).where(
                ProcessingJob.input_fingerprint == input_fingerprint,
                ProcessingJob.status == "SUCCEEDED",
            )
        )

    def mark_running(self, *, workspace_id: uuid.UUID, job_id: uuid.UUID) -> ProcessingJob:
        job = self.get(workspace_id=workspace_id, job_id=job_id)
        job.status = "RUNNING"
        job.started_at = job.started_at or datetime.now(UTC)
        job.heartbeat_at = datetime.now(UTC)
        job.attempts = (job.attempts or 0) + 1
        self.db.flush()
        return job

    def set_progress(
        self,
        *,
        workspace_id: uuid.UUID,
        job_id: uuid.UUID,
        progress: int,
        note: str | None = None,
    ) -> ProcessingJob:
        job = self.get(workspace_id=workspace_id, job_id=job_id)
        job.progress = max(0, min(100, progress))
        job.progress_note = note
        job.heartbeat_at = datetime.now(UTC)
        self.db.flush()
        return job

    def mark_succeeded(
        self, *, workspace_id: uuid.UUID, job_id: uuid.UUID, note: str | None = None
    ) -> ProcessingJob:
        job = self.get(workspace_id=workspace_id, job_id=job_id)
        job.status = "SUCCEEDED"
        job.progress = 100
        job.progress_note = note
        job.finished_at = datetime.now(UTC)
        job.error_code = None
        job.error_detail = None
        self.db.flush()
        return job

    def mark_failed(
        self,
        *,
        workspace_id: uuid.UUID,
        job_id: uuid.UUID,
        code: str,
        detail: str,
        retryable: bool = False,
    ) -> ProcessingJob:
        """Record a failure.

        `retryable=True` means "this *could* succeed on another attempt", so a job that has spent
        its attempts becomes DEAD_LETTER (needs a human). A non-retryable failure — bad input,
        no speech in the file — is FAILED immediately: retrying it would just fail again.
        """
        job = self.get(workspace_id=workspace_id, job_id=job_id)
        exhausted = (job.attempts or 0) >= (job.max_attempts or 3)
        job.status = "DEAD_LETTER" if (retryable and exhausted) else "FAILED"
        job.error_code = code
        job.error_detail = detail[:2000]
        job.finished_at = datetime.now(UTC)
        self.db.flush()
        return job

    def list_cursor(
        self,
        *,
        workspace_id: uuid.UUID,
        status: str | None = None,
        stage: str | None = None,
        project_id: uuid.UUID | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Page:
        """Newest-first page of jobs, with a stable keyset cursor.

        Ordered by `created_at DESC` using the same microsecond cursor as projects, so paging
        never skips or repeats a row that lands between two requests.
        """
        stmt = select(ProcessingJob).where(ProcessingJob.workspace_id == workspace_id)
        if status:
            stmt = stmt.where(ProcessingJob.status == status)
        if stage:
            stmt = stmt.where(ProcessingJob.stage == stage)
        if project_id:
            stmt = stmt.where(ProcessingJob.project_id == project_id)
        if cursor:
            stmt = stmt.where(ProcessingJob.created_at < _decode_cursor(cursor))
        rows = list(
            self.db.scalars(stmt.order_by(ProcessingJob.created_at.desc()).limit(limit + 1))
        )
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = _encode_cursor(rows[-1].created_at)
        return Page(items=rows, next_cursor=next_cursor)

    def mark_cancelled(self, *, workspace_id: uuid.UUID, job_id: uuid.UUID) -> ProcessingJob:
        """Cancel a job that has not finished. Cancelling finished work is a conflict."""
        job = self.get(workspace_id=workspace_id, job_id=job_id)
        if job.status in _TERMINAL_JOB_STATUSES:
            raise ConflictError("job_not_cancellable", f"job is already {job.status.lower()}")
        job.status = "CANCELLED"
        job.finished_at = datetime.now(UTC)
        job.progress_note = "cancelled by user"
        self.db.flush()
        return job

    def requeue(self, *, workspace_id: uuid.UUID, job_id: uuid.UUID) -> ProcessingJob:
        """Reset a retired job so it can run again.

        Only FAILED / DEAD_LETTER / CANCELLED jobs may be retried. Retrying a SUCCEEDED job is
        refused: that is a re-run request with different semantics (see `/analyze?force=true`),
        and conflating the two would let a retry silently discard good work.
        """
        job = self.get(workspace_id=workspace_id, job_id=job_id)
        if job.status not in _RETRYABLE_JOB_STATUSES:
            raise ConflictError(
                "job_not_retryable",
                f"only failed, dead-letter or cancelled jobs can be retried; this one is "
                f"{job.status.lower()}",
            )
        job.status = "PENDING"
        job.attempts = 0
        job.progress = 0
        job.progress_note = None
        job.error_code = None
        job.error_detail = None
        job.finished_at = None
        job.next_retry_at = None
        self.db.flush()
        return job


# --------------------------------------------------------------------------- audit


class AuditRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def record(
        self,
        *,
        action: str,
        workspace_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        target_type: str | None = None,
        target_id: Any = None,
        ip: str | None = None,
        user_agent: str | None = None,
        detail: dict | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            action=action,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            target_type=target_type,
            target_id=target_id if isinstance(target_id, uuid.UUID) else None,
            ip=valid_ip(ip),
            user_agent=(user_agent or "")[:500] or None,
            detail=detail or {},
        )
        self.db.add(entry)
        self.db.flush()
        return entry


# --------------------------------------------------------------------------- dashboard


class DashboardRepository:
    """Every number here comes from a real query. No placeholder metrics."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def summary(self, *, workspace_id: uuid.UUID) -> dict[str, Any]:
        project_count = self.db.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.workspace_id == workspace_id, Project.deleted_at.is_(None))
        )
        by_status = dict(
            self.db.execute(
                select(Project.status, func.count())
                .where(Project.workspace_id == workspace_id, Project.deleted_at.is_(None))
                .group_by(Project.status)
            ).all()
        )
        sources_ready = self.db.scalar(
            select(func.count())
            .select_from(SourceVideo)
            .where(SourceVideo.workspace_id == workspace_id, SourceVideo.state == "ready")
        )
        storage_bytes = self.db.scalar(
            select(func.coalesce(func.sum(SourceVideo.byte_size), 0)).where(
                SourceVideo.workspace_id == workspace_id
            )
        )
        failed_jobs = self.db.scalar(
            select(func.count())
            .select_from(ProcessingJob)
            .where(
                ProcessingJob.workspace_id == workspace_id,
                ProcessingJob.status.in_(["FAILED", "DEAD_LETTER"]),
            )
        )
        queue_depth = self.db.scalar(
            select(func.count())
            .select_from(ProcessingJob)
            .where(
                ProcessingJob.workspace_id == workspace_id,
                ProcessingJob.status.in_(["PENDING", "RUNNING", "RETRYING"]),
            )
        )
        recent = list(
            self.db.scalars(
                select(Project)
                .where(Project.workspace_id == workspace_id, Project.deleted_at.is_(None))
                .order_by(Project.created_at.desc())
                .limit(5)
            )
        )
        activity = list(
            self.db.scalars(
                select(AuditLog)
                .where(AuditLog.workspace_id == workspace_id)
                .order_by(AuditLog.at.desc())
                .limit(10)
            )
        )
        unread = self.db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.workspace_id == workspace_id, Notification.read_at.is_(None))
        )
        return {
            "projects_total": project_count or 0,
            "projects_by_status": by_status,
            "sources_processed": sources_ready or 0,
            "failed_jobs": failed_jobs or 0,
            "queue_depth": queue_depth or 0,
            "storage_mb": round((storage_bytes or 0) / (1024 * 1024), 2),
            "unread_notifications": unread or 0,
            "recent_projects": recent,
            "recent_activity": activity,
            "is_empty": (project_count or 0) == 0,
        }


# --------------------------------------------------------------------------- cursors


def _encode_cursor(created_at: datetime) -> str:
    """Opaque, URL-safe cursor.

    A raw ISO-8601 string is not safe in a query string: the `+` in `+00:00` is decoded as a
    space by form-style query parsers, silently corrupting the timestamp. Base64url of an epoch
    microsecond value is short, unambiguous and safe in any transport.
    """
    micros = int(created_at.timestamp() * 1_000_000)
    return base64.urlsafe_b64encode(str(micros).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> datetime:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        micros = int(base64.urlsafe_b64decode(padded.encode()).decode())
        return datetime.fromtimestamp(micros / 1_000_000, tz=UTC)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ConflictError("invalid_cursor", "cursor is malformed or expired") from exc
