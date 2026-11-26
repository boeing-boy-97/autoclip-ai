"""Celery application for background pipeline work.

The worker is a **separate process** with its own database session and its own storage handle.
Nothing here reaches into request-scoped state: a task receives ids and re-reads everything from
the database, so a task is replayable and a killed worker leaves no half-remembered context behind.

Queues are split by cost profile, mirroring `JobRepository.QUEUE_FOR_STAGE`:
  media      — FFmpeg probing/extraction (I/O + CPU, no model)
  transcribe — ASR (heavy CPU, model resident)
  analyze    — scoring and moment detection (cheap CPU)
A slow transcription therefore cannot starve a cheap scoring job.

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
_TASK_ROUTES: dict[str, dict[str, Any]] = {
    "analysis.run": {"queue": "transcribe"},
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
        task_acks_late=True,          # redeliver if a worker dies mid-task
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,  # long tasks: never hoard messages
        task_track_started=True,
        task_time_limit=settings.celery_task_time_limit,
        task_soft_time_limit=settings.celery_task_soft_time_limit,
        result_expires=3600,
        broker_connection_retry_on_startup=True,
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
