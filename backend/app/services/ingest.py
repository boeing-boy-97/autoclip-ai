"""Source ingestion: validate, store, probe, and advance the project state machine.

The order is deliberate and fail-fast:
  rights → project state → stream to storage (with size cap + checksum) → probe → persist facts.

A probe failure deletes the stored object and marks the source rejected, so a bad upload never
leaves orphaned bytes or a project stuck mid-pipeline. The project advances
CREATED → SOURCE_VALIDATION → INGESTING → MEDIA_INSPECTION using the same declarative graph the
API and tests use — there is no second code path for "internal" transitions.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.stage_graph import ProjectStage
from app.db.models import Project, SourceVideo
from app.db.repositories import (
    AuditRepository,
    ConflictError,
    NotFoundError,
    ProjectRepository,
    SourceRepository,
)
from app.services.media_probe import (
    MediaValidationError,
    ProbeResult,
    probe_video,
    validate_extension,
    validate_magic,
    validate_mime,
)
from app.services.storage import Storage, StorageError, build_key


class IngestError(Exception):
    def __init__(self, code: str, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


@dataclass(slots=True)
class IngestResult:
    source: SourceVideo
    probe: ProbeResult


class IngestService:
    def __init__(self, db: Session, settings: Settings, storage: Storage) -> None:
        self.db = db
        self.settings = settings
        self.storage = storage
        self.projects = ProjectRepository(db)
        self.sources = SourceRepository(db)
        self.audit = AuditRepository(db)

    # ------------------------------------------------------------------ public
    def ingest_upload(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        filename: str,
        declared_mime: str | None,
        spool_path: str,
        rights_basis: str,
        rights_statement: str,
    ) -> IngestResult:
        project = self.projects.get(workspace_id=workspace_id, project_id=project_id)
        self._assert_can_accept_source(project)
        self._assert_rights(rights_basis, rights_statement)

        try:
            ext = validate_extension(filename)
            validate_mime(declared_mime)
        except MediaValidationError as exc:
            raise IngestError(exc.code, exc.detail, status_code=415) from exc

        # ---- container signature must match the extension before we store anything
        with open(spool_path, "rb") as sniff:  # noqa: PTH123
            try:
                validate_magic(sniff.read(4096), ext)
            except MediaValidationError as exc:
                raise IngestError(exc.code, exc.detail, status_code=415) from exc

        # Opened fresh for the copy: the sniff above consumed the first 4 KiB, and streaming from
        # that same handle would store a file with its header missing — corrupt bytes that still
        # look plausible in the metadata.
        with open(spool_path, "rb") as fh:  # noqa: PTH123
            # ---- stream into storage, enforcing the size cap and computing the checksum
            max_bytes = self.settings.max_upload_mb * 1024 * 1024
            key = build_key(
                workspace_id=workspace_id, project_id=project_id, kind="sources", ext=ext
            )
            try:
                stored = self.storage.put_stream(
                    key=key, chunks=_file_chunks(fh), max_bytes=max_bytes
                )
            except StorageError as exc:
                message = str(exc)
                code = "payload_too_large" if "limit" in message else "storage_unavailable"
                status_code = 413 if code == "payload_too_large" else 503
                raise IngestError(code, message, status_code=status_code) from exc

        # ---- real media inspection
        hexdigest = stored.sha256
        sha256 = bytes.fromhex(hexdigest)
        try:
            probe = self._probe_path(spool_path)
        except MediaValidationError as exc:
            self.storage.delete(key)
            self._audit_rejection(
                project=project,
                user_id=user_id,
                filename=filename,
                code=exc.code,
                reason=exc.detail,
                byte_size=stored.bytes,
                sha256=hexdigest,
            )
            # Persist the refusal trail even though the request fails, then fail the request.
            # The project is deliberately left untouched: a rejected file is a client error, and
            # burning the project (FAILED is absorbing) after one wrong upload would force the
            # user to start over. No source row is created either, so an immediate retry works.
            self.db.commit()
            raise IngestError(exc.code, exc.detail, status_code=415) from exc

        source = self.sources.create(
            workspace_id=workspace_id,
            project_id=project_id,
            kind="upload",
            rights_basis=rights_basis,
            rights_statement=rights_statement,
            rights_confirmed_by=user_id,
            state="ready",
            original_filename=filename[:255],
            detected_mime=(declared_mime or "")[:120] or None,
            byte_size=stored.bytes,
            sha256=sha256,
            storage_key=key,
            duration_ms=probe.duration_ms,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            video_codec=probe.video_codec,
            audio_codec=probe.audio_codec,
            audio_channels=probe.audio_channels,
            audio_sample_rate=probe.audio_sample_rate,
            probe_json=probe.raw,
        )
        self._journ_ingest(project, source)
        self.audit.record(
            action="source.upload",
            workspace_id=workspace_id,
            actor_user_id=user_id,
            target_type="source_video",
            target_id=source.id,
            detail={
                "bytes": stored.bytes,
                "sha256": stored.sha256,
                "duration_ms": probe.duration_ms,
                "rights_basis": rights_basis,
            },
        )
        self.db.flush()
        return IngestResult(source=source, probe=probe)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _assert_rights(basis: str, statement: str) -> None:
        allowed = {"original", "licensed", "permission", "creative_commons", "owned_channel"}
        if basis not in allowed:
            raise IngestError(
                "source_not_authorized",
                f"rights basis must be one of {sorted(allowed)}",
                status_code=422,
            )
        if not statement or len(statement.strip()) < 10:
            raise IngestError(
                "source_not_authorized",
                "a rights statement is required before media can be ingested",
                status_code=422,
            )

    def _assert_can_accept_source(self, project: Project) -> None:
        if project.status not in {ProjectStage.CREATED, ProjectStage.SOURCE_VALIDATION}:
            raise ConflictError(
                "project_already_has_source",
                f"project is in {project.status}; a source can only be attached while it is "
                "CREATED or SOURCE_VALIDATION",
            )
        if self.sources.for_project(workspace_id=project.workspace_id, project_id=project.id):
            raise ConflictError(
                "project_already_has_source", "this project already has a source video"
            )

    def _probe_path(self, path: str) -> ProbeResult:
        """Inspect the uploaded bytes with ffprobe (protocol-restricted, time-limited)."""
        return probe_video(
            Path(path),
            ffprobe_bin=self.settings.ffprobe_bin,
            max_duration_ms=self.settings.max_source_duration_minutes * 60 * 1000,
        )

    def _audit_rejection(
        self,
        *,
        project: Project,
        user_id: uuid.UUID,
        filename: str,
        code: str,
        reason: str,
        byte_size: int,
        sha256: str | None = None,
    ) -> None:
        """Record a refused upload: who tried, what the file was, and why it was refused.

        No `source_videos` row is written on purpose — a project holds one source, so a rejected
        row would block the user's retry with a 409.
        """
        self.audit.record(
            action="source.rejected",
            workspace_id=project.workspace_id,
            actor_user_id=user_id,
            target_type="project",
            target_id=project.id,
            detail={
                "code": code,
                "reason": reason[:500],
                "filename": filename[:255],
                "bytes": byte_size,
                "sha256": sha256,
            },
        )
        self.db.flush()

    def _journ_ingest(self, project: Project, source: SourceVideo) -> None:
        """Walk the legal path CREATED → SOURCE_VALIDATION → INGESTING → MEDIA_INSPECTION."""
        path = [
            ProjectStage.SOURCE_VALIDATION,
            ProjectStage.INGESTING,
            ProjectStage.MEDIA_INSPECTION,
        ]
        for stage in path:
            if project.status == stage.value:
                continue
            task = {
                ProjectStage.SOURCE_VALIDATION: "validate_source",
                ProjectStage.INGESTING: "store_source",
                ProjectStage.MEDIA_INSPECTION: "inspect_media",
            }[stage]
            self.projects.transition(
                workspace_id=project.workspace_id,
                project_id=project.id,
                target=stage,
                task_name=task,
                detail=f"source {source.id}",
            )


def _file_chunks(fh: IO[bytes], size: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield 1 MiB chunks from an open file — constant memory regardless of video size."""
    while True:
        block = fh.read(size)
        if not block:
            return
        yield block


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:  # noqa: PTH123
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def now() -> datetime:
    return datetime.now(UTC)


__all__ = ["IngestError", "IngestResult", "IngestService", "NotFoundError", "sha256_file"]
