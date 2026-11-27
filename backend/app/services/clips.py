"""Clip generation and rendering orchestration.

Two steps, deliberately separate:

1. `generate_clips` — materialise `clips` rows from the selected candidate moments. Nothing is
   rendered yet. This is cheap, idempotent, and lets a user see and edit the cut list before
   spending minutes of CPU.
2. `render_clip` — cut, reframe and caption one clip into a new `clip_versions` row.

Rendering is append-only: every render creates a new version with its own plan, hash and asset.
Nothing overwrites a previous file, so a bad render can be compared with the good one instead of
silently replacing it.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.locks import guard_project, project_execution
from app.db.models import (
    CandidateMoment,
    Clip,
    ClipVersion,
    MediaAsset,
    SourceVideo,
    Transcript,
    TranscriptSegment,
)
from app.db.repositories import ClipRepository, JobRepository, ProjectRepository
from app.services.captions import CaptionTrack, Word, build_track, write_captions
from app.services.render import (
    RenderError,
    Template,
    build_plan,
    nudge_to_word_gap,
    probe_source_size,
)
from app.services.render import (
    render_clip as run_ffmpeg,
)
from app.services.storage import Storage, build_key

logger = logging.getLogger(__name__)

# Only clips in these states may be re-rendered without an explicit force. A rendered clip that
# someone approved should not be silently replaced.
# A rejected clip is re-renderable: the rejection is what grants permission to replace the file.
_RERENDERABLE_STATES = frozenset({"DRAFT", "QA_FAILED", "REJECTED"})
# States from which "reject" makes no sense: nothing to look at, or already refused.
_UNREJECTABLE_STATES = frozenset({"DRAFT", "RENDERING", "REJECTED", "ARCHIVED"})


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class ClipError(RuntimeError):
    """A clip operation failed with a machine-readable code."""

    def __init__(self, code: str, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


@dataclass(slots=True)
class GenerateOutcome:
    created: int
    existing: int
    clip_ids: list[uuid.UUID]


@dataclass(slots=True)
class RenderOutcome:
    clip_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    width: int
    height: int
    duration_ms: int
    bytes: int
    has_captions: bool
    cue_count: int
    render_seconds: float


class ClipService:
    def __init__(self, db: Session, settings: Settings, storage: Storage) -> None:
        self.db = db
        self.settings = settings
        self.storage = storage
        self.projects = ProjectRepository(db)
        self.jobs = JobRepository(db)
        self.clips = ClipRepository(db)

    # ------------------------------------------------------------------ generation
    def generate(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,  # noqa: ARG002 - recorded by the API layer, not needed here
    ) -> GenerateOutcome:
        """Create one clip per selected moment. Idempotent: existing clips are left alone."""
        guard_project(self.db, workspace_id, project_id)
        project = self.projects.get(workspace_id=workspace_id, project_id=project_id)
        source = self.db.scalar(
            select(SourceVideo).where(
                SourceVideo.project_id == project_id, SourceVideo.state == "ready"
            )
        )
        if source is None:
            raise ClipError(
                "source_not_ready",
                "this project has no ready source video; upload one before generating clips",
                status_code=409,
            )

        moments = list(
            self.db.scalars(
                select(CandidateMoment)
                .where(CandidateMoment.project_id == project_id, CandidateMoment.selected.is_(True))
                .order_by(CandidateMoment.start_ms)
            )
        )
        if not moments:
            raise ClipError(
                "no_candidates",
                "this project has no selected moments; run analysis first",
                status_code=409,
            )

        existing_by_moment = {
            c.candidate_moment_id: c
            for c in self.db.scalars(select(Clip).where(Clip.project_id == project_id))
            if c.candidate_moment_id is not None
        }

        created_ids: list[uuid.UUID] = []
        existing = 0
        for moment in moments:
            if moment.id in existing_by_moment:
                existing += 1
                continue
            clip = Clip(
                project_id=project_id,
                workspace_id=workspace_id,
                source_video_id=source.id,
                candidate_moment_id=moment.id,
                title=_title_for(moment),
                start_ms=moment.start_ms,
                end_ms=moment.end_ms,
                duration_ms=moment.end_ms - moment.start_ms,
                state="DRAFT",
                layout="single_subject",
                template_key=project.template_key or "podcast",
                meta={
                    "generated_from": "candidate_moment",
                    "category": moment.category,
                    "rationale": moment.rationale,
                },
            )
            self.db.add(clip)
            self.db.flush()
            created_ids.append(clip.id)

        logger.info(
            "clips generated",
            extra={
                "project_id": str(project_id),
                # `created` is a reserved LogRecord attribute; logging with it raises KeyError and
                # would turn a successful generate into a 500 after the rows were already written.
                "created_count": len(created_ids),
                "existing_count": existing,
            },
        )
        return GenerateOutcome(created=len(created_ids), existing=existing, clip_ids=created_ids)

    # ------------------------------------------------------------------ rendering
    def render(
        self,
        *,
        workspace_id: uuid.UUID,
        clip_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        template_key: str | None = None,
        job_id: uuid.UUID | None = None,
    ) -> RenderOutcome:
        clip = self.clips.get(workspace_id=workspace_id, clip_id=clip_id)
        with project_execution(workspace_id, clip.project_id) as acquired:
            if not acquired:
                raise ClipError(
                    "project_busy", "project work is already executing", status_code=409
                )
            # A clip loaded before waiting/acquiring must not carry a stale version/state.
            self.db.refresh(clip)
            result = self._render(
                workspace_id=workspace_id,
                clip_id=clip_id,
                user_id=user_id,
                template_key=template_key,
                job_id=job_id,
            )
            self.db.commit()
            return result

    def _render(
        self,
        *,
        workspace_id: uuid.UUID,
        clip_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        template_key: str | None = None,
        job_id: uuid.UUID | None = None,
    ) -> RenderOutcome:
        clip = self.clips.get(workspace_id=workspace_id, clip_id=clip_id)
        if clip.state not in _RERENDERABLE_STATES:
            raise ClipError(
                "clip_not_renderable",
                f"clip is {clip.state.lower()}; re-rendering a reviewed clip requires an explicit "
                "regenerate",
                status_code=409,
            )

        try:
            template = Template.load(template_key or clip.template_key, self.settings.template_dir)
        except RenderError as exc:
            # A missing template is a bad request, not a server fault.
            raise ClipError(exc.code, exc.detail, status_code=422) from exc
        source = self.db.get(SourceVideo, clip.source_video_id)
        if source is None or not source.storage_key:
            raise ClipError(
                "source_not_ready", "the source video for this clip is missing", status_code=409
            )

        scratch = Path(self.settings.tmp_root) / f"render-{clip_id}-{uuid.uuid4().hex[:8]}"
        scratch.mkdir(parents=True, exist_ok=True)
        job_ref = job_id
        if job_ref is None:
            job = self.jobs.enqueue(
                workspace_id=workspace_id,
                project_id=clip.project_id,
                clip_id=clip_id,
                stage="render",
                task_name="clips.render",
            )
            job_ref = job.id
            self.db.commit()

        try:
            self.jobs.mark_running(workspace_id=workspace_id, job_id=job_ref)
            clip.state = "RENDERING"
            self.db.flush()

            source_path = self._materialise_source(source, scratch)
            self.jobs.set_progress(
                workspace_id=workspace_id, job_id=job_ref, progress=15, note="cutting and reframing"
            )

            # Resolve the cut first, then caption it. Building captions from the pre-nudge window
            # and rendering the post-nudge window would put both files on slightly different
            # timelines — a cue could start before the picture does.
            words = self._words_in_window(clip)
            start_ms, end_ms, adjustments = nudge_to_word_gap(clip.start_ms, clip.end_ms, words)
            if adjustments.get("reason"):
                logger.info("clip boundaries adjusted", extra={"clip_id": str(clip_id)})

            track = self._caption_track(clip, template, start_ms=start_ms, end_ms=end_ms)
            ass_path, _vtt_path = write_captions(
                track,
                out_dir=scratch,
                width=template.width,
                height=template.height,
                font_family=str(template.caption_style.get("font_family", "DejaVu Sans")),
                font_size_px=int(template.caption_style.get("font_size_px", 64)),
                text_color=str(template.caption_style.get("text_color", "#FFFFFF")),
                outline_px=int(template.caption_style.get("outline_px", 6)),
                position=str(template.caption_style.get("position", "lower_third")),
                max_lines=template.max_lines,
            )

            src_w, src_h = probe_source_size(source_path, self.settings.ffprobe_bin)
            plan = build_plan(
                clip_id=str(clip_id),
                template=template,
                start_ms=start_ms,
                end_ms=end_ms,
                cue_count=len(track.cues),
                source_width=src_w,
                source_height=src_h,
                adjustments=adjustments,
                warnings=list(track.warnings),
            )

            self.jobs.set_progress(
                workspace_id=workspace_id, job_id=job_ref, progress=35, note="encoding"
            )
            output = scratch / "clip.mp4"
            result = run_ffmpeg(
                source_path=source_path,
                output_path=output,
                plan=plan,
                ass_path=ass_path if track.has_captions else None,
                ffmpeg_bin=self.settings.ffmpeg_bin,
                ffprobe_bin=self.settings.ffprobe_bin,
                target_lufs=self.settings.audio_target_lufs if template.audio_loudnorm else None,
            )

            self.jobs.set_progress(
                workspace_id=workspace_id, job_id=job_ref, progress=85, note="storing assets"
            )
            video_asset = self._store_asset(
                clip=clip,
                kind="clip_video",
                path=output,
                mime="video/mp4",
                duration_ms=result.duration_ms,
                width=result.width,
                height=result.height,
            )
            caption_asset = (
                self._store_asset(
                    clip=clip,
                    kind="caption_ass",
                    path=ass_path,
                    mime="text/plain",
                    duration_ms=None,
                    width=None,
                    height=None,
                )
                if track.has_captions
                else None
            )

            version_no = self._next_version_no(clip_id)
            version = ClipVersion(
                clip_id=clip_id,
                version_no=version_no,
                render_plan=plan.to_dict(),
                render_plan_version=1,
                video_asset_id=video_asset.id,
                caption_asset_id=caption_asset.id if caption_asset else None,
                width=result.width,
                height=result.height,
                duration_ms=result.duration_ms,
                bytes=result.bytes,
                sha256=bytes.fromhex(result.sha256),
                render_seconds=result.render_seconds,
                encoder_log_ref=result.encoder_log[-2000:] if result.encoder_log else None,
                source="initial",
                created_by=user_id,
                rendered_at=func.now(),
            )
            self.db.add(version)
            self.db.flush()

            clip.current_version_id = version.id
            clip.start_ms = start_ms
            clip.end_ms = end_ms
            clip.duration_ms = end_ms - start_ms
            # Encoding checks are not the independent QA/approval gate. Until that gate
            # exists, renders are reviewable/downloadable but never automatically publishable.
            clip.state = "AWAITING_REVIEW"
            clip.publishable = False
            clip.meta = {
                **(clip.meta or {}),
                "captions": {
                    "burned": track.has_captions,
                    "cue_count": len(track.cues),
                    "coverage": round(track.coverage, 4),
                    "words_in_range": track.words_in_range,
                    "words_without_timing": track.words_without_timing,
                    "warnings": track.warnings,
                },
                "render_plan_version": 1,
            }
            self.jobs.mark_succeeded(
                workspace_id=workspace_id,
                job_id=job_ref,
                note=f"rendered {result.width}x{result.height} in {result.render_seconds}s",
            )
            self.db.flush()

            return RenderOutcome(
                clip_id=clip_id,
                version_id=version.id,
                version_no=version_no,
                width=result.width,
                height=result.height,
                duration_ms=result.duration_ms,
                bytes=result.bytes,
                has_captions=track.has_captions,
                cue_count=len(track.cues),
                render_seconds=result.render_seconds,
            )

        except RenderError as exc:
            self._fail(job_ref, workspace_id, clip, exc.code, exc.detail)
            raise ClipError(exc.code, exc.detail, status_code=422) from exc
        except ClipError:
            raise
        except Exception as exc:
            self._fail(job_ref, workspace_id, clip, "render_failed", str(exc)[:500])
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # ------------------------------------------------------------------ helpers
    def reject(
        self,
        *,
        workspace_id: uuid.UUID,
        clip_id: uuid.UUID,
        reason: str,
        user_id: uuid.UUID | None = None,
    ) -> Clip:
        """Record that a human looked at this clip and refused it.

        A rejection is not a re-render. It is the *permission* to re-render: without it a reviewed
        clip is frozen, because silently overwriting a file somebody already judged would destroy
        the record of what they judged. The rejected version stays in `clip_versions` forever.
        """
        clip = self.clips.get(workspace_id=workspace_id, clip_id=clip_id)
        guard_project(self.db, workspace_id, clip.project_id)
        self.db.refresh(clip)
        if clip.state in _UNREJECTABLE_STATES:
            raise ClipError(
                "clip_not_rejectable",
                f"a clip in {clip.state.lower()} cannot be rejected",
                status_code=409,
            )
        if not reason or not reason.strip():
            # A rejection with no reason cannot be acted on, so it is not accepted.
            raise ClipError(
                "reason_required",
                "a rejection must say what was wrong with the clip",
                status_code=422,
            )
        clip.state = "REJECTED"
        clip.rejection_reason = reason.strip()[:2000]
        clip.publishable = False
        clip.meta = {
            **(clip.meta or {}),
            "rejected_at": _now_iso(),
            "rejected_by": str(user_id) if user_id else None,
        }
        self.db.flush()
        logger.info("clip rejected", extra={"clip_id": str(clip_id)})
        return clip

    def restore_version(
        self,
        *,
        workspace_id: uuid.UUID,
        clip_id: uuid.UUID,
        version_id: uuid.UUID,
        user_id: uuid.UUID | None = None,  # noqa: ARG002 - audit attribution, not used yet
    ) -> Clip:
        """Point `current_version_id` back at an earlier render.

        No re-encode happens: every version is immutable and its file is still in storage, so
        restoring is a pointer move. That is the whole reason versions are append-only.
        """
        clip = self.clips.get(workspace_id=workspace_id, clip_id=clip_id)
        guard_project(self.db, workspace_id, clip.project_id)
        self.db.refresh(clip)
        version = self.clips.get_version(
            workspace_id=workspace_id, clip_id=clip_id, version_id=version_id
        )
        if clip.current_version_id == version.id:
            raise ClipError(
                "already_current", "that version is already the current one", status_code=409
            )
        previous = clip.current_version_id
        clip.current_version_id = version.id
        clip.state = "AWAITING_REVIEW" if version.video_asset_id else clip.state
        clip.publishable = False
        clip.meta = {
            **(clip.meta or {}),
            "restored_from_version_id": str(previous) if previous else None,
            "restored_to_version_no": version.version_no,
        }
        self.db.flush()
        logger.info(
            "clip version restored",
            extra={"clip_id": str(clip_id), "version_no": version.version_no},
        )
        return clip

    def _fail(
        self,
        job_id: uuid.UUID,
        workspace_id: uuid.UUID,
        clip: Clip,
        code: str,
        detail: str,
    ) -> None:
        """Record a render failure without masking the original error."""
        with contextlib.suppress(Exception):
            self.db.rollback()
        try:
            self.jobs.mark_failed(
                workspace_id=workspace_id, job_id=job_id, code=code, detail=detail, retryable=True
            )
            fresh = self.db.get(Clip, clip.id)
            if fresh is not None:
                fresh.state = "QA_FAILED"
                fresh.meta = {**(fresh.meta or {}), "last_render_error": detail[:2000]}
            self.db.commit()
        except Exception:  # noqa: BLE001
            self.db.rollback()

    def _caption_track(
        self, clip: Clip, template: Template, *, start_ms: int, end_ms: int
    ) -> CaptionTrack:
        segments = self._segments_for(clip)
        return build_track(
            segments,
            clip_start_ms=start_ms,
            clip_end_ms=end_ms,
            max_words_per_cue=template.max_words_per_cue,
            max_lines=template.max_lines,
        )

    def _segments_for(self, clip: Clip) -> list[dict]:
        transcript = self.db.scalar(
            select(Transcript)
            .where(Transcript.source_video_id == clip.source_video_id)
            .order_by(Transcript.created_at.desc())
            .limit(1)
        )
        if transcript is None:
            return []
        rows = self.db.scalars(
            select(TranscriptSegment)
            .where(
                TranscriptSegment.transcript_id == transcript.id,
                TranscriptSegment.end_ms > clip.start_ms,
                TranscriptSegment.start_ms < clip.end_ms,
            )
            .order_by(TranscriptSegment.idx)
        )
        return [
            {"idx": r.idx, "start_ms": r.start_ms, "end_ms": r.end_ms, "words": r.words}
            for r in rows
        ]

    def _words_in_window(self, clip: Clip) -> list[Word]:
        out: list[Word] = []
        for seg in self._segments_for(clip):
            for entry in seg.get("words") or []:
                if not isinstance(entry, dict):
                    continue
                s, e = entry.get("s"), entry.get("e")
                if isinstance(s, int) and isinstance(e, int) and e > s:
                    out.append(Word(text=str(entry.get("w", "")), start_ms=s, end_ms=e))
        out.sort(key=lambda w: (w.start_ms, w.end_ms))
        return out

    def _materialise_source(self, source: SourceVideo, scratch: Path) -> Path:
        key = source.storage_key
        if not key:
            raise ClipError(
                "source_not_ready",
                "the source video has no stored object to render from",
                status_code=409,
            )
        from app.services.storage import LocalStorage  # noqa: PLC0415

        if isinstance(self.storage, LocalStorage):
            return self.storage._path(key)
        target = scratch / "source.mp4"
        with self.storage.open(key) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        return target

    def _next_version_no(self, clip_id: uuid.UUID) -> int:
        current = self.db.scalar(
            select(func.max(ClipVersion.version_no)).where(ClipVersion.clip_id == clip_id)
        )
        return int(current or 0) + 1

    def _store_asset(
        self,
        *,
        clip: Clip,
        kind: str,
        path: Path,
        mime: str,
        duration_ms: int | None,
        width: int | None,
        height: int | None,
    ) -> MediaAsset:
        key = build_key(
            workspace_id=clip.workspace_id,
            project_id=clip.project_id,
            kind=kind,
            ext=path.suffix,
        )
        with path.open("rb") as fh:
            stored = self.storage.put_stream(
                key=key,
                chunks=iter(lambda: fh.read(1024 * 1024), b""),
                max_bytes=max(self.settings.max_upload_mb, 512) * 1024 * 1024,
            )
        asset = MediaAsset(
            workspace_id=clip.workspace_id,
            project_id=clip.project_id,
            clip_id=clip.id,
            kind=kind,
            storage_key=stored.key,
            byte_size=stored.bytes,
            sha256=bytes.fromhex(stored.sha256),
            mime=mime,
            duration_ms=duration_ms,
            width=width,
            height=height,
            meta={"derived_from_clip": str(clip.id)},
        )
        self.db.add(asset)
        self.db.flush()
        return asset


def _title_for(moment: CandidateMoment) -> str:
    """A human label derived from the moment's own text. Never invented copy."""
    for candidate in (moment.hook_text, moment.payoff_text):
        text = (candidate or "").strip()
        if text:
            return text[:120]
    return f"{moment.category.replace('_', ' ').title()} moment"
