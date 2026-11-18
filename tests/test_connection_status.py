"""
"Connection lost": what the gateway tells a person when the server of the selected host is not reachable - the layer
status endpoint, the readable page for browser navigations, the backend state from systemd and the `next` target of the
login page (a session that ended during an outage returns to where the person was).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import backend_manager, error_pages, routes_gateway
from app.proxy import make_proxy_app
from app.routes import auth as auth_routes

HTML = {"Accept": "text/html,application/xhtml+xml"}


# --- backend_manager.state -------------------------------------------------


def test_the_backend_state_names_what_systemd_says():
    def inspect_with(answer):
        return lambda args: answer

    assert backend_manager.state("lab.yml", inspect=inspect_with("active")) == "running"
    assert (
        backend_manager.state("lab.yml", inspect=inspect_with("activating"))
        == "starting"
    )
    assert (
        backend_manager.state("lab.yml", inspect=inspect_with("reloading"))
        == "starting"
    )
    for answer in ("inactive", "failed", "deactivating", ""):
        assert (
            backend_manager.state("lab.yml", inspect=inspect_with(answer)) == "stopped"
        )


def test_the_state_asks_for_the_unit_of_that_profile():
    asked = []
    backend_manager.state(
        "host-a.yml", inspect=lambda args: asked.append(args) or "active"
    )
    assert asked == [
        ["systemctl", "--user", "is-active", "xeonis-backend@host-a.service"]
    ]


# --- GET /gateway/status ----------------------------------------------------


def test_status_without_a_selection_says_none(gateway_client):
    response = gateway_client.get("/gateway/status")
    assert response.status_code == 200
    assert response.json() == {"selected": None, "backend": "none", "host": None}
    assert response.headers["cache-control"] == "no-store"


def _stored_admin():
    from app import users

    users.create_user("test-admin", "a-long-enough-pass", "admin")


def test_status_reports_the_selected_servers_state(gateway_client, monkeypatch):
    _stored_admin()
    gateway_client.cookies.set(routes_gateway.BACKEND_COOKIE, "lab.yml")
    for state in ("running", "starting", "stopped"):
        monkeypatch.setattr(backend_manager, "state", lambda filename, s=state: s)
        body = gateway_client.get("/gateway/status").json()
        assert body["selected"] == "lab.yml"
        assert body["backend"] == state


def test_status_includes_the_hosts_address_from_the_profile(
    gateway_client, monkeypatch
):
    _stored_admin()
    gateway_client.post(
        "/settings-profiles",
        files={"file": ("lab.yml", b"esxi:\n  ip: 10.0.0.9\n", "text/yaml")},
    )
    gateway_client.cookies.set(routes_gateway.BACKEND_COOKIE, "lab.yml")
    monkeypatch.setattr(backend_manager, "state", lambda filename: "running")
    assert gateway_client.get("/gateway/status").json()["host"] == "10.0.0.9"


def test_status_needs_a_session_and_is_not_proxied(gateway_anonymous_client):
    anonymous = gateway_anonymous_client.get("/gateway/status")
    assert anonymous.status_code == 401
    page = gateway_anonymous_client.get(
        "/gateway/status", headers=HTML, follow_redirects=False
    )
    assert page.status_code == 303 and page.headers["location"] == "/login"


def test_gateway_health_stays_open_for_the_monitors_first_check(
    gateway_anonymous_client,
):
    assert gateway_anonymous_client.get("/gateway/health").status_code == 200


def test_a_host_the_user_may_not_use_counts_as_not_selected(monkeypatch):
    from app import auth, gateway_main, users

    users.create_user(
        "restricted", "a-long-enough-pass", "user", allowed_hosts=["other.yml"]
    )
    restricted = TestClient(gateway_main.app)
    restricted.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("restricted", "user")
    )
    restricted.cookies.set(routes_gateway.BACKEND_COOKIE, "lab.yml")
    monkeypatch.setattr(backend_manager, "state", lambda filename: "running")
    assert restricted.get("/gateway/status").json() == {
        "selected": None,
        "backend": "none",
        "host": None,
    }


# --- the readable page -------------------------------------------------------


def test_the_unavailable_page_names_the_server_and_the_failing_layer():
    page = error_pages.unavailable_html("lab.yml", "unreachable")
    assert "The server for lab.yml is not reachable" in page
    assert 'http-equiv="refresh" content="10"' in page
    assert page.count('class="cl-layer cl-ok"') == 2  # browser and gateway were reached
    assert 'class="cl-layer cl-fail"' in page and 'class="cl-layer cl-unknown"' in page
    assert "not reachable</span>" in page  # the state is also written, not colour alone
    assert 'href="/pick-backend"' in page


def test_the_unavailable_page_says_starting_for_a_unit_that_is_coming_up():
    page = error_pages.unavailable_html("lab.yml", "starting")
    assert "The server for lab.yml is starting" in page


def test_the_unavailable_page_escapes_the_host_label():
    page = error_pages.unavailable_html("<script>alert(1)</script>.yml", "stopped")
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_the_unavailable_page_without_a_host_has_a_generic_title():
    assert (
        "The server for the selected host is not reachable"
        in error_pages.unavailable_html(None, "unreachable")
    )


# --- the proxy answers navigations with the page, API callers as before ----------


def _proxy_to_nothing(describe):
    app = FastAPI()
    app.mount(
        "/",
        make_proxy_app(lambda scope: ("127.0.0.1", 1), describe_unavailable=describe),
    )
    return TestClient(app)


def test_a_browser_navigation_during_an_outage_gets_the_card_not_the_exception():
    client = _proxy_to_nothing(lambda scope: ("lab.yml", "unreachable"))
    response = client.get("/anything", headers=HTML)
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "The server for lab.yml is not reachable" in response.text
    assert (
        "Upstream unreachable" not in response.text
        and "Connection refused" not in response.text
    )


def test_fetch_callers_keep_the_plain_502_text():
    client = _proxy_to_nothing(lambda scope: ("lab.yml", "unreachable"))
    response = client.get("/anything", headers={"Accept": "application/json"})
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text.startswith("Upstream unreachable")


def test_only_a_get_navigation_gets_the_card():
    client = _proxy_to_nothing(lambda scope: ("lab.yml", "unreachable"))
    response = client.post("/anything", headers=HTML, content=b"x")
    assert response.headers["content-type"].startswith("text/plain")


def test_without_a_describe_function_the_proxy_behaves_as_before():
    app = FastAPI()
    app.mount("/", make_proxy_app(lambda scope: ("127.0.0.1", 1)))
    response = TestClient(app).get("/anything", headers=HTML)
    assert response.status_code == 502 and response.text.startswith(
        "Upstream unreachable"
    )


def test_describe_unavailable_maps_the_unit_state_to_the_page_state(
    gateway_client, monkeypatch
):
    _stored_admin()
    scope = {"type": "http", "headers": [(b"cookie", b"xeonis_backend=lab.yml")]}
    monkeypatch.setattr(routes_gateway, "_caller_username", lambda s: "test-admin")
    for unit, shown in (
        ("starting", "starting"),
        ("stopped", "stopped"),
        ("running", "unreachable"),
    ):
        monkeypatch.setattr(backend_manager, "state", lambda filename, u=unit: u)
        assert routes_gateway.describe_unavailable(scope) == ("lab.yml", shown)


# --- the login page returns to where the person was ----------------------------


def test_next_accepts_only_a_same_origin_path():
    ok = auth_routes.safe_next
    assert ok("/") == "/" and ok("/projects?id=3") == "/projects?id=3"
    for bad in (
        None,
        "",
        "relative",
        "//evil.example/x",
        "http://evil.example",
        "https://evil.example",
        "/\\evil.example",
        "/ok\nHeader: x",
        "/login",
        "/login?next=/",
        "/logout",
        "/" + "a" * 600,
    ):
        assert ok(bad) is None, bad


def test_the_login_page_carries_the_target_and_the_session_note(
    gateway_anonymous_client,
):
    page = gateway_anonymous_client.get("/login?next=/pick-backend&expired=1").text
    assert '<input type="hidden" name="next" value="/pick-backend">' in page
    assert "Your session ended - please sign in again." in page


def test_the_login_page_drops_a_foreign_target(gateway_anonymous_client):
    page = gateway_anonymous_client.get("/login?next=//evil.example").text
    assert 'name="next"' not in page


def test_a_successful_login_goes_to_the_target(gateway_anonymous_client):
    from app import users

    users.create_user("alice", "a-long-enough-pass", "user")
    response = gateway_anonymous_client.post(
        "/login",
        data={
            "username": "alice",
            "password": "a-long-enough-pass",
            "next": "/pick-backend",
        },
        follow_redirects=False,
    )
    assert (
        response.status_code == 303 and response.headers["location"] == "/pick-backend"
    )


def test_a_login_with_a_foreign_target_goes_home(gateway_anonymous_client):
    from app import users

    users.create_user("alice", "a-long-enough-pass", "user")
    response = gateway_anonymous_client.post(
        "/login",
        data={
            "username": "alice",
            "password": "a-long-enough-pass",
            "next": "//evil.example",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/?enter=1"


def test_a_failed_login_keeps_the_target(gateway_anonymous_client):
    response = gateway_anonymous_client.post(
        "/login",
        data={"username": "nobody", "password": "wrong", "next": "/pick-backend"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login?error=1&next=%2Fpick-backend"
