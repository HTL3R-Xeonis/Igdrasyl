"""
Everything app/routes_gateway.py moved onto the gateway from
app/routes/admin.py and app/routes/host_settings.py (user accounts, AI
snippet topics, settings-profile CRUD, host-config, OVA templates), plus
what's genuinely new there - activate's changed meaning, the explicit
restart action, and app/proxy.py's get_target callback.
"""

from __future__ import annotations

import pytest
import requests
from fastapi import FastAPI

from app import (
    audit_log,
    backend_manager,
    backend_ports,
    backend_presence,
    routes_gateway,
    users,
)


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


@pytest.fixture(autouse=True)
def _reset_presence():
    backend_presence._presence.clear()
    yield
    backend_presence._presence.clear()


def _upload(gateway_client, filename="host-a.yml", content=b"esxi:\n  ip: 10.0.0.1\n"):
    return gateway_client.post(
        "/settings-profiles",
        files={"file": (filename, content, "application/x-yaml")},
    )


# --- GNS3 VM provisioning ---------------------------------------------


def test_profile_gns3_vm_deploy_keeps_the_current_host(gateway_client, monkeypatch):
    _upload(
        gateway_client,
        content=b"esxi:\n  ip: 10.0.0.1\n  gns3_vm_template_id: gns3-vm\n",
    )
    gateway_client.cookies.set(routes_gateway.BACKEND_COOKIE, "host-b.yml")
    started = []

    def start(filename):
        started.append(filename)
        return 9200

    def read_health(port, **_kwargs):
        assert port == 9200
        response = requests.Response()
        response._content = b'{"gns3_vm_ip":"10.0.0.20","warning":null}'
        return response

    monkeypatch.setattr(backend_manager, "ensure_running", start)
    monkeypatch.setattr(backend_manager, "wait_for_health", read_health)
    url = "/settings-profiles/host-a.yml/deploy-gns3-vm"
    response = gateway_client.post(url)

    assert response.status_code == 202
    assert "set-cookie" not in response.headers
    assert gateway_client.cookies.get(routes_gateway.BACKEND_COOKIE) == "host-b.yml"
    assert started == ["host-a.yml"]
    status = gateway_client.get(f"{url}/{response.json()['job_id']}")
    assert status.status_code == 200
    assert status.json()["status"] == "ready"
    assert status.json()["gns3_vm_ip"] == "10.0.0.20"


def test_profile_gns3_vm_deploy_requires_an_admin_and_template(
    gateway_client, gateway_user_client, monkeypatch
):
    _upload(gateway_client)

    def unexpected_start(_filename):
        pytest.fail("A request rejected at preflight started the backend")

    monkeypatch.setattr(backend_manager, "ensure_running", unexpected_start)
    url = "/settings-profiles/host-a.yml/deploy-gns3-vm"
    assert gateway_user_client.post(url).status_code == 403
    assert gateway_user_client.get(f"{url}/unknown").status_code == 403
    response = gateway_client.post(url)
    assert response.status_code == 409
    assert "Select a GNS3 VM template" in response.json()["detail"]


@pytest.mark.parametrize("backend_answers", [True, False])
def test_profile_gns3_vm_deploy_distinguishes_backend_and_vm_health(
    gateway_client, monkeypatch, backend_answers
):
    _upload(gateway_client)
    backend_ports.assign_port("host-a.yml")
    monkeypatch.setattr(
        backend_manager,
        "is_active",
        lambda filename, **_kwargs: filename == "host-a.yml",
    )

    def unexpected_start(_filename, **_kwargs):
        pytest.fail("An already running backend was started again")

    monkeypatch.setattr(backend_manager, "start", unexpected_start)
    health = requests.Response()
    health._content = b'{"gns3_vm_ip":null,"warning":"Management IP pending"}'
    monkeypatch.setattr(
        backend_manager,
        "wait_for_health",
        lambda _port, **_kwargs: health if backend_answers else None,
    )
    url = "/settings-profiles/host-a.yml/deploy-gns3-vm"
    response = gateway_client.post(url)
    assert response.status_code == 202
    status = gateway_client.get(f"{url}/{response.json()['job_id']}")
    if backend_answers:
        assert status.status_code == 200
        assert status.json()["status"] == "unavailable"
        assert status.json()["warning"] == "Management IP pending"
    else:
        assert status.status_code == 503
        assert "did not answer healthy" in status.json()["detail"]


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


