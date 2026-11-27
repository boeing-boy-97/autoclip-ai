"""Celery tasks for the analysis pipeline.

Design rules that everything here follows:

1. **Ids in, ids out.** A task receives UUIDs, never objects. It re-reads the project, source and
   job row from the database, so a redelivered or retried task sees current state rather than a
   stale snapshot baked into a message.
2. **Run the real service, do not reimplement it.** These tasks call `AnalysisService` exactly as
   the synchronous API path did. There is one implementation of the pipeline, not two that can
   drift apart.
3. **Retry only what can succeed later.** `AsrUnavailableError` (model/device missing) is
   transient-if-configured; `AsrFailedError` (no speech, corrupt audio) is not. Retrying the latter
   just burns CPU to fail identically.
4. **Never leave a job RUNNING forever.** Every exit path marks the job; a sweeper reclaims jobs
   whose worker was killed outright and therefore never got to run its exit path.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select

from app.db.locks import project_execution
from app.db.models import ProcessingJob
from app.db.repositories import JobInterruptedError, JobRepository
from app.db.session import session_scope
from app.services.analysis import AnalysisError, AnalysisService
from app.services.storage import get_storage
from app.services.transcription import AsrUnavailableError
from app.worker import celery_app

logger = logging.getLogger(__name__)

# Failures worth another attempt: the same input could plausibly succeed later.
_RETRYABLE_CODES = frozenset({"asr_unavailable", "storage_unavailable", "analysis_failed"})


@celery_app.task(
    name="analysis.run",
    bind=True,
    max_retries=3,
    default_retry_delay=15,
    acks_late=True,
)
def run_analysis(
    self,  # noqa: ANN001 - bound task instance
    *,
    workspace_id: str,
    project_id: str,
    user_id: str,
    job_id: str,
    force: bool = False,
) -> dict:
    """Execute the analysis pipeline for one project.

    Returns a small summary for the result backend. The authoritative record is the database;
    the result backend is a convenience, never the source of truth.
    """
    ws = uuid.UUID(workspace_id)
    pid = uuid.UUID(project_id)
    uid = uuid.UUID(user_id)
    jid = uuid.UUID(job_id)

    logger.info(
        "analysis task started",
        extra={"project_id": str(pid), "job_id": str(jid), "attempt": self.request.retries + 1},
    )

    try:
        with project_execution(ws, pid) as acquired, session_scope() as db:
            if not acquired:
                return {"job_id": str(jid), "skipped": "project_busy"}
            job = JobRepository(db).get(workspace_id=ws, job_id=jid)
            if job.celery_task_id and job.celery_task_id != self.request.id:
                return {"job_id": str(jid), "skipped": "stale_delivery"}
            if job.project_id != pid or job.status not in {"PENDING", "RETRYING"}:
                return {"job_id": str(jid), "skipped": job.status.lower()}
            settings = _settings()
            service = AnalysisService(db, settings, get_storage(settings))
            outcome = service.analyse(
                workspace_id=ws, project_id=pid, user_id=uid, force=force, job_id=jid
            )
            return {
                "project_id": str(pid),
                "job_id": str(jid),
                "transcript_id": str(outcome.transcript_id),
                "candidates": len(outcome.candidates),
                "selected": sum(1 for c in outcome.candidates if c.selected),
                "reused": outcome.reused,
                "elapsed_seconds": outcome.elapsed_seconds,
            }

    except JobInterruptedError:
        return {"job_id": str(jid), "skipped": "cancelled_or_retired"}

    except SoftTimeLimitExceeded:
        # Celery kills the task after the hard limit; record something useful first.
        _record_terminal_failure(jid, ws, "analysis_timeout", "task exceeded its time limit")
        raise

    except AsrUnavailableError as exc:
        # The model or device is missing — an operator problem, not a data problem.
        if self.request.retries < self.max_retries:
            delay = 15 * (2**self.request.retries)
            logger.warning(
                "asr unavailable; retrying",
                extra={"project_id": str(pid), "retry_in": delay, "detail": exc.detail},
            )
            if _mark_retrying(jid, ws, delay):
                raise self.retry(exc=exc, countdown=delay) from exc
        raise

    except AnalysisError as exc:
        # Already recorded on the job by the service; do not double-write. Only escalate to a
        # Celery retry when the code says another attempt could behave differently.
        if exc.code in _RETRYABLE_CODES and self.request.retries < self.max_retries:
            delay = 15 * (2**self.request.retries)
            logger.warning(
                "analysis failed with retryable code; retrying",
                extra={"project_id": str(pid), "code": exc.code, "retry_in": delay},
            )
            if _mark_retrying(jid, ws, delay):
                raise self.retry(exc=exc, countdown=delay) from exc
        logger.error(
            "analysis failed permanently",
            extra={"project_id": str(pid), "code": exc.code, "detail": exc.detail},
        )
        raise

    except Exception as exc:
        # Unexpected: the service's own failure path may not have run (e.g. import error,
        # programming bug). Record it so the project is not left mid-stage with no explanation.
        _record_terminal_failure(jid, ws, "analysis_failed", str(exc)[:500])
        raise


@celery_app.task(name="analysis.sweep_stuck")
def sweep_stuck_jobs() -> dict:
    """Reclaim jobs whose worker died without running its exit path.

    A killed worker cannot mark its own job failed. The sweeper finds RUNNING jobs whose heartbeat
    has gone quiet and moves them to DEAD_LETTER so a human sees them, rather than leaving the
    project wedged forever with a job that looks alive.

    Deliberately conservative: this does **not** silently retry the work (which could duplicate
    side effects). It surfaces the problem and lets an operator or the retry endpoint decide.
    """
    settings = _settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.job_stuck_after_seconds)
    reclaimed: list[str] = []

    with session_scope() as db:
        rows = list(
            db.scalars(
                select(ProcessingJob).where(
                    ((ProcessingJob.status == "RUNNING") & (ProcessingJob.heartbeat_at < cutoff))
                    | (
                        (ProcessingJob.status.in_(["PENDING", "RETRYING"]))
                        & (
                            func.coalesce(
                                ProcessingJob.next_retry_at,
                                ProcessingJob.heartbeat_at,
                                ProcessingJob.created_at,
                            )
                            < cutoff
                        )
                    ),
                )
            )
        )
        for job in rows:
            if job.project_id is not None:
                # No live worker may own the project, even if an ASR call ran longer than
                # the heartbeat cutoff. A connection crash releases the execution lock.
                from app.db.locks import guard_project  # noqa: PLC0415
                from app.db.repositories import ConflictError  # noqa: PLC0415

                try:
                    guard_project(db, job.workspace_id, job.project_id)
                except ConflictError:
                    continue
            db.refresh(job, with_for_update=True)
            if job.status not in {"RUNNING", "PENDING", "RETRYING"}:
                continue
            if job.status == "RUNNING" and job.heartbeat_at and job.heartbeat_at >= cutoff:
                continue
            was_pending = job.status in {"PENDING", "RETRYING"}
            job.status = "DEAD_LETTER"
            job.error_code = "queue_wait_timeout" if was_pending else "worker_lost"
            last_seen = job.heartbeat_at.isoformat() if job.heartbeat_at else "never"
            job.error_detail = (
                f"no heartbeat since {last_seen}; the worker was most likely killed. "
                "Retry the job to continue."
            )
            job.finished_at = datetime.now(UTC)
            reclaimed.append(str(job.id))

    if reclaimed:
        logger.warning("reclaimed stuck jobs", extra={"count": len(reclaimed)})
    return {"reclaimed": reclaimed, "count": len(reclaimed)}


# --------------------------------------------------------------------------- helpers
def _settings():  # noqa: ANN202 - Settings, imported lazily to keep eager-mode imports cheap
    from app.core.config import get_settings  # noqa: PLC0415

    return get_settings()


def _record_terminal_failure(
    job_id: uuid.UUID, workspace_id: uuid.UUID, code: str, detail: str
) -> None:
    """Best-effort failure record for paths the service could not reach.

    Must never raise: it runs while another exception is propagating, and masking the real error
    with a bookkeeping error is how outages become unexplainable.
    """
    try:
        with session_scope() as db:
            from app.db.repositories import JobRepository  # noqa: PLC0415

            JobRepository(db).mark_failed(
                workspace_id=workspace_id,
                job_id=job_id,
                code=code,
                detail=detail,
                retryable=False,
            )
    except Exception:
        logger.exception("could not record terminal failure", extra={"job_id": str(job_id)})


def _mark_retrying(job_id: uuid.UUID, workspace_id: uuid.UUID, delay: int) -> bool:
    with session_scope() as db:
        job = JobRepository(db)._locked(workspace_id, job_id)
        if job.status != "FAILED":
            return False
        job.status = "RETRYING"
        job.finished_at = None
        job.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        return True
