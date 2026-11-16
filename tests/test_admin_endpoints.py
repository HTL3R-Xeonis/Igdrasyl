"""Tests the admin-only endpoints (app/routes/admin.py) and the admin gate on app/routes/host_settings.py."""

from __future__ import annotations

from unittest.mock import Mock

from app import audit_log, log_summary


def test_regular_user_cannot_summarize_logs(user_client):
    assert user_client.post("/admin/logs/summarize").status_code == 403


def test_admin_can_summarize_logs(client, monkeypatch):
    monkeypatch.setattr(log_summary, "recent_log_text", lambda: "log text")
    monkeypatch.setattr(log_summary, "summarize", Mock(return_value="short summary"))
    resp = client.post("/admin/logs/summarize")
    assert resp.status_code == 200
    assert resp.json() == {"summary": "short summary"}
    log_summary.summarize.assert_called_once_with("log text")


def test_summarize_logs_maps_timeout_to_504(client, monkeypatch):
    monkeypatch.setattr(log_summary, "recent_log_text", lambda: "log text")

    def _raise(_text):
        raise TimeoutError("took too long")

    monkeypatch.setattr(log_summary, "summarize", _raise)
    assert client.post("/admin/logs/summarize").status_code == 504


def test_summarize_logs_maps_a_generator_failure_to_502(client, monkeypatch):
    monkeypatch.setattr(log_summary, "recent_log_text", lambda: "log text")

    def _raise(_text):
        raise RuntimeError("generator unreachable")

    monkeypatch.setattr(log_summary, "summarize", _raise)
    assert client.post("/admin/logs/summarize").status_code == 502


def test_regular_user_cannot_list_or_create_users(user_client):
    assert user_client.get("/admin/users").status_code == 403
    resp = user_client.post(
        "/admin/users",
        json={"username": "x", "password": "longenough1", "role": "user"},
    )
    assert resp.status_code == 403


def test_admin_can_create_list_and_delete_a_user(client):
    created = client.post(
        "/admin/users",
        json={"username": "alice", "password": "correct-horse", "role": "user"},
    )
    assert created.status_code == 201
    assert created.json()["role"] == "user"

    listed = client.get("/admin/users").json()
    assert any(entry["username"] == "alice" for entry in listed)
    assert "password_hash" not in listed[0]

    deleted = client.delete("/admin/users/alice")
    assert deleted.status_code == 200
    assert "alice" not in [
        entry["username"] for entry in client.get("/admin/users").json()
    ]


def test_admin_cannot_create_a_user_with_a_bad_role(client):
    resp = client.post(
        "/admin/users",
        json={"username": "alice", "password": "correct-horse", "role": "superadmin"},
    )
    assert resp.status_code == 400


def test_deleting_an_unknown_user_404s(client):
    assert client.delete("/admin/users/ghost").status_code == 404


def test_set_role_and_set_password(client):
    client.post(
        "/admin/users",
        json={"username": "alice", "password": "correct-horse", "role": "user"},
    )
    resp = client.put("/admin/users/alice/role", json={"role": "admin"})
    assert resp.status_code == 200 and resp.json()["role"] == "admin"

    resp = client.put(
        "/admin/users/alice/password", json={"password": "new-password-1"}
    )
    assert resp.status_code == 200


def test_ai_config_round_trip_and_is_admin_only(client, user_client):
    url = "/admin/ai-config"
    assert user_client.get(url).status_code == 403
    assert user_client.put(url, json={"enabled": True}).status_code == 403
    assert client.get(url).json()["enabled"] is False
    assert client.put(url, json={"enabled": "true"}).status_code == 422

    updated = client.put(url, json={"enabled": True})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True
    assert client.get(url).json()["enabled"] is True
    assert client.put(url, json={"enabled": False}).json()["enabled"] is False


def test_regular_user_cannot_upload_create_edit_delete_or_activate_a_host_profile(
    user_client,
):
    assert (
        user_client.post(
            "/settings-profiles", files={"file": ("x.yml", b"esxi: {}", "text/yaml")}
        ).status_code
        == 403
    )
    assert user_client.delete("/settings-profiles/x.yml").status_code == 403
    assert user_client.post("/settings-profiles/x.yml/activate").status_code == 403
    assert (
        user_client.put(
            "/settings-profiles/x.yml/connections", json={"fields": {}, "secrets": {}}
        ).status_code
        == 403
    )


def test_regular_user_can_still_list_host_profiles(user_client):
    assert user_client.get("/settings-profiles").status_code == 200


# --- Host restrictions + bulk import ---------------------------------------


def _upload_profile(client, filename, content=b"esxi:\n  ip: 10.0.0.1\n"):
    return client.post(
        "/settings-profiles", files={"file": (filename, content, "application/x-yaml")}
    )


