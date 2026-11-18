"""After login and server choice the chosen server's canvas opens directly (?enter=1); failures stay on the picker."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import backend_manager, routes_gateway, users

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = {"Accept": "text/html"}
URL_ENV = (
    "ESXI_TEMPLATE_SERVER_URL",
    "GNS3_TEMPLATE_SERVER_URL",
    "TOPOLOGY_GENERATOR_URL",
    "OVA_DEPLOY_URL",
)


def _upload(client, filename="host-a.yml", content=b"esxi:\n  ip: 10.0.0.1\n"):
    return client.post(
        "/settings-profiles",
        files={"file": (filename, content, "application/x-yaml")},
    )


def _healthy_backend(monkeypatch):
    monkeypatch.setattr(backend_manager, "ensure_running", lambda _filename: 9200)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda _port: True)


def _restricted(username, role, allowed_hosts):
    import app.gateway_main as gateway_module
    from app import auth

    users.create_user(username, "correct-horse", role, allowed_hosts)
    client = TestClient(gateway_module.app)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(username, role))
    return client


# --- the redirect after Activate and after login ---------------------------------


def test_activate_from_a_browser_form_enters_the_canvas(gateway_client, monkeypatch):
    _upload(gateway_client)
    _healthy_backend(monkeypatch)
    response = gateway_client.post(
        "/settings-profiles/host-a.yml/activate", headers=HTML, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?enter=1"
    assert gateway_client.cookies.get(routes_gateway.BACKEND_COOKIE) == "host-a.yml"


def test_activate_from_a_script_keeps_the_json_answer(gateway_client, monkeypatch):
    _upload(gateway_client)
    _healthy_backend(monkeypatch)
    response = gateway_client.post(
        "/settings-profiles/host-a.yml/activate",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.json() == {"status": "active"}


# --- failures: the browser goes back to the picker with a message, scripts keep their status ------


@pytest.mark.parametrize(
    ("path", "code"),
    [("/settings-profiles/nope.yml/activate", "not_found")],
)
def test_an_unknown_host_goes_back_to_the_picker(gateway_client, path, code):
    response = gateway_client.post(path, headers=HTML, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/pick-backend?error={code}&host=nope.yml"
    assert (
        gateway_client.post(path, headers={"Accept": "application/json"}).status_code
        == 404
    )


def test_a_host_outside_the_allowed_list_goes_back_to_the_picker(
    gateway_client, monkeypatch
):
    _upload(gateway_client, filename="lab-a.yml")
    _healthy_backend(monkeypatch)
    restricted = _restricted("restricted-user", "user", ["lab-b.yml"])
    response = restricted.post(
        "/settings-profiles/lab-a.yml/activate", headers=HTML, follow_redirects=False
    )
    assert response.status_code == 303
    assert (
        response.headers["location"] == "/pick-backend?error=forbidden&host=lab-a.yml"
    )
    assert restricted.post("/settings-profiles/lab-a.yml/activate").status_code == 403


def test_a_backend_that_never_gets_healthy_goes_back_to_the_picker(
    gateway_client, monkeypatch
):
    _upload(gateway_client)
    monkeypatch.setattr(backend_manager, "ensure_running", lambda _filename: 9200)
    monkeypatch.setattr(backend_manager, "wait_healthy", lambda _port: False)
    response = gateway_client.post(
        "/settings-profiles/host-a.yml/activate", headers=HTML, follow_redirects=False
    )
    assert (
        response.headers["location"] == "/pick-backend?error=unhealthy&host=host-a.yml"
    )
    assert (
        gateway_client.post("/settings-profiles/host-a.yml/activate").status_code == 503
    )


def test_a_profile_without_service_addresses_goes_back_to_the_picker(
    gateway_client, monkeypatch
):
    for name in URL_ENV:
        monkeypatch.delenv(name, raising=False)
    _upload(gateway_client)
    response = gateway_client.post(
        "/settings-profiles/host-a.yml/activate", headers=HTML, follow_redirects=False
    )
    assert response.headers["location"].startswith(
        "/pick-backend?error=missing_settings"
    )


# --- the picker shows the message and the right empty text ---------------------------------------


def test_the_picker_shows_the_message_of_a_failed_activation(gateway_client):
    page = gateway_client.get("/pick-backend?error=forbidden&host=lab-a.yml").text
    assert 'class="backend-picker-error"' in page
    assert "You are not allowed to use lab-a.yml." in page


def test_the_picker_only_shows_known_messages_and_safe_host_names(gateway_client):
    page = gateway_client.get(
        "/pick-backend?error=<script>alert(1)</script>&host=x"
    ).text
    assert "backend-picker-error" not in page.split("<style>")[-1].split("</style>")[-1]
    page = gateway_client.get("/pick-backend?error=forbidden&host=<b>x</b>").text
    assert "<b>x</b>" not in page and "that host" in page


def test_the_empty_picker_says_who_can_fix_it(gateway_client):
    for profile in gateway_client.get("/settings-profiles").json():
        gateway_client.delete(f"/settings-profiles/{profile['filename']}")
    assert "an admin creates one" in gateway_client.get("/pick-backend").text
    plain = _restricted("plain-user", "user", [])
    text = plain.get("/pick-backend").text
    assert "No host is available for your account - ask an admin." in text
    assert "an admin creates one" not in text


# --- the page side (static files) ---------------------------------------------------------------


def _text(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_page_starts_through_startview_not_the_main_menu_directly():
    last = _text("consoles-and-push.js")
    assert re.search(r"^startView\(\);$", last, re.MULTILINE)
    assert not re.search(r"^showLandingView\(\);$", last, re.MULTILINE)
    start = _text("import-and-generate.js")
    assert "async function startView()" in start
    assert 'params.delete("enter")' in start and "history.replaceState" in start
    assert 'classList.remove("enter-pending")' in start


def test_the_pending_flag_hides_the_main_menu_before_the_first_paint():
    html = _text("index.html")
    assert 'classList.add("enter-pending")' in html
    assert html.index("enter-pending") < html.index("</head>")
    assert "html.enter-pending #landing-view { display: none !important; }" in _text(
        "styles.css"
    )


def test_the_gateway_only_entries_and_the_choose_project_button_exist():
    html = _text("index.html")
    for element_id in (
        "switch-server-btn",
        "landing-switch-server",
        "warning-choose-project-btn",
    ):
        assert re.search(rf'id="{element_id}"[^>]*hidden', html), element_id
    assert "chooseProject:" in _text("canvas-state.js")
    assert "warning-choose-project-btn" in _text("projects.js")


def test_the_gateway_mode_switch_does_not_use_the_restart_wording():
    js = _text("connection-settings-and-maintenance.js")
    gateway_branch = js[js.index("if (await isGatewayMode())") :]
    gateway_branch = gateway_branch[: gateway_branch.index("return;")]
    assert "restarts" not in gateway_branch
    assert 'window.location.assign("/?enter=1")' in js


def test_the_no_project_message_points_at_the_button():
    from app.topology import load_graph  # noqa: F401  (the message lives next to it)

    assert "Use Choose project… to select a topology." in Path(
        STATIC.parent / "topology.py"
    ).read_text(encoding="utf-8")
