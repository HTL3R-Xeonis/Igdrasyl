from unittest.mock import Mock

import pytest

from app.routes import common
from app.routes.common import SETTINGS_ENV_VAR, has_active_host


@pytest.fixture
def without_active_host(monkeypatch, app_state):
    monkeypatch.setattr(common, "has_active_host", has_active_host)
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)


@pytest.mark.parametrize(
    ("method", "url", "kwargs", "work"),
    [
        (
            "POST",
            "/config-push",
            {"json": {"devices": {"R1": ["show version"]}}},
            "app.routes.push.push_all",
        ),
        (
            "POST",
            "/config-push/jobs",
            {"json": {"devices": {"R1": ["show version"]}}},
            "app.push_jobs.start_job",
        ),
        (
            "POST",
            "/config-snippets/ai/push",
            {"json": {"draft_id": "a" * 32}},
            "app.ai_snippets.push_draft",
        ),
        ("GET", "/nodes/R1/serial", {}, "app.esxi_serial.status"),
        ("POST", "/nodes/R1/serial/enable", {}, "app.esxi_serial.enable"),
        ("GET", "/project/export", {}, "app.project_bundle.build_export"),
        (
            "POST",
            "/project/import",
            {"files": {"file": ("test.topoproj", b"test")}},
            "app.import_jobs.start_job",
        ),
    ],
)
def test_host_specific_route_refuses_before_doing_work(
    client, without_active_host, monkeypatch, method, url, kwargs, work
):
    downstream = Mock()
    monkeypatch.setattr(work, downstream)

    response = client.request(method, url, **kwargs)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "No host is active - choose one on the landing page first."
    )
    downstream.assert_not_called()


def test_an_active_profile_allows_the_serial_status_request(
    client, without_active_host, monkeypatch
):
    monkeypatch.setenv(SETTINGS_ENV_VAR, "lab.yml")
    status = Mock(return_value={"enabled": False, "port": None})
    monkeypatch.setattr("app.esxi_serial.status", status)

    response = client.get("/nodes/R1/serial")

    assert response.status_code == 200
    assert response.json() == status.return_value
    status.assert_called_once()


def test_snippet_library_can_be_opened_without_a_host(client, without_active_host):
    assert client.get("/config-snippets").status_code == 200


def test_bundle_preview_can_be_opened_without_a_host(
    client, without_active_host, monkeypatch
):
    monkeypatch.setattr("app.import_jobs.peek_target_node_names", lambda _: {"R1"})

    response = client.post(
        "/project/import/preview", files={"file": ("test.topoproj", b"test")}
    )

    assert response.status_code == 200
    assert response.json()["bundle_nodes"] == ["R1"]
