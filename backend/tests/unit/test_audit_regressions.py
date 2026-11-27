"""Regression coverage for security boundaries and real media delivery."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.responses import FileResponse, RedirectResponse

from app.api.deps import ApiError, require_csrf
from app.api.v1 import clips
from app.main import create_app
from app.services.storage import LocalStorage, StorageError


def test_local_storage_rejects_sibling_prefix_and_root(tmp_path):
    storage = LocalStorage(str(tmp_path / "media"))
    for key in ("../media-secret/file.mp4", "..", ".", str(tmp_path / "media-secret")):
        with pytest.raises(StorageError):
            storage._path(key)
    assert storage._path("safe/file.mp4").is_relative_to(storage.root)


def test_local_storage_rejects_symlink_escape(tmp_path):
    storage = LocalStorage(str(tmp_path / "media"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError):
        storage._path("link/file.mp4")


@pytest.mark.parametrize("suffix", ["/reject", "/versions/{version_id}/restore"])
def test_clip_mutations_declare_csrf(suffix):
    route = next(r for r in clips.router.routes if r.path.endswith(suffix))
    assert require_csrf in [dep.call for dep in route.dependant.dependencies]


def test_http_errors_preserve_protocol_headers():
    client = TestClient(create_app())
    response = client.delete("/api/v1/health/livez")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]


def test_trace_id_rejects_unbounded_untrusted_input():
    client = TestClient(create_app())
    response = client.get("/api/v1/health/livez", headers={"X-Trace-Id": "x" * 500})
    assert len(response.headers["X-Trace-Id"]) == 32


def _media_setup(monkeypatch, tmp_path):
    ws, cid = uuid.uuid4(), uuid.uuid4()
    storage = LocalStorage(str(tmp_path))
    storage.put_stream(key="clip.mp4", chunks=[b"real-file-bytes"], max_bytes=100)
    clip = SimpleNamespace(current_version_id=uuid.uuid4())
    version = SimpleNamespace(video_asset_id=uuid.uuid4(), version_no=1)
    asset = SimpleNamespace(workspace_id=ws, storage_key="clip.mp4")
    db = MagicMock()
    db.get.side_effect = [version, asset]
    monkeypatch.setattr(clips, "_clip", lambda *_: clip)
    monkeypatch.setattr(clips, "get_storage", lambda _: storage)
    return ws, cid, db, storage, asset


def test_media_serves_real_local_file(monkeypatch, tmp_path):
    ws, cid, db, storage, _ = _media_setup(monkeypatch, tmp_path)
    response = clips.clip_media(ws, cid, db, None, download=True)
    assert isinstance(response, FileResponse)
    assert response.path == storage._path("clip.mp4")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "private, no-store"


def test_media_rejects_asset_from_other_workspace(monkeypatch, tmp_path):
    ws, cid, db, _, asset = _media_setup(monkeypatch, tmp_path)
    asset.workspace_id = uuid.uuid4()
    with pytest.raises(ApiError) as exc:
        clips.clip_media(ws, cid, db, None)
    assert exc.value.status_code == 404


def test_media_redirects_to_real_s3_presigner(monkeypatch, tmp_path):
    ws, cid, db, _, _ = _media_setup(monkeypatch, tmp_path)
    storage = MagicMock()
    storage.signed_url.return_value = "https://storage.example/authorized-object"
    monkeypatch.setattr(clips, "get_storage", lambda _: storage)
    response = clips.clip_media(ws, cid, db, None)
    assert isinstance(response, RedirectResponse)
    assert response.headers["location"] == storage.signed_url.return_value
    storage.signed_url.assert_called_once()


def test_restoring_a_version_does_not_bypass_qa():
    from app.services.clips import ClipService

    clip = SimpleNamespace(
        current_version_id=uuid.uuid4(), state="REJECTED", publishable=True, meta={}
    )
    version = SimpleNamespace(id=uuid.uuid4(), video_asset_id=uuid.uuid4(), version_no=1)
    service = ClipService.__new__(ClipService)
    service.db = MagicMock()
    service.clips = MagicMock()
    service.clips.get.return_value = clip
    service.clips.get_version.return_value = version
    restored = service.restore_version(
        workspace_id=uuid.uuid4(),
        clip_id=uuid.uuid4(),
        version_id=version.id,
    )
    assert restored.state == "AWAITING_REVIEW"
    assert restored.publishable is False


def test_authenticated_media_route_supports_byte_ranges(monkeypatch, tmp_path):
    ws, cid, db, _storage, _ = _media_setup(monkeypatch, tmp_path)
    app = create_app()
    # Replace only authorization and DB dependencies; retain the real HTTP/file response.
    from app.api.deps import get_db

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[clips.WorkspaceViewer.__metadata__[0].dependency] = lambda: None
    client = TestClient(app)
    response = client.get(
        f"/api/v1/workspaces/{ws}/clips/{cid}/media",
        headers={"Range": "bytes=0-3"},
    )
    assert response.status_code == 206
    assert response.content == b"real"
    assert response.headers["content-range"] == "bytes 0-3/15"


def test_retry_broker_failure_is_recoverable(monkeypatch):
    from app.api.v1 import jobs
    from app.worker.dispatch import BrokerUnavailableError

    job = SimpleNamespace(task_name="analysis.run", project_id=uuid.uuid4())
    repo = MagicMock()
    repo.requeue.return_value = job
    monkeypatch.setattr(jobs, "_job", lambda *_: job)
    monkeypatch.setattr(jobs, "JobRepository", lambda _: repo)
    monkeypatch.setattr(jobs, "dispatch_analysis", MagicMock(side_effect=BrokerUnavailableError()))
    db = MagicMock()
    with pytest.raises(ApiError) as exc:
        jobs.retry_job(
            uuid.uuid4(), uuid.uuid4(), db, SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4()))
        )
    assert exc.value.status_code == 503
    assert repo.mark_failed.call_args.kwargs["retryable"] is True
    assert db.commit.call_count == 2


def test_publishable_filter_is_applied_before_pagination():
    from sqlalchemy.dialects import postgresql

    from app.db.repositories import ClipRepository

    db = MagicMock()
    db.scalars.return_value = []
    ClipRepository(db).list_for_project(
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        publishable=False,
        limit=1,
    )
    sql = str(db.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "clips.publishable IS false" in sql
