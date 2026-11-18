"""Tests /login, /logout, /me and app/auth_middleware.py's enforcement, through the real app."""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app import audit_log, auth, users


def test_login_page_is_reachable_without_a_session():
    resp = TestClient(main_module.app).get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_an_unauthenticated_api_request_gets_401_json():
    resp = TestClient(main_module.app).get("/me", headers={"accept": "*/*"})
    assert resp.status_code == 401


def test_an_unauthenticated_html_request_redirects_to_login():
    resp = TestClient(main_module.app, follow_redirects=False).get(
        "/me", headers={"accept": "text/html"}
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_an_unauthenticated_websocket_is_closed(app_state):
    anon = TestClient(main_module.app)
    try:
        with anon.websocket_connect("/ws/console/R1"):
            raise AssertionError("should have been refused before accept")
    except Exception as exc:
        assert "1008" in str(exc) or "WebSocketDisconnect" in type(exc).__name__


def test_health_and_login_never_require_a_session():
    anon = TestClient(main_module.app)
    assert anon.get("/health").status_code == 200
    assert anon.get("/login").status_code == 200


def test_admin_page_itself_is_gated_like_everything_else():
    anon = TestClient(main_module.app, follow_redirects=False)
    resp = anon.get("/admin.html", headers={"accept": "text/html"})
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_admin_page_is_reachable_once_logged_in(client):
    resp = client.get("/admin.html")
    assert resp.status_code == 200
    assert "Admin Panel" in resp.text


def test_login_with_correct_credentials_sets_a_cookie_and_redirects(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", str(tmp_path / "users.json"))
    users.create_user("alice", "correct-horse", "user")
    anon = TestClient(main_module.app, follow_redirects=False)
    resp = anon.post("/login", data={"username": "alice", "password": "correct-horse"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?enter=1"
    assert auth.SESSION_COOKIE in resp.cookies


def test_login_with_the_wrong_password_redirects_back_with_an_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", str(tmp_path / "users.json"))
    users.create_user("alice", "correct-horse", "user")
    anon = TestClient(main_module.app, follow_redirects=False)
    resp = anon.post("/login", data={"username": "alice", "password": "wrong"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=1"
    assert auth.SESSION_COOKIE not in resp.cookies


def test_login_success_and_failure_are_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", str(tmp_path / "users.json"))
    users.create_user("alice", "correct-horse", "user")
    anon = TestClient(main_module.app, follow_redirects=False)

    anon.post("/login", data={"username": "alice", "password": "wrong"})
    anon.post("/login", data={"username": "alice", "password": "correct-horse"})

    events = audit_log.recent()
    assert [e["event"] for e in events] == ["login_success", "login_failure"]
    assert all(e["username"] == "alice" for e in events)


def test_me_reports_the_logged_in_user(client):
    resp = client.get("/me")
    assert resp.status_code == 200
    assert resp.json() == {"username": "test-admin", "role": "admin"}


def test_logout_clears_the_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", str(tmp_path / "users.json"))
    users.create_user("alice", "correct-horse", "user")
    session = TestClient(main_module.app, follow_redirects=False)
    session.post("/login", data={"username": "alice", "password": "correct-horse"})
    assert session.get("/me", headers={"accept": "*/*"}).status_code == 200

    resp = session.post("/logout")
    assert resp.status_code == 303
    assert session.get("/me", headers={"accept": "*/*"}).status_code == 401

    events = audit_log.recent()
    assert events[0]["event"] == "logout"
    assert events[0]["username"] == "alice"


def test_logout_without_a_valid_session_is_not_audited():
    anon = TestClient(main_module.app, follow_redirects=False)
    assert anon.post("/logout").status_code == 303
    assert audit_log.recent() == []


# --- readonly role ----------------------------------------------------


def test_readonly_can_make_safe_requests(readonly_client):
    assert readonly_client.get("/me").status_code == 200
    assert readonly_client.get("/health").status_code == 200


def test_readonly_is_blocked_from_mutating_an_arbitrary_route(readonly_client):
    resp = readonly_client.post("/nodes", json={"name": "x"})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Read-only access"}


def test_readonly_is_blocked_from_a_mutation_even_with_an_html_accept_header(
    readonly_client,
):
    resp = readonly_client.post(
        "/nodes", json={"name": "x"}, headers={"accept": "text/html"}
    )
    assert resp.status_code == 403
    assert "Read-only" in resp.text


def test_readonly_can_still_log_out():
    session = TestClient(main_module.app, follow_redirects=False)
    session.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("test-readonly", "readonly")
    )
    assert session.post("/logout").status_code == 303


def test_user_and_admin_roles_are_unaffected_by_the_readonly_check(client, user_client):
    # Both still 403 on an admin-only route for the usual (role, not readonly) reason,
    # proving the readonly method-block isn't what's firing here.
    assert user_client.post("/admin/users", json={}).status_code == 403
    assert (
        client.post("/admin/users", json={}).status_code == 422
    )  # missing fields, reached the route


def test_readonly_websocket_console_access_is_not_blocked_by_the_method_check(
    readonly_client, app_state, fake_tcp_server
):
    from unittest.mock import patch

    port = fake_tcp_server(lambda reader, writer: None)
    live_node = {
        "name": "R1",
        "node_id": "n1",
        "status": "started",
        "console": port,
        "console_type": "telnet",
    }
    app_state.project_id = "proj-1"
    with patch("app.console.list_live_nodes", return_value=[live_node]):
        with readonly_client.websocket_connect("/ws/console/R1"):
            pass  # reaching accept() at all proves the readonly check didn't 1008 it
