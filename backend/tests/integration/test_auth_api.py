"""Integration tests: authentication against a real database and Redis."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.integration.conftest import Actor

SESSION_COOKIE = "ac_session"


class TestRegistration:
    def test_register_creates_user_workspace_and_owner_membership(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": "new@example.com",
                "password": "a-solid-password",
                "display_name": "New Person",
                "workspace_name": "New Studio",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user"]["email"] == "new@example.com"
        assert len(body["user"]["workspaces"]) == 1
        assert body["user"]["workspaces"][0]["role"] == "owner"
        assert body["csrf_token"]
        # Session cookie is set with the required attributes.
        cookie = resp.headers["set-cookie"]
        assert SESSION_COOKIE in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie.replace("samesite", "SameSite")

    def test_duplicate_email_is_rejected_with_409(self, client: TestClient, alice: Actor):
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": "alice@example.com",
                "password": "another-password",
                "display_name": "Imposter",
            },
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_short_password_rejected(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "x@example.com", "password": "short", "display_name": "X"},
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_invalid_email_rejected(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "password": "a-solid-password", "display_name": "X"},
        )
        assert resp.status_code == 422


class TestLogin:
    def test_login_succeeds_and_rotates_session(self, alice: Actor):
        first_cookie = alice.client.cookies.get(SESSION_COOKIE)
        resp = alice.client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "alice-strong-password"},
        )
        assert resp.status_code == 200
        assert alice.client.cookies.get(SESSION_COOKIE) != first_cookie, (
            "session must rotate on login"
        )

    def test_wrong_password_rejected(self, client: TestClient, alice: Actor):
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "wrong-password"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_credentials"

    def test_unknown_account_gives_identical_error_to_wrong_password(self, client: TestClient):
        """No user enumeration: same status and code for both failure modes."""
        unknown = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "whatever-password"},
        )
        Actor(client, "someone@example.com", "their-strong-password", "Some Studio")
        wrong = client.post(
            "/api/v1/auth/login",
            json={"email": "someone@example.com", "password": "wrong-password"},
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["code"] == wrong.json()["code"] == "invalid_credentials"
        assert unknown.json()["detail"] == wrong.json()["detail"]

    def test_repeated_failures_are_rate_limited(self, client: TestClient, alice: Actor):
        """Throttling lives in Redis so it holds across API replicas."""
        codes = []
        for _ in range(7):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": "alice@example.com", "password": "definitely-wrong"},
            )
            codes.append(resp.status_code)
        assert 429 in codes, f"expected rate limiting, got {codes}"
        limited = client.post(
            "/api/v1/auth/login",
            json={"email": "alice@example.com", "password": "alice-strong-password"},
        )
        assert limited.status_code == 429
        assert "Retry-After" in limited.headers


class TestSessionLifecycle:
    def test_me_requires_authentication(self, client: TestClient):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401
        assert resp.json()["code"] == "unauthenticated"

    def test_me_returns_profile_and_memberships(self, alice: Actor):
        resp = alice.client.get("/api/v1/auth/me")
        assert resp.status_code == 200
        assert resp.json()["email"] == "alice@example.com"
        assert resp.json()["workspaces"][0]["role"] == "owner"

    def test_logout_revokes_session_server_side(self, alice: Actor):
        """Deleting the cookie client-side is not logout; the row must be revoked server-side."""
        stolen = alice.client.cookies.get(SESSION_COOKIE)
        resp = alice.client.post("/api/v1/auth/logout", headers=alice.headers)
        assert resp.status_code == 204

        # Replay the captured token directly: it must be dead.
        replay = alice.client.get("/api/v1/auth/me", cookies={SESSION_COOKIE: stolen})
        assert replay.status_code == 401

    def test_logout_requires_csrf_for_cookie_auth(self, alice: Actor):
        resp = alice.client.post("/api/v1/auth/logout")  # deliberately no CSRF header
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"

    def test_bearer_token_auth_path(self, alice: Actor):
        token = alice.client.cookies.get(SESSION_COOKIE)
        resp = alice.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_invalid_token_rejected(self, client: TestClient):
        resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nonsense"})
        assert resp.status_code == 401

    def test_password_hash_is_never_serialised(self, alice: Actor):
        body = alice.client.get("/api/v1/auth/me").text
        assert "argon2" not in body
        assert "password" not in body.lower()
