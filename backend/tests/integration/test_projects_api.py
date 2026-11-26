"""Integration tests: projects, workspace isolation, and the state machine."""

from __future__ import annotations

import uuid

import pytest

from tests.integration.conftest import Actor


class TestProjectCrud:
    def test_create_project_starts_in_created_state(self, alice: Actor):
        project = alice.create_project(name="Weekly Show")
        assert project["status"] == "CREATED"
        assert project["name"] == "Weekly Show"

    def test_creation_writes_first_journal_entry(self, alice: Actor):
        project = alice.create_project()
        resp = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project['id']}/stages"
        )
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) == 1
        assert events[0]["from_status"] is None
        assert events[0]["to_status"] == "CREATED"

    def test_list_is_scoped_and_paginated(self, alice: Actor):
        for i in range(3):
            alice.create_project(name=f"Project {i}")
        resp = alice.client.get(f"/api/v1/workspaces/{alice.workspace_id}/projects?limit=2")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None

        page2 = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/projects?limit=2&cursor={body['next_cursor']}"
        )
        assert len(page2.json()["items"]) == 1

    def test_filter_by_status(self, alice: Actor):
        alice.create_project(name="A")
        resp = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/projects?status=CREATED"
        )
        assert len(resp.json()["items"]) == 1
        empty = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/projects?status=PUBLISHED"
        )
        assert empty.json()["items"] == []

    def test_update_changes_settings_not_source(self, alice: Actor):
        project = alice.create_project()
        resp = alice.client.patch(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project['id']}",
            json={"name": "Renamed", "clips_requested": 8},
            headers=alice.headers,
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"
        assert resp.json()["clips_requested"] == 8

    def test_invalid_duration_range_rejected(self, alice: Actor):
        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects",
            json={"name": "Bad", "min_duration_ms": 50000, "max_duration_ms": 20000},
            headers=alice.headers,
        )
        assert resp.status_code == 422

    def test_delete_is_soft_and_hides_project(self, alice: Actor):
        project = alice.create_project()
        resp = alice.client.delete(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project['id']}",
            headers=alice.headers,
        )
        assert resp.status_code == 204
        gone = alice.client.get(f"/api/v1/workspaces/{alice.workspace_id}/projects/{project['id']}")
        assert gone.status_code == 404
        listing = alice.client.get(f"/api/v1/workspaces/{alice.workspace_id}/projects")
        assert listing.json()["items"] == []


class TestCsrfProtection:
    def test_mutation_without_csrf_is_forbidden(self, alice: Actor):
        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects",
            json={"name": "No CSRF"},
        )
        assert resp.status_code == 403

    def test_mutation_with_wrong_csrf_is_forbidden(self, alice: Actor):
        resp = alice.client.post(
            f"/api/v1/workspaces/{alice.workspace_id}/projects",
            json={"name": "Bad CSRF"},
            headers={"X-CSRF-Token": "not-the-right-token"},
        )
        assert resp.status_code == 403

    def test_reads_do_not_require_csrf(self, alice: Actor):
        project = alice.create_project()
        resp = alice.client.get(f"/api/v1/workspaces/{alice.workspace_id}/projects/{project['id']}")
        assert resp.status_code == 200


