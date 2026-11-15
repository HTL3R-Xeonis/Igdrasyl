"""Tests /login, /logout, /me and app/auth_middleware.py's enforcement, through the real app."""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app import auth, users


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
    assert resp.headers["location"] == "/"
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
