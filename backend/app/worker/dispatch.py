"""Handing work to Celery — with an honest fallback.

The broker may be unreachable (a laptop with no Redis, or an outage). There are only two honest
responses to that, and which one applies depends on configuration:

  * `celery_task_always_eager=True` — deliver the task in-process via `.apply()`. Same code path,
    same database writes, just no process boundary. Used by tests and local development.
  * otherwise — raise `BrokerUnavailableError`. The API then reports 503 and *no work is claimed*.
    Silently running the pipeline inside the web request instead would make a misconfigured
    production deploy look healthy while blocking a request thread for minutes, which is exactly
    the failure mode this whole change exists to remove.
"""

from __future__ import annotations

import logging
import uuid

from celery.exceptions import CeleryError, OperationalError
from kombu.exceptions import KombuError

from app.core.config import Settings
from app.db.repositories import JobRepository
from app.db.session import session_scope

logger = logging.getLogger(__name__)


class BrokerUnavailableError(RuntimeError):
    """The task broker could not accept the message."""


def dispatch_analysis(
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    force: bool = False,
) -> str | None:
    """Enqueue `analysis.run`. Returns the Celery task id, or None when run eagerly."""
    from app.worker.tasks import run_analysis  # noqa: PLC0415 - avoids an import cycle

    # Persist delivery identity before publishing. Requeue clears it so old broker deliveries
    # cannot execute a newer attempt. Duplicate HTTP dispatches reuse the same identity.
    with session_scope() as db:
        job = JobRepository(db)._locked(workspace_id, job_id)
        task_id = job.celery_task_id or uuid.uuid4().hex
        job.celery_task_id = task_id

    kwargs = {
        "workspace_id": str(workspace_id),
        "project_id": str(project_id),
        "user_id": str(user_id),
        "job_id": str(job_id),
        "force": force,
    }

    if settings.celery_task_always_eager:
        logger.info(
            "running analysis inline (eager mode)",
            extra={"project_id": str(project_id), "job_id": str(job_id)},
        )
        # Eager mode sets task_eager_propagates=True, so a failing task raises here and the API
        # reports the real error. Callers must therefore be prepared for AnalysisError.
        run_analysis.apply(kwargs=kwargs, task_id=task_id)
        return None

    try:
        async_result = run_analysis.apply_async(kwargs=kwargs, queue="transcribe", task_id=task_id)
    except (CeleryError, KombuError, OperationalError, OSError) as exc:
        # `celery.exceptions.OperationalError` is actually a *kombu* error and is NOT a subclass
        # of CeleryError, so catching CeleryError alone let a broker outage escape as a 500.
        logger.error(
            "celery broker unavailable",
            extra={"project_id": str(project_id), "error_type": type(exc).__name__},
        )
        raise BrokerUnavailableError(
            "the task queue is unavailable; the job was recorded but not dispatched"
        ) from exc
    return async_result.id