class TestWorkspaceIsolation:
    """The core multi-tenancy guarantee. Non-members must see 404, never data and never 403."""

    def test_cannot_read_other_workspace_project(self, alice: Actor, bob: Actor):
        """Authorization fails at the workspace boundary, before the project is looked up.

        The code is `workspace_not_found` (not `project_not_found`) because membership is checked
        first — which is the stronger guarantee: the caller learns nothing about whether the
        project exists at all. Either code is acceptable only insofar as it is a 404 and leaks
        no data; we assert the boundary behaviour explicitly.
        """
        project = alice.create_project(name="Alice Secret")
        resp = bob.client.get(f"/api/v1/workspaces/{alice.workspace_id}/projects/{project['id']}")
        assert resp.status_code == 404
        assert resp.json()["code"] in {"workspace_not_found", "project_not_found"}
        assert "Alice Secret" not in resp.text
        assert project["id"] not in resp.text

    def test_cannot_list_other_workspace_projects(self, alice: Actor, bob: Actor):
        alice.create_project(name="Alice Secret")
        resp = bob.client.get(f"/api/v1/workspaces/{alice.workspace_id}/projects")
        assert resp.status_code == 404

    def test_cannot_read_other_workspace_dashboard(self, alice: Actor, bob: Actor):
        alice.create_project()
        resp = bob.client.get(f"/api/v1/workspaces/{alice.workspace_id}/dashboard")
        assert resp.status_code == 404

    def test_cannot_modify_other_workspace_project(self, alice: Actor, bob: Actor):
        project = alice.create_project(name="Alice Secret")
        resp = bob.client.patch(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project['id']}",
            json={"name": "Hijacked"},
            headers=bob.headers,
        )
        assert resp.status_code == 404
        # Confirm nothing changed, reading as the owner.
        check = alice.client.get(
            f"/api/v1/workspaces/{alice.workspace_id}/projects/{project['id']}"
        )
        assert check.json()["name"] == "Alice Secret"

    def test_project_id_from_other_workspace_under_own_workspace_is_404(
        self, alice: Actor, bob: Actor
    ):
        """Even with a valid own workspace, a foreign project id resolves to nothing."""
        project = alice.create_project()
        resp = bob.client.get(f"/api/v1/workspaces/{bob.workspace_id}/projects/{project['id']}")
        assert resp.status_code == 404

    def test_random_uuid_is_404_not_500(self, alice: Actor):
        resp = alice.client.get(f"/api/v1/workspaces/{alice.workspace_id}/projects/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestStageMachine:
    def test_illegal_transition_is_rejected(self, alice: Actor):
        from app.db.repositories import ConflictError, ProjectRepository
        from app.db.session import get_session_factory

        project = alice.create_project()
        db = get_session_factory()()
        try:
            repo = ProjectRepository(db)
            with pytest.raises(ConflictError) as exc:
                repo.transition(
                    workspace_id=uuid.UUID(alice.workspace_id),
                    project_id=uuid.UUID(project["id"]),
                    target="PUBLISHED",
                )
            assert exc.value.code == "invalid_transition"
        finally:
            db.rollback()
            db.close()

    def test_legal_transition_is_journalled(self, alice: Actor):
        from app.db.repositories import ProjectRepository
        from app.db.session import get_session_factory

        project = alice.create_project()
        db = get_session_factory()()
        try:
            repo = ProjectRepository(db)
            repo.transition(
                workspace_id=uuid.UUID(alice.workspace_id),
                project_id=uuid.UUID(project["id"]),
                target="SOURCE_VALIDATION",
                task_name="validate_source",
            )
            db.commit()
            events = repo.stage_events(
                workspace_id=uuid.UUID(alice.workspace_id),
                project_id=uuid.UUID(project["id"]),
            )
            assert [e.to_status for e in events] == ["CREATED", "SOURCE_VALIDATION"]
            assert events[-1].task_name == "validate_source"
            assert events[-1].from_status == "CREATED"
        finally:
            db.close()

    def test_failed_transition_does_not_write_journal(self, alice: Actor):
        from app.db.repositories import ConflictError, ProjectRepository
        from app.db.session import get_session_factory

        project = alice.create_project()
        db = get_session_factory()()
        try:
            repo = ProjectRepository(db)
            with pytest.raises(ConflictError):
                repo.transition(
                    workspace_id=uuid.UUID(alice.workspace_id),
                    project_id=uuid.UUID(project["id"]),
                    target="PUBLISHED",
                )
            db.rollback()
            events = repo.stage_events(
                workspace_id=uuid.UUID(alice.workspace_id),
                project_id=uuid.UUID(project["id"]),
            )
            assert len(events) == 1, "illegal transitions must not be journalled"
        finally:
            db.close()


class TestDashboard:
    def test_empty_workspace_reports_zeros_and_empty_flag(self, alice: Actor):
        resp = alice.client.get(f"/api/v1/workspaces/{alice.workspace_id}/dashboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["projects_total"] == 0
        assert body["is_empty"] is True
        assert body["recent_projects"] == []

    def test_counts_match_database(self, alice: Actor):
        alice.create_project(name="One")
        alice.create_project(name="Two")
        body = alice.client.get(f"/api/v1/workspaces/{alice.workspace_id}/dashboard").json()
        assert body["projects_total"] == 2
        assert body["projects_by_status"] == {"CREATED": 2}
        assert len(body["recent_projects"]) == 2
        assert body["is_empty"] is False

    def test_dashboard_is_workspace_scoped(self, alice: Actor, bob: Actor):
        alice.create_project()
        alice.create_project()
        bob_body = bob.client.get(f"/api/v1/workspaces/{bob.workspace_id}/dashboard").json()
        assert bob_body["projects_total"] == 0
