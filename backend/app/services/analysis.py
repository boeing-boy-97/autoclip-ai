"""The analysis pipeline: audio extraction → transcription → candidate detection → scoring.

Design notes
------------
* **Idempotent re-runs.** Every stage first looks for completed work keyed by an input fingerprint
  (source sha256 + provider + model + config version). Re-running an analysis returns the existing
  result instead of duplicating rows or re-billing an ASR call.
* **Resumable.** Stages advance the project through the declarative graph, so a crash leaves the
  journal pointing at the last completed stage; re-entry skips forward from there via
  `resume_stage` rather than restarting from scratch.
* **Failures are recorded, not hidden.** An ASR failure marks the job FAILED with the engine's own
  message and leaves the project in a state a human can act on. Nothing is ever filled in with
  invented content to make the pipeline look successful.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.stage_graph import PIPELINE_ORDER, TERMINAL_STAGES, ProjectStage
from app.db.models import (
    CandidateMoment,
    CandidateScore,
    MediaAsset,
    Project,
    Scene,
    Speaker,
    Transcript,
    TranscriptSegment,
    VideoAnalysis,
)
from app.db.repositories import (
    AuditRepository,
    ConflictError,
    JobRepository,
    ProjectRepository,
    SourceRepository,
)
from app.services.scoring import (
    CandidateWindow,
    ScoreResult,
    SegmentView,
    build_windows,
    categorise,
    deduplicate,
    load_scoring_config,
    score_moment,
)
from app.services.storage import Storage, build_key
from app.services.transcription import (
    AsrFailedError,
    AsrUnavailableError,
    TranscriptionResult,
    extract_asr_audio,
    transcribe,
)
from app.services.visual import VisualAnalysisError, VisualReport, analyse_frames


@dataclass(slots=True)
class AnalysisPlan:
    """The decision made *before* any work starts.

    Either an identical run already exists (`reused=True`, nothing to do) or a `processing_jobs`
    row has been created and committed (`job_id`), ready to be dispatched. Keeping this separate
    from execution lets the API reject an illegal request with a precise error *before* talking to
    the broker, instead of enqueuing something that will fail minutes later.
    """

    job_id: uuid.UUID | None
    reused: bool
    transcript_id: uuid.UUID | None = None
    fingerprint: str = ""


@dataclass(slots=True)
class AnalysisOutcome:
    transcript_id: uuid.UUID
    candidates: list[CandidateMoment]
    scores: list[ScoreResult]
    windows: list[CandidateWindow]
    reused: bool
    elapsed_seconds: float


# Project states from which analysis may run (or re-run). MEDIA_INSPECTION is the normal entry
# point after ingestion; the rest allow a repeat of an earlier completed analysis.
_REPEATABLE_STATUSES = frozenset(
    {
        ProjectStage.MEDIA_INSPECTION.value,
        ProjectStage.AUDIO_EXTRACTION.value,
        ProjectStage.TRANSCRIPTION.value,
        ProjectStage.VISUAL_ANALYSIS.value,
        ProjectStage.SEMANTIC_ANALYSIS.value,
        ProjectStage.MOMENT_DETECTION.value,
    }
)


# States in which analysis has already run: a repeat request should report, not restart.
_ANALYSIS_DONE_STATUSES = frozenset({ProjectStage.MOMENT_DETECTION.value})

# Absorbing states a job retry must be able to leave. FAILED and CANCELLED are terminal for the
# *project* because nothing resumes them automatically -- but an explicit retry is not automation,
# and refusing it would make the retry endpoint useless for the only case anyone calls it in.
_RESUMABLE_STATUSES = frozenset({ProjectStage.FAILED.value, ProjectStage.CANCELLED.value})


class AnalysisError(Exception):
    """A pipeline failure with a machine-readable code and the HTTP status it implies.

    `to_dict` exists because this exception crosses a process boundary when Celery reports it:
    without a stable representation Celery can only pickle the class reference, and the worker log
    ends up full of `UnpickleableExceptionWrapper(...)` instead of the actual failure reason.
    """

    def __init__(self, code: str, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "detail": self.detail, "status_code": self.status_code}

    @classmethod
    def from_dict(cls, data: dict) -> AnalysisError:
        return cls(
            str(data.get("code", "analysis_failed")),
            str(data.get("detail", "analysis failed")),
            status_code=int(data.get("status_code", 422)),
        )

    def __reduce__(self) -> tuple:
        # Celery pickles exceptions to report them. Reduce to plain JSON-safe primitives so a
        # redelivered or re-raised error never depends on import order in another process.
        return (_rebuild_analysis_error, (self.to_dict(),))


def _rebuild_analysis_error(data: dict) -> AnalysisError:
    return AnalysisError.from_dict(data)


class AnalysisService:
    def __init__(self, db: Session, settings: Settings, storage: Storage) -> None:
        self.db = db
        self.settings = settings
        self.storage = storage
        self.projects = ProjectRepository(db)
        self.sources = SourceRepository(db)
        self.jobs = JobRepository(db)
        self.audit = AuditRepository(db)

    # ------------------------------------------------------------------ entry point
    def plan(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        force: bool = False,
    ) -> AnalysisPlan:
        """Validate the request and reserve a job, without running any pipeline work.

        Raises `AnalysisError` for every reason the request cannot legally run: no ready source, a
        project beyond the analysis stages. Callers can therefore surface a precise 4xx *before*
        enqueuing anything, rather than enqueueing a request doomed to fail minutes later.
        """
        project = self.projects.get(workspace_id=workspace_id, project_id=project_id)
        source = self.sources.for_project(workspace_id=workspace_id, project_id=project_id)
        if source is None or source.state != "ready":
            raise AnalysisError(
                "source_not_ready",
                "this project has no ready source video; upload one before analysing",
                status_code=409,
            )

        config = load_scoring_config(self.settings.scoring_config_path)
        # A forced re-run must bypass the idempotency key, so the fingerprint is salted. Without
        # this, `force=true` was accepted by the API and then silently ignored.
        fingerprint = self._fingerprint(
            source.sha256, config.version, salt=uuid.uuid4().hex if force else None
        )

        # MOMENT_DETECTION is the end of the analysis band, so an earlier completed run may be
        # repeated. Any state beyond it means real work exists downstream of analysis and repeating
        # it would discard results, so it is refused explicitly.
        if project.status not in _REPEATABLE_STATUSES and not force:
            raise AnalysisError(
                "analysis_not_repeatable",
                f"project is in {project.status}; analysis can only run while the project is "
                f"within the analysis stages ({', '.join(sorted(_REPEATABLE_STATUSES))})",
                status_code=409,
            )

        existing = self._existing_transcript(source.id, fingerprint, force=force)
        if existing is None and not force and project.status in _ANALYSIS_DONE_STATUSES:
            existing = self._latest_transcript(source.id)
        if existing is not None:
            return AnalysisPlan(
                job_id=None, reused=True, transcript_id=existing.id, fingerprint=fingerprint
            )

        # `enqueue` is idempotent on the fingerprint: a copy already PENDING/RUNNING is returned
        # rather than duplicated, so an impatient client cannot pay for two transcriptions.
        job = self.jobs.enqueue(
            workspace_id=workspace_id,
            project_id=project_id,
            stage="transcription",
            task_name="analysis.run",
            input_fingerprint=fingerprint,
        )
        # Commit before dispatching: if the process dies between commit and enqueue, the job is
        # still discoverable as PENDING rather than vanishing without trace.
        self.db.commit()
        return AnalysisPlan(job_id=job.id, reused=False, fingerprint=fingerprint)

    def analyse(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        force: bool = False,
        job_id: uuid.UUID | None = None,
    ) -> AnalysisOutcome:
        """Run the pipeline for an already-reserved job.

        `job_id` is supplied when a Celery task drives the work: the API created and committed
        that `processing_jobs` row before dispatching, so creating another one here would leave an
        orphan PENDING job behind on every run. When it is None (the synchronous entry point) this
        method reserves its own job first.
        """
        started = time.monotonic()
        project = self.projects.get(workspace_id=workspace_id, project_id=project_id)
        source = self.sources.for_project(workspace_id=workspace_id, project_id=project_id)
        if source is None or source.state != "ready":
            raise AnalysisError(
                "source_not_ready",
                "this project has no ready source video; upload one before analysing",
                status_code=409,
            )

        config = load_scoring_config(self.settings.scoring_config_path)
        # A forced re-run must bypass the idempotency key, so the fingerprint is salted. Without
        # this, `force=true` was accepted by the API and then silently ignored — the worst kind of
        # bug, because the caller believes they re-ran the work.
        fingerprint = self._fingerprint(
            source.sha256, config.version, salt=uuid.uuid4().hex if force else None
        )

        # A retry carries an explicit job the user asked to re-run, so it may resume a project
        # that is otherwise absorbing. This must happen BEFORE the gate below, or the gate
        # rejects the retry before the resume ever runs.
        if job_id is not None and project.status in _RESUMABLE_STATUSES:
            self._resume(project)
            project = self.projects.get(workspace_id=workspace_id, project_id=project_id)

        # MOMENT_DETECTION is the end of the analysis band, so an earlier completed run may be
        # repeated. Any state beyond it means real work exists downstream of analysis and repeating
        # it would discard results, so it is refused explicitly.
        if project.status not in _REPEATABLE_STATUSES and not force:
            raise AnalysisError(
                "analysis_not_repeatable",
                f"project is in {project.status}; analysis can only run while the project is "
                f"within the analysis stages ({', '.join(sorted(_REPEATABLE_STATUSES))})",
                status_code=409,
            )

        # Idempotency: an identical successful run already exists.
        existing = self._existing_transcript(source.id, fingerprint, force=force)
        if existing is None and not force and project.status in _ANALYSIS_DONE_STATUSES:
            # Nothing to redo and no legal forward transition: report the existing work rather
            # than failing a project that is already in a valid terminal-for-analysis state.
            existing = self._latest_transcript(source.id)
        if existing is not None:
            if job_id is not None:
                # An adopted job must never be left dangling in PENDING just because the work
                # turned out to be already done. Close it with an honest note.
                self._finish_job(job_id, workspace_id, note="existing transcript reused")
                self.db.commit()
            candidates, scores, windows = self._load_candidates(project_id)
            return AnalysisOutcome(
                transcript_id=existing.id,
                candidates=candidates,
                scores=scores,
                windows=windows,
                reused=True,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )

        if job_id is not None:
            job_ref = job_id  # reserved by plan()/the API and already committed
        else:
            job = self.jobs.enqueue(
                workspace_id=workspace_id,
                project_id=project_id,
                stage="transcription",
                task_name="analysis.run",
                input_fingerprint=fingerprint,
            )
            job_ref = job.id
            # Commit before doing the work: if the run fails, the failure-recording path starts
            # with a rollback, which would otherwise discard this very job row and leave no trace
            # at all.
            self.db.commit()
        scratch = Path(self.settings.tmp_root) / f"analysis-{project_id}-{uuid.uuid4().hex[:8]}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            self._mark_running(job_ref, workspace_id)
            self._enter(project, ProjectStage.AUDIO_EXTRACTION, task="extract_audio")

            source_path = self._materialise_source(source, scratch)
            audio_path = extract_asr_audio(
                source_path,
                scratch / "asr.wav",
                ffmpeg_bin=self.settings.ffmpeg_bin,
                target_lufs=self.settings.audio_target_lufs,
            )
            audio_key = self._store_asset(
                project=project,
                source_id=source.id,
                kind="audio_asr",
                path=audio_path,
                mime="audio/wav",
                duration_ms=source.duration_ms,
            )

            self._enter(project, ProjectStage.TRANSCRIPTION, task="transcribe")
            self._set_progress(job_ref, workspace_id, 35, "transcribing audio")
            result = transcribe(
                audio_path,
                settings=self.settings,
                language=project.language if project.language != "auto" else None,
                duration_ms=source.duration_ms,
            )
            transcript = self._persist_transcript(
                project=project,
                source=source,
                result=result,
                raw_audio_key=audio_key,
                replace=force,
            )

            # Audio analysis happens before visual analysis in the reviewed stage graph, so the
            # picture pass records its own scene rows here rather than being skipped.
            self._enter(project, ProjectStage.VISUAL_ANALYSIS, task="analyse_frames")
            self._set_progress(job_ref, workspace_id, 55, "detecting scenes and measuring frames")
            visual = self._run_visual(project=project, source_path=source_path, source=source)

            self._enter(project, ProjectStage.SEMANTIC_ANALYSIS, task="score_candidates")
            self._set_progress(job_ref, workspace_id, 75, "detecting and scoring candidate moments")
            candidates, scores, windows = self._detect_and_score(
                project=project,
                transcript=transcript,
                config=config,
                visual_quality=visual.metrics.get("visual_quality_score"),
            )

            self._enter(
                project,
                ProjectStage.MOMENT_DETECTION,
                task="select_candidates",
                detail=f"{len(candidates)} candidate moments",
            )
            self.projects.update(
                workspace_id=workspace_id,
                project_id=project_id,
                cost_usd=float(project.cost_usd or 0) + 0.0,  # local ASR is free; hosted adds cost
            )
            self.audit.record(
                action="analysis.completed",
                workspace_id=workspace_id,
                actor_user_id=user_id,
                target_type="project",
                target_id=project_id,
                detail={
                    "transcript_id": str(transcript.id),
                    "provider": result.provider,
                    "model": result.model,
                    "language": result.language,
                    "segments": len(result.segments),
                    "words": result.word_count,
                    "candidates": len(candidates),
                    "weights_version": config.version,
                    "asr_seconds": result.elapsed_seconds,
                },
            )
            self._finish_job(job_ref, workspace_id, note=f"{len(candidates)} candidates")
            self.db.flush()
            return AnalysisOutcome(
                transcript_id=transcript.id,
                candidates=candidates,
                scores=scores,
                windows=windows,
                reused=False,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
        except (AsrUnavailableError, AsrFailedError) as exc:
            self._record_failure(
                job_ref,
                workspace_id,
                project,
                exc.code,
                exc.detail,
                # The ASR engine being unavailable is an operator problem that may be fixed;
                # finding no speech in the file will not change on a retry.
                retryable=isinstance(exc, AsrUnavailableError),
            )
            raise AnalysisError(
                exc.code,
                exc.detail,
                status_code=503 if isinstance(exc, AsrUnavailableError) else 422,
            ) from exc
        except Exception as exc:
            self._record_failure(
                job_ref,
                workspace_id,
                project,
                "analysis_failed",
                str(exc)[:500],
                retryable=True,  # an unexpected error is exactly the kind worth re-attempting
            )
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # ------------------------------------------------------------------ stages
    def _materialise_source(self, source, scratch: Path) -> Path:  # noqa: ANN001
        """Fetch the stored object to local scratch if the storage backend is remote."""
        local = Path(self.settings.local_storage_root) / (source.storage_key or "")
        if source.storage_key and local.exists():
            return local
        if not source.storage_key:
            raise AnalysisError(
                "source_not_ready", "source has no stored object to analyse", status_code=409
            )
        target = scratch / "source"
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.storage.open(source.storage_key) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        return target

    def _store_asset(
        self,
        *,
        project: Project,
        source_id: uuid.UUID | None,
        kind: str,
        path: Path,
        mime: str,
        duration_ms: int | None,
    ) -> str:
        key = build_key(
            workspace_id=project.workspace_id,
            project_id=project.id,
            kind=kind,
            ext=path.suffix,
        )
        with path.open("rb") as fh:
            stored = self.storage.put_stream(
                key=key,
                chunks=iter(lambda: fh.read(1024 * 1024), b""),
                max_bytes=self.settings.max_upload_mb * 1024 * 1024,
            )
        self.db.add(
            MediaAsset(
                workspace_id=project.workspace_id,
                project_id=project.id,
                kind=kind,
                storage_key=stored.key,
                byte_size=stored.bytes,
                sha256=bytes.fromhex(stored.sha256),
                mime=mime,
                duration_ms=duration_ms,
                meta={"derived_from_source": str(source_id) if source_id else None},
            )
        )
        self.db.flush()
        return stored.key

    def _persist_transcript(
        self,
        *,
        project: Project,
        source,  # noqa: ANN001
        result: TranscriptionResult,
        raw_audio_key: str,
        replace: bool = False,
    ) -> Transcript:
        if replace:
            # A forced re-run supersedes the previous result rather than accumulating duplicates.
            # Order matters, because the schema enforces exactly one live transcript per
            # (source, provider, model, is_corrected) and one speaker per (project, label):
            # children first, then parents.
            self.db.execute(
                delete(TranscriptSegment).where(TranscriptSegment.project_id == project.id)
            )
            self.db.execute(
                delete(Transcript).where(
                    Transcript.source_video_id == source.id,
                    Transcript.is_corrected.is_(False),
                )
            )
            self.db.execute(delete(Speaker).where(Speaker.project_id == project.id))
            self.db.flush()

        transcript = Transcript(
            project_id=project.id,
            source_video_id=source.id,
            provider=result.provider,
            model=result.model,
            language=result.language,
            word_timestamps=result.word_timestamps,
            confidence=_opt_float(result.confidence),
            cost_usd=0,
            raw_json={
                **result.raw,
                "audio_asset_key": raw_audio_key,
                "elapsed_seconds": result.elapsed_seconds,
            },
        )
        self.db.add(transcript)
        self.db.flush()

        # A single speaker label unless diarization is configured. We never invent multiple
        # speakers: 'method' records that this is the no-diarization case.
        speaker = Speaker(
            project_id=project.id,
            label="Speaker 1",
            method="default_single",
            confidence=None,
            color="#4C6EF5",
        )
        self.db.add(speaker)
        self.db.flush()

        for seg in result.segments:
            self.db.add(
                TranscriptSegment(
                    transcript_id=transcript.id,
                    project_id=project.id,
                    idx=seg.idx,
                    start_ms=seg.start_ms,
                    end_ms=seg.end_ms,
                    text=seg.text,
                    speaker_id=speaker.id,
                    avg_logprob=_opt_float(seg.avg_logprob),
                    no_speech_prob=_opt_float(seg.no_speech_prob),
                    words=[w.as_dict() for w in seg.words] if seg.words else None,
                )
            )
        self.db.flush()
        return transcript

    def _detect_and_score(  # noqa: ANN202 - returns three related collections
        self,
        *,
        project: Project,
        transcript: Transcript,
        config,  # noqa: ANN001
        visual_quality: float | None = None,
    ):
        segments = list(
            self.db.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.transcript_id == transcript.id)
                .order_by(TranscriptSegment.idx)
            )
        )
        if not segments:
            raise AnalysisError(
                "no_speech_detected", "the transcript contains no segments to analyse"
            )

        views = [
            SegmentView(idx=s.idx, start_ms=s.start_ms, end_ms=s.end_ms, text=s.text)
            for s in segments
        ]
        windows = build_windows(
            views,
            target_min_ms=project.min_duration_ms,
            target_max_ms=project.max_duration_ms,
            config=config,
        )
        if not windows:
            raise AnalysisError(
                "no_candidates",
                "the transcript is too short to form a clip within the configured duration band "
                f"({project.min_duration_ms}-{project.max_duration_ms} ms)",
            )

        scored = [
            (
                w,
                score_moment(
                    text=w.text,
                    duration_ms=w.duration_ms,
                    config=config,
                    has_speech=True,
                    # Only a real measurement is passed; None keeps it explicitly "unknown".
                    visual_quality=visual_quality,
                    method="heuristic",
                ),
            )
            for w in windows
        ]
        ranked = deduplicate(
            scored,
            max_overlap=float(config.diversity.get("max_overlap_iou", 0.15)),
            min_gap_ms=int(config.diversity.get("min_gap_between_clips_ms", 3000)),
        )

        # Clear previous candidates for a re-run so this stage stays idempotent.
        self.db.execute(delete(CandidateMoment).where(CandidateMoment.project_id == project.id))

        candidates: list[CandidateMoment] = []
        scores: list[ScoreResult] = []
        wanted = project.clips_requested
        kept = 0
        for window, score, suppressed in ranked:
            first = window.segments[0].text.strip()
            last = window.segments[-1].text.strip()
            moment = CandidateMoment(
                project_id=project.id,
                start_ms=window.start_ms,
                end_ms=window.end_ms,
                required_duration_ms=window.duration_ms,
                category=categorise(window.text, config.enabled_categories),
                hook_text=first[:500],
                context_text=window.text[:2000],
                payoff_text=last[:500],
                ending_text=last[:500],
                rationale=(
                    f"Score {score.overall:.1f}/100 using weights {score.weights_version} "
                    f"({score.method}). "
                    + " ".join(f"{k}: {v}" for k, v in score.evidence.items())
                )[:4000],
                standalone_ok=score.criteria["standalone"] >= 60,
                selected=suppressed is None and kept < wanted,
                suppressed_reason=suppressed,
            )
            self.db.add(moment)
            if moment.selected:
                kept += 1
            candidates.append(moment)
            scores.append(score)

        self.db.flush()
        for moment, score in zip(candidates, scores, strict=True):
            row = score.as_row()
            self.db.add(
                CandidateScore(
                    candidate_id=moment.id,
                    weights_version=score.weights_version,
                    **row,
                )
            )
        self.db.flush()
        return candidates, scores, [window for window, _, _ in ranked]

    def _run_visual(self, *, project: Project, source_path: Path, source) -> VisualReport:  # noqa: ANN001
        """Detect scenes and measure frames; persist scenes and the analysis payload.

        A visual failure degrades the *scoring inputs* (visual quality becomes unknown) but does
        not destroy the audio work already done. The failure is recorded on the analysis row.
        """
        try:
            report = analyse_frames(
                source_path,
                duration_ms=source.duration_ms or 0,
                ffmpeg_bin=self.settings.ffmpeg_bin,
            )
        except VisualAnalysisError as exc:
            self.db.execute(delete(VideoAnalysis).where(VideoAnalysis.project_id == project.id))
            self.db.add(
                VideoAnalysis(
                    project_id=project.id,
                    schema_version=1,
                    language=project.language,
                    payload={"visual_analysis": "unavailable", "reason": exc.detail},
                    provider="ffmpeg",
                    model="scene+frames",
                )
            )
            self.db.flush()
            return VisualReport(
                scenes=[], frames=[], detector="ffmpeg_scene", metrics={}, notes=[exc.detail]
            )

        # `scenes` is UNIQUE (project_id, idx) and `video_analysis` UNIQUE (project_id,
        # schema_version): a re-run replaces those rows instead of colliding with them.
        self.db.execute(delete(Scene).where(Scene.project_id == project.id))
        self.db.execute(delete(VideoAnalysis).where(VideoAnalysis.project_id == project.id))
        for scene in report.scenes:
            self.db.add(
                Scene(
                    project_id=project.id,
                    idx=scene.idx,
                    start_ms=scene.start_ms,
                    end_ms=scene.end_ms,
                    detector=report.detector,
                    mean_motion=None,
                )
            )
        analysis = VideoAnalysis(
            project_id=project.id,
            schema_version=1,
            language=project.language,
            payload={
                "detector": report.detector,
                "scene_count": len(report.scenes),
                "frame_samples": len(report.frames),
                "metrics": report.metrics,
                "notes": report.notes,
                "_kind": "technical_visual_pass",
            },
            provider="ffmpeg",
            model="scene+frames",
            analysis_confidence=None,
        )
        self.db.add(analysis)
        self.db.flush()
        return report

    # ------------------------------------------------------------------ helpers
    def _record_failure(
        self,
        job_id: uuid.UUID,
        workspace_id: uuid.UUID,
        project: Project,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
    ) -> None:
        """Best-effort failure bookkeeping that can never mask the original exception.

        The SQLAlchemy session may already be poisoned (e.g. a failed flush), in which case even
        the query that loads the job raises `PendingRollbackError`. Losing the real error behind
        that secondary failure is exactly the kind of thing that makes production debugging awful,
        so each step is isolated and the session is reset first.
        """
        with contextlib.suppress(Exception):  # rollback must never mask the real error
            self.db.rollback()
        try:
            self._fail_job(job_id, workspace_id, code, detail, retryable=retryable)
            self._fail_project(project, code, detail)
            self.db.commit()
        except Exception as bookkeeping_error:  # noqa: BLE001
            self.db.rollback()
            self.audit_error = f"{type(bookkeeping_error).__name__}: {bookkeeping_error}"

    def _resume(self, project: Project) -> None:
        """Move a FAILED/CANCELLED project back onto the pipeline before re-running.

        `_enter()` deliberately refuses to leave a terminal stage (see its docstring), so a retry
        that reached a terminal project would silently do all the work and record none of it. The
        single legal transition FAILED/CANCELLED -> MEDIA_INSPECTION exists in the graph for
        exactly this reason: the user asked for this, it is not automation.
        """
        self.projects.transition(
            workspace_id=project.workspace_id,
            project_id=project.id,
            target=ProjectStage.MEDIA_INSPECTION,
            task_name="retry_analysis",
            detail=f"resuming from {project.status} after an explicit retry",
        )
        # Keep the in-memory project in step; later `_enter` calls compare against it.
        project.status = ProjectStage.MEDIA_INSPECTION.value

    def _enter(
        self, project: Project, stage: ProjectStage, *, task: str, detail: str | None = None
    ) -> None:
        """Advance the project to `stage`, or skip if it has already been past it.

        The graph is acyclic and progress-only: a *repeat* analysis must not move the project
        backwards (that transition does not exist, and pretending it does would corrupt the
        journal). The stage already happened, so a re-run simply does not re-record it. The new
        work is recorded instead on `processing_jobs` and the `analysis.completed` audit entry.
        """
        if project.status == stage.value:
            return
        current = ProjectStage(project.status)
        if current in TERMINAL_STAGES:
            return
        if (
            current in PIPELINE_ORDER
            and stage in PIPELINE_ORDER
            and PIPELINE_ORDER.index(stage) < PIPELINE_ORDER.index(current)
        ):
            return  # already past this stage: nothing to advance
        self.projects.transition(
            workspace_id=project.workspace_id,
            project_id=project.id,
            target=stage,
            task_name=task,
            detail=detail,
        )

    def _fingerprint(
        self, sha256: bytes | None, config_version: str, *, salt: str | None = None
    ) -> str:
        payload = json.dumps(
            {
                "source_sha256": sha256.hex() if sha256 else None,
                "provider": self.settings.asr_provider,
                "model": self.settings.asr_model,
                "scoring": config_version,
                "salt": salt,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def _existing_transcript(  # noqa: ANN202 - returns an ORM row or None
        self, source_id: uuid.UUID, fingerprint: str, *, force: bool
    ):
        """Return the transcript produced by an identical completed run, if one exists."""
        if force:
            return None
        if self.jobs.find_succeeded(input_fingerprint=fingerprint) is None:
            return None
        return self.db.scalar(
            select(Transcript)
            .where(Transcript.source_video_id == source_id)
            .order_by(Transcript.created_at.desc())
        )

    def _latest_transcript(self, source_id: uuid.UUID) -> Transcript | None:
        return self.db.scalar(
            select(Transcript)
            .where(Transcript.source_video_id == source_id)
            .order_by(Transcript.created_at.desc())
        )

    def _load_candidates(self, project_id: uuid.UUID):  # noqa: ANN202
        moments = list(
            self.db.scalars(
                select(CandidateMoment)
                .where(CandidateMoment.project_id == project_id)
                .order_by(CandidateMoment.start_ms)
            )
        )
        scores_by_candidate = {
            row.candidate_id: row
            for row in self.db.scalars(
                select(CandidateScore).where(
                    CandidateScore.candidate_id.in_([m.id for m in moments])
                )
            )
        }
        return moments, list(scores_by_candidate.values()), []

    def _mark_running(self, job_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        self.jobs.mark_running(workspace_id=workspace_id, job_id=job_id)

    def _set_progress(
        self, job_id: uuid.UUID, workspace_id: uuid.UUID, pct: int, note: str
    ) -> None:
        self.jobs.set_progress(workspace_id=workspace_id, job_id=job_id, progress=pct, note=note)

    def _finish_job(self, job_id: uuid.UUID, workspace_id: uuid.UUID, *, note: str) -> None:
        self.jobs.mark_succeeded(workspace_id=workspace_id, job_id=job_id, note=note)

    def _fail_job(
        self,
        job_id: uuid.UUID,
        workspace_id: uuid.UUID,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
    ) -> None:
        """Record the failure and say whether it *could* succeed on another attempt.

        `retryable` matters: it is the only way a job ever reaches DEAD_LETTER (retryable but out
        of attempts), which is the state that tells a human to look. Hardcoding it to False made
        DEAD_LETTER unreachable and quietly mislabelled transient failures as permanent.
        """
        self.jobs.mark_failed(
            workspace_id=workspace_id,
            job_id=job_id,
            code=code,
            detail=detail,
            retryable=retryable,
        )

    def _fail_project(self, project: Project, code: str, detail: str) -> None:
        """Illegal transitions are swallowed here on purpose.

        If the project already moved past a state that can reach FAILED (or is terminal), the job
        record still carries the error; refusing to overwrite a terminal state is the safer choice.
        """
        try:
            self.projects.transition(
                workspace_id=project.workspace_id,
                project_id=project.id,
                target=ProjectStage.FAILED,
                task_name="analysis",
                error_code=code,
                detail=detail[:500],
            )
        except ConflictError:
            self.db.rollback()
            self.db.refresh(project)


def _opt_float(value: object) -> float | None:
    """Coerce to a plain Python float.

    faster-whisper returns numpy scalars; psycopg cannot adapt them to NUMERIC and raises at
    INSERT time. Converting at the persistence boundary keeps the DB layer ignorant of numpy.
    """
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = ["AnalysisError", "AnalysisOutcome", "AnalysisService"]
