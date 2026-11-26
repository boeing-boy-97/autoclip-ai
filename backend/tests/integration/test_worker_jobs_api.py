"""Worker and job-control tests.

These run with `CELERY_TASK_ALWAYS_EAGER=true`, so tasks execute in-process. That is deliberate:
it exercises the *same* task body, service call and database writes the worker uses, without
needing a broker in CI. What eager mode cannot prove — that a message reaches a real worker
process — was verified by hand against a live Redis and a live `celery worker`; see
DEVELOPMENT_STATUS.md.

The tests below focus on the behaviour most likely to regress silently: a job that is never
closed, a retry that resumes nothing, and an error that loses its HTTP contract on the way
through Celery.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.integration.conftest import Actor

MEDIA = Path(__file__).resolve().parents[1] / "fixtures" / "generated"
SPEECH = MEDIA / "real_speech.mp4"
RIGHTS = "I recorded this footage myself and own all rights to it."

pytestmark = pytest.mark.integration


def need_speech() -> Path:
    if not SPEECH.exists():
        pytest.skip("speech fixture missing; run ops/scripts/make_speech_fixture.sh")
    return SPEECH


def upload(actor: Actor, project_id: str, path: Path) -> dict:
    resp = actor.client.post(
        f"/api/v1/workspaces/{actor.workspace_id}/projects/{project_id}/sources",
        files={"file": (path.name, path.read_bytes(), "video/mp4")},
        data={"rights_basis": "original", "rights_statement": RIGHTS},
        headers=actor.headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_project(actor: Actor, name: str = "Worker") -> str:
    resp = actor.client.post(
        f"/api/v1/workspaces/{actor.workspace_id}/projects",
        json={"name": name, "clips_requested": 3},
        headers=actor.headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def jobs(actor: Actor, **params) -> list[dict]:
    resp = actor.client.get(
        f"/api/v1/workspaces/{actor.workspace_id}/jobs",
        params=params,
        headers=actor.headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


class TestEnqueue:
    def test_analyze_queues_and_reports_the_job(self, alice: Actor):
        project_id = make_project(alice)
        upload(alice, project_id, need_speech())

        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["queued"] is True
        assert body["job_id"], "the caller must be told which job to follow"

    def test_job_is_terminal_when_the_request_returns(self, alice: Actor):
        """Eager mode finishes the task in-request, so nothing may be left PENDING.

        This is the regression that matters: a job left in PENDING is invisible work that no
        worker will ever pick up, and it looks like a hang to every client that polls it.
        """
        project_id = make_project(alice)
        upload(alice, project_id, need_speech())
        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        )
        job_id = resp.json()["job_id"]
        detail = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/jobs/{job_id}", headers=alice.headers
        ).json()
        assert detail["status"] == "SUCCEEDED", detail
        assert detail["progress"] == 100
        assert detail["attempts"] == 1

    def test_repeat_analyze_reuses_without_queueing(self, alice: Actor):
        project_id = make_project(alice)
        upload(alice, project_id, need_speech())
        first = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        )
        assert first.status_code == 202

        before = len(jobs(alice, project_id=project_id))
        again = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        )
        assert again.status_code == 202
        assert again.json()["queued"] is False
        assert again.json()["job_id"] is None
        assert len(jobs(alice, project_id=project_id)) == before, "no second job may be created"


class TestJobApi:
    def test_list_is_scoped_and_filterable(self, alice: Actor):
        project_id = make_project(alice)
        upload(alice, project_id, need_speech())
        alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        )
        found = jobs(alice, project_id=project_id)
        assert len(found) == 1
        assert found[0]["stage"] == "transcription"
        assert found[0]["task_name"] == "analysis.run"
        # Filters that match nothing must return an empty page, not the full list.
        assert jobs(alice, project_id=project_id, status="RUNNING") == []

    def test_cannot_cancel_finished_work(self, alice: Actor):
        project_id = make_project(alice)
        upload(alice, project_id, need_speech())
        job_id = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        ).json()["job_id"]

        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/jobs/{job_id}/cancel",
            headers=alice.headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "job_not_cancellable"

    def test_cannot_retry_successful_work(self, alice: Actor):
        """Re-running good work is /analyze?force=true, not a retry."""
        project_id = make_project(alice)
        upload(alice, project_id, need_speech())
        job_id = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        ).json()["job_id"]

        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/jobs/{job_id}/retry",
            headers=alice.headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "job_not_retryable"

    def test_bob_cannot_see_alices_jobs(self, alice: Actor, bob: Actor):
        project_id = make_project(alice)
        upload(alice, project_id, need_speech())
        job_id = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        ).json()["job_id"]

        for path in ("", f"/{job_id}", f"/{job_id}/cancel", f"/{job_id}/retry"):
            method = "get" if path in ("", f"/{job_id}") else "post"
            resp = getattr(bob.client, method)(
                f"/api/v1/workspaces/{alice.workspace_id}/jobs{path}", headers=bob.headers
            )
            assert resp.status_code == 404, f"{method} {path} leaked {resp.status_code}"

    def test_cancel_and_retry_require_csrf(self, alice: Actor):
        project_id = make_project(alice)
        upload(alice, project_id, need_speech())
        job_id = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        ).json()["job_id"]

        for action in ("cancel", "retry"):
            resp = alice.client.post(
                f"/api/v1/workspaces/{alice.workspace_id}/jobs/{job_id}/{action}",
                headers={"X-CSRF-Token": "wrong-token-value"},
            )
            assert resp.status_code == 403, f"{action} accepted a bad CSRF token"


class TestRetryResumesWork:
    """A retried job must actually redo the work and leave the project consistent."""

    def test_retry_of_failed_job_resumes_and_succeeds(self, alice: Actor, db_engine):
        project_id = make_project(alice, "RetryResume")
        upload(alice, project_id, need_speech())
        job_id = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        ).json()["job_id"]

        # Simulate the failure a killed worker leaves behind.
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE processing_jobs SET status='FAILED', error_code='worker_lost' "
                    "WHERE id=:id"
                ),
                {"id": job_id},
            )
            conn.execute(
                text("UPDATE projects SET status='FAILED', error_code='worker_lost' WHERE id=:id"),
                {"id": project_id},
            )

        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/jobs/{job_id}/retry",
            headers=alice.headers,
        )
        assert resp.status_code == 202, resp.text

        detail = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/jobs/{job_id}", headers=alice.headers
        ).json()
        # Eager mode: the retry runs in-request, so it must already be finished.
        assert detail["status"] == "SUCCEEDED", detail
        assert detail["error_code"] is None

        project = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analysis",
            headers=alice.headers,
        ).json()
        assert project["status"] == "MOMENT_DETECTION"
        assert project["has_transcript"] is True

    def test_resume_transition_is_journaled(self, alice: Actor, db_engine):
        """The resume must leave a trace; a silent state change is untraceable in production."""
        project_id = make_project(alice, "RetryJournal")
        upload(alice, project_id, need_speech())
        job_id = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        ).json()["job_id"]

        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE processing_jobs SET status='FAILED', error_code='worker_lost' "
                    "WHERE id=:id"
                ),
                {"id": job_id},
            )
            conn.execute(
                text("UPDATE projects SET status='FAILED' WHERE id=:id"), {"id": project_id}
            )

        alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/jobs/{job_id}/retry",
            headers=alice.headers,
        )

        with db_engine.connect() as conn:
            rows = conn.execute(
                text(
                    # Aliases must not collide with SQLAlchemy Row attributes: `Row.t` is a
                    # deprecated built-in and silently returns something else.
                    "SELECT from_status::text AS src, to_status::text AS dst, task_name "
                    "FROM project_stage_events WHERE project_id=:pid ORDER BY at"
                ),
                {"pid": project_id},
            ).all()
        assert any(r.src == "FAILED" and r.dst == "MEDIA_INSPECTION" for r in rows), rows
        resume = [r for r in rows if r.task_name == "retry_analysis"]
        assert len(resume) == 1, "exactly one resume transition may be recorded"


class TestEagerModeIsHonest:
    """Eager mode exists for tests and local dev; it must not soften the API contract."""

    def test_run_analysis_returns_a_summary(self, alice: Actor):
        """The task's return value is what a real worker reports back."""
        from app.core.config import get_settings
        from app.worker.tasks import run_analysis

        project_id = make_project(alice, "TaskResult")
        upload(alice, project_id, need_speech())

        # Reserve the job the way the API does, then invoke the task body directly.
        from app.db.session import session_scope
        from app.services.analysis import AnalysisService
        from app.services.storage import get_storage

        with session_scope() as db:
            settings = get_settings()
            plan = AnalysisService(db, settings, get_storage(settings)).plan(
                workspace_id=uuid.UUID(alice.workspace_id), project_id=uuid.UUID(project_id)
            )
        assert plan.job_id is not None

        result = run_analysis.apply(
            kwargs={
                "workspace_id": alice.workspace_id,
                "project_id": project_id,
                "user_id": alice.user_id,
                "job_id": str(plan.job_id),
                "force": False,
            }
        ).get()
        assert result["candidates"] >= 1
        assert result["transcript_id"]
        assert result["reused"] is False


class TestBrokerUnavailable:
    def test_broker_outage_is_reported_not_hidden(self, alice: Actor, monkeypatch):
        """With a real broker configured, an outage must surface as 503 — never a silent inline run.

        Silently running the pipeline inside the request would make a misconfigured production
        deploy look healthy while blocking a request thread for minutes.
        """
        project_id = make_project(alice, "NoBroker")
        upload(alice, project_id, need_speech())

        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "celery_task_always_eager", False, raising=False)

        from app.worker import tasks as worker_tasks

        def explode(*_a, **_kw):
            from celery.exceptions import OperationalError

            raise OperationalError("broker down")

        # dispatch imports the task object from this module at call time, so patching the task
        # itself is what the code under test actually resolves.
        monkeypatch.setattr(worker_tasks.run_analysis, "apply_async", explode, raising=False)

        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project_id}/analyze",
            headers=alice.headers,
        )
        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "queue_unavailable"

        # The work must still be discoverable and retryable once the broker returns.
        pending = jobs(alice, project_id=project_id)
        assert len(pending) == 1
        assert pending[0]["status"] == "PENDING"
