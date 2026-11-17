"""The application version: one source (app/version.py), shown in /health, the landing footer and the picker."""

import re
import tomllib
from pathlib import Path

from app.version import __version__

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_the_version_is_major_minor_patch():
    assert SEMVER.match(__version__)


def test_the_version_file_is_a_plain_assignment_setuptools_can_read_statically():
    lines = [
        line
        for line in (ROOT / "app" / "version.py").read_text().splitlines()
        if line.strip()
    ]
    assert lines == [f'__version__ = "{__version__}"']


def test_pyproject_takes_the_version_from_app_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" in project["project"]["dynamic"]
    assert "version" not in project["project"]
    assert (
        project["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        == "app.version.__version__"
    )


def test_backend_health_carries_the_version(client, app_state):
    version = client.get("/health").json()["version"]
    assert version == __version__ and SEMVER.match(version)


def test_gateway_health_carries_the_version(gateway_client):
    body = gateway_client.get("/gateway/health").json()
    assert body["status"] == "ok" and body["version"] == __version__


def test_landing_footer_has_a_plain_version_element_next_to_the_links():
    html = (ROOT / "app" / "static" / "index.html").read_text()
    footer = html[html.index('id="landing-footer-links"') :]
    footer = footer[: footer.index("</div>")]
    assert 'id="landing-license-link"' in footer
    assert '<span id="landing-version"></span>' in footer
    assert "title=" not in footer


def test_landing_js_formats_the_version_with_a_leading_v():
    js = (ROOT / "app" / "static" / "import-and-generate.js").read_text()
    assert 'fetch("/health"' in js and "`v${version}`" in js


def test_landing_version_has_no_hover_effect():
    css = (ROOT / "app" / "static" / "styles.css").read_text()
    assert "#landing-version:hover" not in css


def test_picker_page_shows_the_version(gateway_client):
    page = gateway_client.get("/pick-backend").text
    assert re.search(
        r'class="backend-picker-version">v' + re.escape(__version__) + "<", page
    )
