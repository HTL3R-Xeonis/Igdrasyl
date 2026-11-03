"""The app presents itself as Ginnungagap (internal names stay TopologyOperator) and the landing page keeps its
no-tooltip rule."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = {"accept": "text/html,application/xhtml+xml"}


def _landing_markup() -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return html[html.index('<div id="landing-view">') : html.index("<dialog")]


def test_page_title_and_favicon_use_the_new_name():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<title>Ginnungagap</title>" in html
    assert 'rel="icon" type="image/svg+xml" href="/ginnungagap-mark.svg"' in html
    assert "<title>TopologyOperator" not in html


def test_logo_file_exists_and_is_served(client):
    assert (STATIC / "ginnungagap-mark.svg").is_file()
    response = client.get("/ginnungagap-mark.svg")
    assert response.status_code == 200
    assert "svg" in response.headers["content-type"]


def test_landing_header_shows_the_name_and_stays_minimal():
    markup = _landing_markup()
    assert "<h1>Ginnungagap</h1>" in markup
    assert "TopologyOperator" not in markup
    # only the name: no line under it, no tagline or section descriptions (removed on request)
    assert "landing-subtitle" not in markup
    assert "landing-section-hint" not in markup and "landing-tagline" not in markup


def test_landing_page_has_no_tooltips_or_hover_rules_of_its_own():
    # Rule from the user: no native tooltips on the landing page (a broken settings file's tile is the one exception,
    # and that is added by app.js at runtime).
    assert not re.search(r"\btitle=", _landing_markup())
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    branding = css[css.index("/* Ginnungagap landing page") :]
    assert ":hover" not in branding


def test_error_page_and_log_page_use_the_new_name(client):
    response = client.get("/no-such-page.html", headers=HTML)
    assert "Back to Ginnungagap" in response.text
    logs = (STATIC / "logs.html").read_text(encoding="utf-8")
    assert "<title>Ginnungagap - Logs</title>" in logs
    assert "Back to Ginnungagap" in logs


def test_api_title_is_the_new_name(client):
    assert client.get("/openapi.json").json()["info"]["title"] == "Ginnungagap"
