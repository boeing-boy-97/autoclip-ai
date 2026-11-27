"""Real PostgreSQL execution/control tests; no downloaded ASR model required.

Task doubles isolate lifecycle races, not ASR accuracy. Local media tests use real FFmpeg output.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.locks import guard_project, project_execution
from app.db.models import ProcessingJob
from app.db.repositories import ConflictError, JobInterruptedError, JobRepository
from app.worker.tasks import run_analysis, sweep_stuck_jobs

pytestmark = pytest.mark.integration


def reserve(alice, db_engine):
    pid = uuid.UUID(alice.create_project()["id"])
    ws = uuid.UUID(alice.workspace_id)
    with Session(db_engine) as db:
        job = JobRepository(db).enqueue(
            workspace_id=ws,
            project_id=pid,
            stage="transcription",
            task_name="analysis.run",
        )
        jid = job.id
        db.commit()
    return ws, pid, jid


def task_args(alice, ws, pid, jid):
    return {
        "workspace_id": str(ws),
        "project_id": str(pid),
        "job_id": str(jid),
        "user_id": alice.user_id,
    }


def test_execution_lock_survives_commits_and_releases(alice, db_engine):
    ws, pid, _ = reserve(alice, db_engine)
    with project_execution(ws, pid) as acquired:
        assert acquired
        with Session(db_engine) as db, pytest.raises(ConflictError):
            guard_project(db, ws, pid)
    with Session(db_engine) as db:
        guard_project(db, ws, pid)
        db.commit()


def test_cancelled_job_cannot_report_success_or_failure(alice, db_engine):
    ws, _, jid = reserve(alice, db_engine)
    with Session(db_engine) as db:
        repo = JobRepository(db)
        repo.mark_running(workspace_id=ws, job_id=jid)
        db.commit()
        repo.mark_cancelled(workspace_id=ws, job_id=jid)
        db.commit()
        with pytest.raises(JobInterruptedError):
            repo.mark_succeeded(workspace_id=ws, job_id=jid)
        db.rollback()
        repo.mark_failed(workspace_id=ws, job_id=jid, code="late_failure", detail="late worker")
        db.commit()
        assert repo.get(workspace_id=ws, job_id=jid).status == "CANCELLED"


def test_cancel_during_worker_is_preserved_and_retry_waits(alice, db_engine, monkeypatch):
    from app.worker import tasks

    ws, pid, jid = reserve(alice, db_engine)
    entered, release = threading.Event(), threading.Event()
    results = []

    def controlled_analysis(service, **_kwargs):
        service.jobs.mark_running(workspace_id=ws, job_id=jid)
        service.db.commit()
        entered.set()
        assert release.wait(10)
        service.jobs.mark_succeeded(workspace_id=ws, job_id=jid)

    monkeypatch.setattr(tasks.AnalysisService, "analyse", controlled_analysis)
    thread = threading.Thread(
        target=lambda: results.append(run_analysis.run(**task_args(alice, ws, pid, jid)))
    )
    thread.start()
    try:
        assert entered.wait(10)
        with Session(db_engine) as db:
            repo = JobRepository(db)
            repo.mark_cancelled(workspace_id=ws, job_id=jid)
            db.commit()
            with pytest.raises(ConflictError):
                repo.requeue(workspace_id=ws, job_id=jid)
        duplicate = run_analysis.run(**task_args(alice, ws, pid, jid))
        assert duplicate["skipped"] == "project_busy"
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert results[0]["skipped"] == "cancelled_or_retired"
    with Session(db_engine) as db:
        repo = JobRepository(db)
        assert repo.get(workspace_id=ws, job_id=jid).status == "CANCELLED"
        assert repo.requeue(workspace_id=ws, job_id=jid).status == "PENDING"


def test_retired_delivery_never_calls_pipeline(alice, db_engine, monkeypatch):
    from app.worker import tasks

    ws, pid, jid = reserve(alice, db_engine)
    with Session(db_engine) as db:
        JobRepository(db).mark_cancelled(workspace_id=ws, job_id=jid)
        db.commit()

    def forbidden(*_args, **_kwargs):
        pytest.fail("retired task executed")

    monkeypatch.setattr(tasks.AnalysisService, "analyse", forbidden)
    assert run_analysis.run(**task_args(alice, ws, pid, jid))["skipped"] == "cancelled"


def test_sweeper_does_not_reclaim_live_execution(alice, db_engine):
    ws, pid, jid = reserve(alice, db_engine)
    with Session(db_engine) as db:
        job = JobRepository(db).mark_running(workspace_id=ws, job_id=jid)
        job.heartbeat_at = datetime.now(UTC) - timedelta(hours=2)
        db.commit()
    with project_execution(ws, pid) as acquired:
        assert acquired
        assert sweep_stuck_jobs.run()["count"] == 0
    assert str(jid) in sweep_stuck_jobs.run()["reclaimed"]
    with Session(db_engine) as db:
        assert db.get(ProcessingJob, jid).status == "DEAD_LETTER"


def test_sweeper_surfaces_never_dispatched_pending_job(alice, db_engine):
    _, _, jid = reserve(alice, db_engine)
    with Session(db_engine) as db:
        job = db.get(ProcessingJob, jid)
        job.created_at = datetime.now(UTC) - timedelta(hours=2)
        db.commit()
    assert str(jid) in sweep_stuck_jobs.run()["reclaimed"]
    with Session(db_engine) as db:
        assert db.get(ProcessingJob, jid).error_code == "queue_wait_timeout"


def test_custom_session_cookie_supports_login_me_and_logout(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("SESSION_COOKIE_NAME", "studio_session")
    get_settings.cache_clear()
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "custom-cookie@example.com",
            "password": "long-test-password",
            "display_name": "Studio",
        },
    )
    assert response.status_code == 201, response.text
    assert client.cookies.get("studio_session")
    assert client.get("/api/v1/auth/me").status_code == 200
    assert (
        client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": response.json()["csrf_token"]}
        ).status_code
        == 204
    )
    assert client.get("/api/v1/auth/me").status_code == 401


def test_invalid_patch_and_blank_names_are_client_errors(alice):
    ws = alice.workspace_id
    path = f"/api/v1/workspaces/{ws}/projects"
    assert alice.client.post(path, json={"name": "   "}, headers=alice.headers).status_code == 422
    pid = alice.create_project()["id"]
    for patch in ({"publishing_mode": "invalid"}, {"template_key": "../../private"}):
        response = alice.client.patch(f"{path}/{pid}", json=patch, headers=alice.headers)
        assert response.status_code == 422, response.text


def test_real_render_media_version_pinning_and_tenant_access(alice, bob, db_engine):
    from pathlib import Path

    from app.db.models import Clip, SourceVideo

    path = Path(__file__).resolve().parents[1] / "fixtures/generated/talking_30s.mp4"
    if not path.exists():
        pytest.skip("generate real media fixtures first")
    project = alice.create_project()
    ws, pid = uuid.UUID(alice.workspace_id), uuid.UUID(project["id"])
    uploaded = alice.client.post(
        f"/api/v1/workspaces/{ws}/projects/{pid}/sources",
        files={"file": (path.name, path.read_bytes(), "video/mp4")},
        data={
            "rights_basis": "original",
            "rights_statement": "I created this test recording myself.",
        },
        headers=alice.headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    with Session(db_engine) as db:
        source = db.scalar(select(SourceVideo).where(SourceVideo.project_id == pid))
        clip = Clip(
            workspace_id=ws,
            project_id=pid,
            source_video_id=source.id,
            title="Real render",
            start_ms=0,
            end_ms=3000,
            duration_ms=3000,
            state="DRAFT",
            layout="single_subject",
            template_key="podcast",
        )
        db.add(clip)
        db.commit()
        cid = clip.id
    base = f"/api/v1/workspaces/{ws}/clips/{cid}"
    result = alice.client.post(f"{base}/render", headers=alice.headers)
    assert result.status_code == 201, result.text
    v1 = result.json()["version_id"]
    assert result.json()["state"] == "AWAITING_REVIEW"
    assert alice.client.get(base).json()["publishable"] is False
    data = alice.client.get(f"{base}/media?version_id={v1}", headers={"Range": "bytes=0-31"})
    assert data.status_code == 206
    assert b"ftyp" in data.content
    assert bob.client.get(f"{base}/media?version_id={v1}").status_code == 404
    assert alice.client.get(f"{base}/media?version_id={uuid.uuid4()}").status_code == 404
    assert (
        alice.client.post(
            f"{base}/reject", json={"reason": "New version"}, headers=alice.headers
        ).status_code
        == 200
    )
    second = alice.client.post(f"{base}/render", headers=alice.headers)
    assert second.status_code == 201, second.text
    pinned = alice.client.get(f"{base}/media?version_id={v1}", headers={"Range": "bytes=0-31"})
    assert pinned.content == data.content


def test_project_mutations_refused_while_worker_owns_project(alice):
    pid = uuid.UUID(alice.create_project()["id"])
    ws = uuid.UUID(alice.workspace_id)
    base = f"/api/v1/workspaces/{ws}/projects/{pid}"
    with project_execution(ws, pid) as acquired:
        assert acquired
        for response in (
            alice.client.patch(base, json={"name": "Race"}, headers=alice.headers),
            alice.client.delete(base, headers=alice.headers),
            alice.client.post(f"{base}/generate-clips", headers=alice.headers),
            alice.client.post(f"{base}/analyze", headers=alice.headers),
        ):
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "project_busy"


def test_project_pagination_keeps_equal_timestamps(alice, db_engine):
    from app.db.models import Project

    ws = uuid.UUID(alice.workspace_id)
    ids = {alice.create_project()["id"] for _ in range(4)}
    with Session(db_engine) as db:
        for project in db.scalars(select(Project).where(Project.workspace_id == ws)):
            project.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        db.commit()
    seen, cursor = [], ""
    for _ in range(5):
        page = alice.client.get(f"/api/v1/workspaces/{ws}/projects?limit=1&cursor={cursor}")
        assert page.status_code == 200, page.text
        seen.extend(row["id"] for row in page.json()["items"])
        cursor = page.json()["next_cursor"]
        if not cursor:
            break
    assert set(seen) == ids
    assert len(seen) == 4
