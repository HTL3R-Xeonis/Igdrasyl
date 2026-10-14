"""
Tests the log viewer's endpoints (/logs/entries, /logs/download, and the
in-app /logs panel across a rotation), /templates/health, and that the
new pieces are really wired into the app and the landing page.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from src.settings import Settings

from app import log_store
from app.main import app
from app.request_logging import RequestLoggingMiddleware

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "app.log"
    monkeypatch.setattr(Settings, "LOG_FILE_PATH", str(path))
    return path


def _write_sample(path):
    path.write_text(
        "2026-10-14 09:00:00.000 | DEBUG    | a.py:poll:1 - polling nodes\n"
        "2026-10-14 09:00:01.000 | INFO     | a.py:deploy:2 - deployed lab\n"
        "2026-10-14 09:00:02.000 | ERROR    | a.py:login:3 - failed for /srv/private/x.yaml\n"
        "Traceback (most recent call last):\n"
        "ValueError: x\n"
    )


def test_entries_are_returned_with_the_log_info(client, log_path):
    _write_sample(log_path)

    body = client.get("/logs/entries").json()

    assert [e["level"] for e in body["entries"]] == ["DEBUG", "INFO", "ERROR"]
    assert body["info"]["max_lines"] == 100_000
    assert body["info"]["lines"] == 5


def test_a_traceback_stays_one_entry_in_the_response(client, log_path):
    _write_sample(log_path)

    error = client.get("/logs/entries?level=ERROR").json()["entries"]

    assert len(error) == 1
    assert "Traceback" in error[0]["message"]


def test_the_level_search_and_count_parameters_work(client, log_path):
    _write_sample(log_path)

    assert len(client.get("/logs/entries?level=INFO").json()["entries"]) == 2
    assert len(client.get("/logs/entries?search=deployed").json()["entries"]) == 1
    assert len(client.get("/logs/entries?lines=1").json()["entries"]) == 1


def test_an_absurd_line_count_is_clamped_instead_of_failing(client, log_path):
    _write_sample(log_path)

    assert client.get("/logs/entries?lines=0").status_code == 200
    assert client.get("/logs/entries?lines=99999999").status_code == 200


def test_server_paths_are_redacted_in_the_entries(client, log_path):
    _write_sample(log_path)

    text = client.get("/logs/entries").text

    assert "/srv/private" not in text
    assert "[server file]" in text


def test_the_download_is_plain_text_redacted_and_named(client, log_path):
    _write_sample(log_path)

    response = client.get("/logs/download")

    assert response.headers["content-type"].startswith("text/plain")
    assert 'filename="topologyoperator.log"' in response.headers["content-disposition"]
    assert "deployed lab" in response.text
    assert "/srv/private" not in response.text


def test_the_download_spans_both_files_after_a_rotation(client, log_path):
    sink = log_store.LineLimitedFile(log_path, lines_per_file=3)
    for number in range(1, 6):
        sink.write(
            f"2026-10-14 09:00:0{number}.000 | INFO     | a.py:f:1 - line {number}\n"
        )

    text = client.get("/logs/download").text

    assert [line.split(" - ")[1] for line in text.splitlines()] == [
        f"line {n}" for n in range(1, 6)
    ]


def test_the_in_app_logs_panel_still_has_history_right_after_a_rotation(
    client, log_path
):
    sink = log_store.LineLimitedFile(log_path, lines_per_file=3)
    for number in range(1, 5):  # the 4th line starts a fresh file
        sink.write(
            f"2026-10-14 09:00:0{number}.000 | INFO     | a.py:f:1 - line {number}\n"
        )

    panel = client.get("/logs?lines=300").json()

    assert [e["message"] for e in panel] == ["line 1", "line 2", "line 3", "line 4"]


def test_template_health_is_served_and_redacted(client):
    healthy = {"ok": True, "problems": [], "esxi_templates": 1, "gns3_templates": 2}
    with patch("app.main.template_health.check", return_value=healthy):
        body = client.get("/templates/health").json()

    assert body["ok"] is True and body["gns3_templates"] == 2


def test_an_image_error_from_adding_a_design_node_names_the_template_problem(client):
    problem = {
        "ok": False,
        "problems": ["The GNS3 template service (x:8001) lists 0 templates."],
    }
    with (
        patch("app.template_health.check", return_value=problem),
        patch(
            "app.main.design.add_node",
            side_effect=ValueError("Image VPCS not found on ESXi or GNS3"),
        ),
    ):
        response = client.post(
            "/design/nodes", json={"name": "A", "role": "PC", "image": "VPCS"}
        )

    assert response.status_code == 400
    assert "possible cause: The GNS3 template service" in response.json()["detail"]


def test_every_request_goes_through_the_logging_middleware():
    assert any(m.cls is RequestLoggingMiddleware for m in app.user_middleware)


def test_the_landing_page_links_to_the_log_page_next_to_the_license():
    html = (STATIC / "index.html").read_text()

    assert 'id="landing-license-link" href="/license"' in html
    assert 'id="landing-logs-link" href="/logs.html"' in html
    assert html.index("landing-license-link") < html.index("landing-logs-link")


def test_the_log_page_is_served_and_talks_to_the_new_endpoints(client):
    response = client.get("/logs.html")

    assert response.status_code == 200
    assert "/logs/entries" in response.text and "/logs/download" in response.text
    # entries are rendered via textContent, never as HTML - log lines can
    # contain user-influenced text
    assert "innerHTML" not in response.text


def test_the_template_banner_exists_on_every_screen():
    html = (STATIC / "index.html").read_text()

    assert html.index('id="template-banner"') < html.index('id="app-view"')


def test_delete_logs_clears_everything_and_notes_it(client, tmp_path, monkeypatch):
    from app import logging_setup

    path = tmp_path / "app.log"
    path.write_text("2026-01-01 00:00:00.000 | INFO     | x.py:f:1 - old entry\n")
    log_store.backup_path(path).write_text("older\n")
    monkeypatch.setattr(Settings, "LOG_FILE_PATH", str(path))
    monkeypatch.setattr(logging_setup, "_file_sink", None)

    response = client.delete("/logs")

    assert response.status_code == 200
    assert not log_store.backup_path(path).exists()
    assert "old entry" not in path.read_text()