def test_create_user_with_allowed_hosts_round_trips(client):
    created = client.post(
        "/admin/users",
        json={
            "username": "alice",
            "password": "correct-horse",
            "role": "user",
            "allowed_hosts": ["lab-a.yml"],
        },
    )
    assert created.status_code == 201
    assert created.json()["allowed_hosts"] == ["lab-a.yml"]


def test_set_user_allowed_hosts(client):
    client.post(
        "/admin/users",
        json={"username": "alice", "password": "correct-horse", "role": "user"},
    )
    resp = client.put(
        "/admin/users/alice/allowed-hosts", json={"allowed_hosts": ["lab-a.yml"]}
    )
    assert resp.status_code == 200
    assert resp.json()["allowed_hosts"] == ["lab-a.yml"]

    assert (
        client.put(
            "/admin/users/ghost/allowed-hosts", json={"allowed_hosts": "ALL"}
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/admin/users/alice/allowed-hosts", json={"allowed_hosts": "nonsense"}
        ).status_code
        == 400
    )


def test_regular_user_cannot_set_allowed_hosts(user_client):
    resp = user_client.put(
        "/admin/users/alice/allowed-hosts", json={"allowed_hosts": "ALL"}
    )
    assert resp.status_code == 403


def test_a_restricted_user_only_sees_their_allowed_host_profiles(
    client, monkeypatch, tmp_path
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path))
    _upload_profile(client, "lab-a.yml")
    _upload_profile(client, "lab-b.yml")
    client.post(
        "/admin/users",
        json={
            "username": "restricted-user",
            "password": "correct-horse",
            "role": "user",
            "allowed_hosts": ["lab-a.yml"],
        },
    )

    from fastapi.testclient import TestClient

    import app.main as main_module
    from app import auth

    restricted_client = TestClient(main_module.app)
    restricted_client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("restricted-user", "user")
    )

    filenames = {
        p["filename"] for p in restricted_client.get("/settings-profiles").json()
    }
    assert filenames == {"lab-a.yml"}
    # admin always sees every profile regardless of its own allowed_hosts
    assert {p["filename"] for p in client.get("/settings-profiles").json()} == {
        "lab-a.yml",
        "lab-b.yml",
    }


def test_bulk_create_users_mixed_success_and_failure(client):
    resp = client.post(
        "/admin/users/bulk",
        json={
            "lines": "alice;secret123\nbad-line-no-semicolon\nbob;anotherpass",
            "role": "user",
            "allowed_hosts": "ALL",
        },
    )
    assert resp.status_code == 200
    results = resp.json()
    assert [r["status"] for r in results] == ["created", "error", "created"]

    listed = {u["username"] for u in client.get("/admin/users").json()}
    assert {"alice", "bob"}.issubset(listed)


def test_bulk_create_users_is_admin_only(user_client):
    resp = user_client.post(
        "/admin/users/bulk", json={"lines": "x;secret123", "role": "user"}
    )
    assert resp.status_code == 403


# --- Audit log --------------------------------------------------------


def test_user_management_actions_are_audited(client):
    client.post(
        "/admin/users",
        json={"username": "other-admin", "password": "correct-horse", "role": "admin"},
    )
    client.post(
        "/admin/users",
        json={"username": "alice", "password": "correct-horse", "role": "user"},
    )
    client.put("/admin/users/alice/role", json={"role": "admin"})
    client.put("/admin/users/alice/password", json={"password": "new-password-1"})
    client.put(
        "/admin/users/alice/allowed-hosts", json={"allowed_hosts": ["lab-a.yml"]}
    )
    client.delete("/admin/users/alice")

    events = audit_log.recent()
    assert [e["event"] for e in events] == [
        "user_deleted",
        "user_allowed_hosts_changed",
        "user_password_changed",
        "user_role_changed",
        "user_created",
        "user_created",
    ]
    assert all(e["username"] == "test-admin" for e in events)
    assert events[3]["detail"] == "alice -> admin"


def test_bulk_create_only_audits_the_successfully_created_lines(client):
    client.post(
        "/admin/users/bulk",
        json={"lines": "alice;secret123\nbad-line\nbob;anotherpass", "role": "user"},
    )
    events = audit_log.recent()
    created = [e for e in events if e["event"] == "user_created"]
    assert {e["detail"] for e in created} == {"alice", "bob"}


def test_ai_config_change_is_audited(client):
    client.put("/admin/ai-config", json={"enabled": True})
    events = audit_log.recent()
    assert events[0]["event"] == "ai_snippet_topics_changed"
    assert events[0]["detail"] == "enabled=True"


def test_audit_log_endpoint_is_admin_only(client, user_client):
    assert user_client.get("/admin/audit-log").status_code == 403
    client.put("/admin/ai-config", json={"enabled": True})
    events = client.get("/admin/audit-log").json()
    assert events[0]["event"] == "ai_snippet_topics_changed"
