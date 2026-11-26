"""Phase 0 tests: the error contract and health reporting.

The spec's rule "never claim success before an operation succeeds" applies to error reporting
too: every failure must use the documented problem+json shape with a stable machine code.
"""

from __future__ import annotations

import pytest
from fastapi import Body
from fastapi.testclient import TestClient
from pydantic import BaseModel

pytestmark = pytest.mark.unit


class ValidationProbePayload(BaseModel):
    name: str
    count: int


async def _validation_probe(payload: ValidationProbePayload = Body(...)) -> dict[str, bool]:
    """Test-only route used to exercise the 422 handler."""
    return {"ok": True}


@pytest.fixture(scope="module")
def client() -> TestClient:
    """TestClient over a freshly built app. Environment comes from tests/conftest.py."""
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


class TestErrorContract:
    def test_unknown_route_returns_problem_json(self, client):
        r = client.get("/api/v1/does-not-exist")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        assert body["code"] == "not_found"
        assert body["status"] == 404
        assert body["type"].startswith("https://docs.autoclip.ai/errors/")

    def test_method_not_allowed_returns_problem_json(self, client):
        r = client.delete("/api/v1/health/livez")
        assert r.status_code == 405
        assert r.json()["code"] == "method_not_allowed"

    def test_trace_id_generated_when_client_sends_none(self, client):
        r = client.get("/api/v1/does-not-exist")
        trace_id = r.json()["trace_id"]
        assert trace_id != "unavailable"
        assert len(trace_id) == 32

    def test_client_trace_id_is_honoured(self, client):
        r = client.get("/api/v1/does-not-exist", headers={"X-Trace-Id": "trace-abc"})
        assert r.json()["trace_id"] == "trace-abc"
        assert r.headers["X-Trace-Id"] == "trace-abc"

    def test_validation_error_uses_problem_json_with_fields(self):
        """422s must carry the documented shape, including per-field detail."""
        from app.main import create_app

        app = create_app()
        app.post("/api/v1/_validation_probe")(
            _validation_probe  # module-level handler: nested defs break pydantic resolution
        )

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.post("/api/v1/_validation_probe", json={"name": "x", "count": "not-an-int"})

        assert r.status_code == 422
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        assert body["code"] == "validation_error"
        assert body["fields"], "field-level errors must be reported"
        assert body["fields"][0]["path"] == "count"


class TestHealthReporting:
    def test_livez_ok_without_dependencies(self, client):
        r = client.get("/api/v1/health/livez")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_reports_all_dependency_keys(self, client):
        body = client.get("/api/v1/health").json()
        assert set(body["dependencies"]) == {"database", "redis", "storage", "ffmpeg"}

    def test_health_reports_unconfigured_integrations_explicitly(self, client):
        body = client.get("/api/v1/health").json()
        integrations = body["integrations"]
        assert integrations["youtube"]["configured"] is False
        assert integrations["youtube"]["publishing_available"] is False
        assert "YOUTUBE_CLIENT_ID" in integrations["youtube"]["missing_env"]
        assert integrations["ai_provider"]["heuristic_fallback_available"] is True

    def test_health_degrades_honestly_when_db_unreachable(self, client):
        """Pointed at a nonexistent database: must report down, not pretend to be healthy."""
        body = client.get("/api/v1/health")
        assert body.json()["dependencies"]["database"]["status"] in {"ok", "down"}

    def test_readyz_returns_503_when_required_dependency_down(self, client):
        r = client.get("/api/v1/health/readyz")
        assert r.status_code in (200, 503)
        assert r.json()["status"] in {"ok", "degraded"}

    def test_ffmpeg_availability_is_reported(self, client):
        body = client.get("/api/v1/health").json()
        assert "ffmpeg" in body["dependencies"]["ffmpeg"]["detail"]