def test_picker_form_redirects_to_backend_and_logout_clears_selection(
    gateway_client, gateway_anonymous_client, monkeypatch
):
    assert _upload(gateway_client).status_code == 200
    monkeypatch.setattr(backend_manager, "ensure_running", lambda _filename: 9200)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda _port: True)

    assert (
        gateway_client.post(
            "/admin/users",
            json={"username": "picker", "password": "correct-horse", "role": "user"},
        ).status_code
        == 201
    )
    session = gateway_anonymous_client
    assert (
        session.post(
            "/login",
            data={"username": "picker", "password": "correct-horse"},
            follow_redirects=False,
        ).status_code
        == 303
    )

    picker = session.get("/pick-backend")
    assert 'action="/logout" method="post"' in picker.text
    response = session.post(
        "/settings-profiles/host-a.yml/activate",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert session.cookies.get(routes_gateway.BACKEND_COOKIE) == "host-a.yml"
    assert session.get("/settings-profiles").json()[0]["active"] is True
    assert gateway_client.get("/settings-profiles").json()[0]["active"] is False

    response = session.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert session.cookies.get(routes_gateway.BACKEND_COOKIE) is None
    assert session.get("/me").status_code == 401


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


def test_activate_and_restart_are_audited(gateway_client, monkeypatch):
    _upload(gateway_client)
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda port, **kw: True)
    monkeypatch.setattr(backend_manager, "restart", lambda filename, **kw: None)

    gateway_client.post("/settings-profiles/host-a.yml/activate")
    gateway_client.post("/settings-profiles/host-a.yml/restart")

    events = audit_log.recent()
    assert [e["event"] for e in events] == ["host_restart", "host_activate"]
    assert all(e["detail"] == "host-a.yml" for e in events)
    assert all(e["username"] == "test-admin" for e in events)


def test_gateway_audit_log_endpoint_is_admin_only(gateway_client, gateway_user_client):
    assert gateway_user_client.get("/admin/audit-log").status_code == 403
    gateway_client.put("/admin/ai-config", json={"enabled": True})
    events = gateway_client.get("/admin/audit-log").json()
    assert events[0]["event"] == "ai_snippet_topics_changed"


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
    users.create_user("alice", "correct-horse", "user")
    target = routes_gateway.get_target(_scope_with_both_cookies("host-a.yml", "alice"))
    assert target == ("127.0.0.1", backend_ports.get_port("host-a.yml"))


def _scope_with_both_cookies(backend: str, username: str, role: str = "user") -> dict:
    from app.auth import SESSION_COOKIE, create_session_token

    token = create_session_token(username, role)
    cookie = f"{routes_gateway.BACKEND_COOKIE}={backend}; {SESSION_COOKIE}={token}"
    return {"type": "http", "headers": [(b"cookie", cookie.encode())]}


def test_get_target_records_presence_on_a_successful_resolve(monkeypatch):
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    backend_ports.assign_port("host-a.yml")
    users.create_user("alice", "correct-horse", "user")
    routes_gateway.get_target(_scope_with_both_cookies("host-a.yml", "alice"))
    assert backend_presence.active_users("host-a.yml") == ["alice"]


def test_get_target_applies_current_host_permissions(monkeypatch):
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    backend_ports.assign_port("host-a.yml")
    users.create_user("alice", "correct-horse", "user")
    scope = _scope_with_both_cookies("host-a.yml", "alice")
    assert routes_gateway.get_target(scope) is not None
    users.set_allowed_hosts("alice", ["host-b.yml"])
    assert routes_gateway.get_target(scope) is None


def test_get_target_records_nothing_when_it_returns_none(monkeypatch):
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: False)
    routes_gateway.get_target(_scope_with_both_cookies("host-a.yml", "alice"))
    assert backend_presence.active_users("host-a.yml") == []


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


# --- Per-user host restrictions ------------------------------------------


def _restricted_client(username, role, allowed_hosts):
    from fastapi.testclient import TestClient

    import app.gateway_main as gateway_module
    from app import auth, users

    users.create_user(username, "correct-horse", role, allowed_hosts)
    test_client = TestClient(gateway_module.app)
    test_client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token(username, role)
    )
    return test_client


