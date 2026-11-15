"""Tests the admin-only endpoints (app/routes/admin.py) and the admin gate on app/routes/host_settings.py."""

from __future__ import annotations

from unittest.mock import Mock

from app import log_summary


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


def test_ai_snippet_topics_round_trip_and_are_admin_only(client, user_client):
    assert user_client.get("/admin/ai-snippet-topics").status_code == 403

    initial = client.get("/admin/ai-snippet-topics").json()
    assert initial["enabled"] == []
    assert "vlan" in initial["topics"]

    updated = client.put("/admin/ai-snippet-topics", json={"topics": ["vlan", "bogus"]})
    assert updated.status_code == 200
    assert updated.json()["enabled"] == ["vlan"]  # "bogus" silently dropped

    assert client.get("/admin/ai-snippet-topics").json()["enabled"] == ["vlan"]


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
