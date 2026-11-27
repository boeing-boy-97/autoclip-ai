"""Celery application for background pipeline work.

The worker is a **separate process** with its own database session and its own storage handle.
Nothing here reaches into request-scoped state: a task receives ids and re-reads everything from
the database, so a task is replayable and a killed worker leaves no half-remembered context behind.

Queues are split by cost profile, mirroring `JobRepository.QUEUE_FOR_STAGE`:
  media      — FFmpeg probing/extraction and bookkeeping (I/O + CPU, no model)
  transcribe — ASR (heavy CPU, model resident)
  analyze    — reserved for the visual/scoring split; no producer yet
A slow transcription therefore cannot starve a cheap scoring job. Only these three queues have
workers: adding a queue to `docker-compose.yml` without a route/task would create a worker that can
never receive work, which is why `JobRepository.ACTIVE_QUEUES` clamps everything else to `media`.

Run a worker:
    celery -A app.worker.celery_app worker --loglevel=INFO -Q media,transcribe,analyze
Beat (for the heartbeat sweeper) is started separately — see `app.worker.sweep_stuck_jobs`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Tasks that are safe to redeliver. A task must be idempotent for this to be true, which is why
# every task here is guarded by a persisted job row and an input fingerprint.
#
# Every queue named here must have a worker consuming it in `docker-compose.yml`; the pairing is
# asserted by tests/unit/test_worker_wiring.py so the two files cannot drift apart again.
_TASK_ROUTES: dict[str, dict[str, Any]] = {
    # ASR-bound: the model is resident in the transcribe worker, so this is where the whole
    # analysis task runs (it owns transcription, scoring and moment detection end to end).
    "analysis.run": {"queue": "transcribe"},
    # Bookkeeping only: cheap database work, so it rides the media queue's spare capacity.
    "analysis.sweep_stuck": {"queue": "media"},
}


def _build() -> Celery:
    settings = get_settings()
    app = Celery(
        "autoclip",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        include=["app.worker.tasks"],
    )
    app.conf.update(
        task_routes=_TASK_ROUTES,
        task_acks_late=True,  # redeliver if a worker dies mid-task
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,  # long tasks: never hoard messages
        task_track_started=True,
        task_time_limit=settings.celery_task_time_limit,
        task_soft_time_limit=settings.celery_task_soft_time_limit,
        result_expires=3600,
        broker_connection_retry_on_startup=True,
        task_default_queue="media",
        # With acks_late, a message must not become visible again while its task is still running.
        # Redis' default visibility timeout is 1 hour; the hard time limit plus a margin is the
        # honest number, otherwise a long transcription is executed twice.
        broker_transport_options={
            "visibility_timeout": settings.celery_task_time_limit + 300,
        },
        # Celery's own handler would otherwise bypass the structlog configuration below.
        worker_hijack_root_logger=False,
        # The sweeper is the only periodic task: reclaim jobs whose worker was killed without
        # running its exit path. Without this schedule the sweeper exists but never runs.
        beat_schedule={
            "sweep-stuck-jobs": {
                "task": "analysis.sweep_stuck",
                "schedule": 60.0,
                "options": {"queue": "media", "expires": 55},
            }
        },
        # Ordering and idempotency are handled in the database, not by the broker.
        task_ignore_result=False,
        timezone="UTC",
        enable_utc=True,
    )
    if settings.celery_task_always_eager:
        # Used by tests and by local runs without a worker. Work still executes in-process, so
        # the code path is identical — only the process boundary disappears.
        #
        # `task_eager_propagates` is essential, not cosmetic. Without it Celery swallows the task's
        # exception into the result object, so a request that should fail with 422
        # no_speech_detected would return 202 instead — the API would claim success for work that
        # failed. Eager mode must behave like the real thing as far as the caller can tell.
        app.conf.task_eager_propagates = True
        logger.info("celery running in eager mode: tasks execute in-process")
    else:
        app.conf.task_eager_propagates = False
    return app


celery_app = _build()


@celery_setup_logging.connect
def _configure_logging(**_: Any) -> None:
    """Use the application's structured logger instead of Celery's default handler."""
    from app.core.logging import configure_logging  # noqa: PLC0415

    configure_logging()