def test_settings_profiles_listing_is_filtered_for_a_restricted_user(gateway_client):
    _upload(gateway_client, filename="lab-a.yml", content=b"esxi:\n  ip: 10.0.0.1\n")
    _upload(gateway_client, filename="lab-b.yml", content=b"esxi:\n  ip: 10.0.0.2\n")

    restricted = _restricted_client("restricted-user", "user", ["lab-a.yml"])
    filenames = {p["filename"] for p in restricted.get("/settings-profiles").json()}
    assert filenames == {"lab-a.yml"}

    assert {p["filename"] for p in gateway_client.get("/settings-profiles").json()} == {
        "lab-a.yml",
        "lab-b.yml",
    }


def test_activate_403s_for_a_host_outside_the_users_allowed_list(
    gateway_client, monkeypatch
):
    _upload(gateway_client, filename="lab-a.yml")
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda port, **kw: True)

    restricted = _restricted_client("restricted-user", "user", ["lab-b.yml"])
    resp = restricted.post("/settings-profiles/lab-a.yml/activate")
    assert resp.status_code == 403


def test_activate_still_404s_for_an_unknown_profile_before_the_host_check(
    gateway_client,
):
    restricted = _restricted_client("restricted-user", "user", ["lab-a.yml"])
    resp = restricted.post("/settings-profiles/nope.yml/activate")
    assert resp.status_code == 404


def test_activate_allowed_for_a_host_the_user_is_restricted_to(
    gateway_client, monkeypatch
):
    _upload(gateway_client, filename="lab-a.yml")
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda port, **kw: True)

    restricted = _restricted_client("restricted-user", "user", ["lab-a.yml"])
    resp = restricted.post("/settings-profiles/lab-a.yml/activate")
    assert resp.status_code == 200


def test_admin_can_always_activate_regardless_of_their_own_allowed_hosts(
    gateway_client, monkeypatch
):
    _upload(gateway_client, filename="lab-a.yml")
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda port, **kw: True)

    restricted_admin = _restricted_client("restricted-admin", "admin", ["lab-b.yml"])
    resp = restricted_admin.post("/settings-profiles/lab-a.yml/activate")
    assert resp.status_code == 200


def test_readonly_can_activate_a_backend_but_not_mutate_anything_else(
    gateway_client, gateway_readonly_client, monkeypatch
):
    _upload(gateway_client, filename="lab-a.yml")
    monkeypatch.setattr(backend_manager, "is_active", lambda filename, **kw: True)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda port, **kw: True)

    # The one exempted mutation: picking which backend this reader's own view points at.
    resp = gateway_readonly_client.post("/settings-profiles/lab-a.yml/activate")
    assert resp.status_code == 200

    # Everything else mutating still 403s with the readonly message, not the
    # usual admin-only 403 - proving the blanket method check is what fired.
    resp = gateway_readonly_client.post(
        "/settings-profiles", files={"file": ("x.yml", b"esxi: {}", "text/yaml")}
    )
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Read-only access"}

    assert gateway_readonly_client.get("/settings-profiles").status_code == 200


def test_pick_backend_page_only_lists_the_users_allowed_hosts(gateway_client):
    _upload(gateway_client, filename="lab-a.yml", content=b"esxi:\n  ip: 10.0.0.1\n")
    _upload(gateway_client, filename="lab-b.yml", content=b"esxi:\n  ip: 10.0.0.2\n")

    restricted = _restricted_client("restricted-user", "user", ["lab-a.yml"])
    resp = restricted.get("/pick-backend", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "lab-a.yml" in resp.text
    assert "lab-b.yml" not in resp.text


def test_pick_backend_page_shows_presence_only_for_a_host_that_has_it(gateway_client):
    _upload(gateway_client, filename="lab-a.yml", content=b"esxi:\n  ip: 10.0.0.1\n")
    _upload(gateway_client, filename="lab-b.yml", content=b"esxi:\n  ip: 10.0.0.2\n")
    backend_presence.record("lab-a.yml", "alice")
    backend_presence.record("lab-a.yml", "bob")

    non_admin_page = (
        _restricted_client("regular-user", "user", "ALL")
        .get("/pick-backend", headers={"Accept": "text/html"})
        .text
    )
    assert "2 active users" in non_admin_page
    assert "alice" not in non_admin_page  # only a count for a non-admin

    admin_page = gateway_client.get(
        "/pick-backend", headers={"Accept": "text/html"}
    ).text
    assert "2 active users: alice, bob" in admin_page
    assert "lab-b.yml" in admin_page
    lab_b_tile = admin_page.split("lab-b.yml", 1)[1].split("</form>", 1)[0]
    assert "active" not in lab_b_tile
