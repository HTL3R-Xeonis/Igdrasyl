"""
Phase 3: everything app/routes_gateway.py moved onto the gateway from
app/routes/admin.py and app/routes/host_settings.py (user accounts, AI
snippet topics, settings-profile CRUD, host-config, OVA templates), plus
what's genuinely new there - activate's changed meaning, the explicit
restart action, and app/proxy.py's get_target callback.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app import backend_manager, backend_ports, routes_gateway


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path / "profiles")
    )
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_BACKEND_PORTS_FILE", str(tmp_path / "ports.json")
    )
    monkeypatch.delenv("TOPOLOGYOPERATOR_SETTINGS", raising=False)
    yield


def _upload(gateway_client, filename="host-a.yml", content=b"esxi:\n  ip: 10.0.0.1\n"):
    return gateway_client.post(
        "/settings-profiles",
        files={"file": (filename, content, "application/x-yaml")},
    )


# --- User accounts ----------------------------------------------------


def test_regular_user_cannot_list_or_create_users(gateway_user_client):
    assert gateway_user_client.get("/admin/users").status_code == 403
    resp = gateway_user_client.post(
        "/admin/users",
        json={"username": "x", "password": "longenough1", "role": "user"},
    )
    assert resp.status_code == 403


def test_admin_can_create_list_and_delete_a_user(gateway_client):
    created = gateway_client.post(
        "/admin/users",
        json={"username": "alice", "password": "correct-horse", "role": "user"},
    )
    assert created.status_code == 201
    assert created.json()["role"] == "user"

    listed = gateway_client.get("/admin/users").json()
    assert any(entry["username"] == "alice" for entry in listed)
    assert "password_hash" not in listed[0]

    deleted = gateway_client.delete("/admin/users/alice")
    assert deleted.status_code == 200
    assert "alice" not in [
        entry["username"] for entry in gateway_client.get("/admin/users").json()
    ]


def test_admin_cannot_create_a_user_with_a_bad_role(gateway_client):
    resp = gateway_client.post(
        "/admin/users",
        json={"username": "alice", "password": "correct-horse", "role": "superadmin"},
    )
    assert resp.status_code == 400


def test_deleting_an_unknown_user_404s(gateway_client):
    assert gateway_client.delete("/admin/users/ghost").status_code == 404


def test_set_role_and_set_password(gateway_client):
    gateway_client.post(
        "/admin/users",
        json={"username": "alice", "password": "correct-horse", "role": "user"},
    )
    resp = gateway_client.put("/admin/users/alice/role", json={"role": "admin"})
    assert resp.status_code == 200 and resp.json()["role"] == "admin"

    resp = gateway_client.put(
        "/admin/users/alice/password", json={"password": "new-password-1"}
    )
    assert resp.status_code == 200


# --- AI configuration ---------------------------------------------------


def test_ai_config_round_trip_and_is_admin_only(gateway_client, gateway_user_client):
    url = "/admin/ai-config"
    assert gateway_user_client.get(url).status_code == 403
    assert gateway_user_client.put(url, json={"enabled": True}).status_code == 403
    assert gateway_client.get(url).json()["enabled"] is False
    assert gateway_client.put(url, json={"enabled": "true"}).status_code == 422

    updated = gateway_client.put(url, json={"enabled": True})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True
    assert gateway_client.get(url).json()["enabled"] is True
    assert gateway_client.put(url, json={"enabled": False}).json()["enabled"] is False


# --- Settings-profile CRUD ----------------------------------------------


def test_regular_user_cannot_upload_create_edit_or_delete_a_host_profile(
    gateway_user_client,
):
    assert (
        gateway_user_client.post(
            "/settings-profiles", files={"file": ("x.yml", b"esxi: {}", "text/yaml")}
        ).status_code
        == 403
    )
    assert gateway_user_client.delete("/settings-profiles/x.yml").status_code == 403
    assert (
        gateway_user_client.put(
            "/settings-profiles/x.yml/connections", json={"fields": {}, "secrets": {}}
        ).status_code
        == 403
    )


def test_regular_user_can_still_list_host_profiles(gateway_user_client):
    assert gateway_user_client.get("/settings-profiles").status_code == 200


def test_settings_profiles_upload_list_and_delete_round_trip(gateway_client):
    resp = _upload(gateway_client)
    assert resp.status_code == 200, resp.text

    profiles = gateway_client.get("/settings-profiles").json()
    assert len(profiles) == 1
    assert profiles[0]["filename"] == "host-a.yml"
    assert profiles[0]["esxi_ip"] == "10.0.0.1"

    resp = gateway_client.delete("/settings-profiles/host-a.yml")
    assert resp.status_code == 200, resp.text
    assert gateway_client.get("/settings-profiles").json() == []


def test_settings_profiles_upload_rejects_invalid_yaml(gateway_client):
    resp = _upload(gateway_client, content=b"esxi: [unclosed")
    assert resp.status_code == 400


def test_settings_profiles_delete_404s_for_unknown_file(gateway_client):
    assert gateway_client.delete("/settings-profiles/nope.yml").status_code == 404


def test_deleting_a_profile_also_forgets_its_assigned_port(gateway_client):
    _upload(gateway_client)
    backend_ports.assign_port("host-a.yml")
    gateway_client.delete("/settings-profiles/host-a.yml")
    assert backend_ports.get_port("host-a.yml") is None


# --- Activate (any user) / restart (admin only) --------------------------


def test_activate_starts_the_backend_waits_healthy_and_sets_the_cookie(
    gateway_client, monkeypatch
):
    _upload(gateway_client)
    calls: list[str] = []
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: False)
    monkeypatch.setattr(
        backend_manager,
        "start",
        lambda filename, **kw: calls.append(filename),
    )
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda port, **kw: True)

    resp = gateway_client.post("/settings-profiles/host-a.yml/activate")

    assert resp.status_code == 200
    assert calls == ["host-a.yml"]
    assert gateway_client.cookies.get(routes_gateway.BACKEND_COOKIE) == "host-a.yml"


def test_regular_user_can_activate_a_backend(
    gateway_client, gateway_user_client, monkeypatch
):
    # Phase 1's admin-only gate on activate doesn't apply anymore: it used to restart the one
    # shared process for everyone, now it only ever affects the caller's own session. Uploading
    # the profile itself is still admin-only, hence gateway_client (not _user_client) here.
    _upload(gateway_client)
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda port, **kw: True)

    resp = gateway_user_client.post("/settings-profiles/host-a.yml/activate")

    assert resp.status_code == 200


def test_activate_404s_for_an_unknown_profile(gateway_client):
    resp = gateway_client.post("/settings-profiles/nope.yml/activate")
    assert resp.status_code == 404


def test_activate_503s_when_the_backend_never_becomes_healthy(
    gateway_client, monkeypatch
):
    _upload(gateway_client)
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda port, **kw: False)

    resp = gateway_client.post("/settings-profiles/host-a.yml/activate")

    assert resp.status_code == 503


def test_regular_user_cannot_restart_a_backend(gateway_user_client, monkeypatch):
    _upload(gateway_user_client)
    resp = gateway_user_client.post("/settings-profiles/host-a.yml/restart")
    assert resp.status_code == 403


def test_admin_can_restart_a_backend(gateway_client, monkeypatch):
    _upload(gateway_client)
    calls: list[str] = []
    monkeypatch.setattr(
        backend_manager, "restart", lambda filename, **kw: calls.append(filename)
    )

    resp = gateway_client.post("/settings-profiles/host-a.yml/restart")

    assert resp.status_code == 200
    assert calls == ["host-a.yml"]


def test_restart_404s_for_an_unknown_profile(gateway_client):
    assert gateway_client.post("/settings-profiles/nope.yml/restart").status_code == 404


# --- get_target (app/proxy.py's own GetTarget callback) -----------------


def _scope_with_cookie(value: str | None) -> dict:
    headers = (
        [(b"cookie", f"{routes_gateway.BACKEND_COOKIE}={value}".encode())]
        if value
        else []
    )
    return {"type": "http", "headers": headers}


def test_get_target_is_none_without_a_cookie():
    assert routes_gateway.get_target(_scope_with_cookie(None)) is None


def test_get_target_is_none_when_the_backend_is_not_active(monkeypatch):
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: False)
    assert routes_gateway.get_target(_scope_with_cookie("host-a.yml")) is None


def test_get_target_resolves_to_127_0_0_1_and_the_assigned_port(monkeypatch):
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    backend_ports.assign_port("host-a.yml")
    target = routes_gateway.get_target(_scope_with_cookie("host-a.yml"))
    assert target == ("127.0.0.1", backend_ports.get_port("host-a.yml"))


# --- The "pick a backend" page -------------------------------------------


def test_pick_backend_page_lists_profiles_and_requires_login(
    gateway_client, gateway_anonymous_client
):
    _upload(gateway_client, filename="lab.yml", content=b"esxi:\n  ip: 10.0.0.2\n")

    resp = gateway_client.get("/pick-backend")
    assert resp.status_code == 200
    assert "lab.yml" in resp.text
    assert "10.0.0.2" in resp.text

    anon = gateway_anonymous_client.get(
        "/pick-backend", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert anon.status_code == 303
    assert anon.headers["location"] == "/login"


def test_a_browser_with_no_backend_selected_is_redirected_to_the_picker(
    gateway_client,
):
    resp = gateway_client.get(
        "/", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/pick-backend"


# --- /health must reach a backend, not be swallowed by the gateway ------
#
# Found by actually running a gateway and a backend together (deploy/README.md's
# own verification section) - a /health registered directly on the gateway app
# would always win over app/proxy.py's catch-all mount, silently stopping the
# real per-backend /health (gns3_vm_ip, warning, ...) the frontend's own JS
# depends on from ever reaching a backend through the gateway at all.


def test_health_is_not_an_explicit_gateway_route():
    app = FastAPI()
    routes_gateway.register(app)
    paths = {getattr(route, "path", None) for route in app.router.routes}
    assert "/health" not in paths
    assert "/gateway/health" in paths


def test_gateway_health_is_reachable_without_login():
    from app.auth_middleware import _ALLOWED_PATHS

    assert "/gateway/health" in _ALLOWED_PATHS
