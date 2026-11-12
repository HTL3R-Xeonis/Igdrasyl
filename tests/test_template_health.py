"""
Tests app/template_health.py: what it reports for a healthy service, an
unreachable one, and the real outage this exists for (the service is up,
lists 0 templates and states why in its own /api/servers).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient
from src.settings import Settings

from app import template_health
from app.main import app

ESXI_URL = "http://templates.test:8000"
GNS3_URL = "http://templates.test:8001"


@pytest.fixture(autouse=True)
def _real_lookups(monkeypatch):
    # conftest turns literal values on for the whole suite - these tests
    # are about the real HTTP lookups.
    monkeypatch.setattr(Settings.API, "LITERAL_API_VALUES", False)
    monkeypatch.setattr(Settings.API, "ESXI_TEMPLATE_SERVER_URL", ESXI_URL)
    monkeypatch.setattr(Settings.API, "GNS3_TEMPLATE_SERVER_URL", GNS3_URL)
    monkeypatch.setattr(template_health, "_cache", None)


def _response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _fake_get(routes):
    """requests.get replacement: a payload, or an exception to raise, per URL."""

    def get(url, timeout):
        outcome = routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return _response(outcome)

    return get


HEALTHY = {
    f"{ESXI_URL}/api/templates": {"templates": [{"name": "Ubuntu-Server"}]},
    f"{GNS3_URL}/api/templates": {"templates": [{"name": "VPCS"}, {"name": "R"}]},
    f"{GNS3_URL}/api/servers": {"servers": [{"name": "gns3-server-2", "status": "ok"}]},
}


def test_a_healthy_service_reports_no_problems_and_its_counts():
    with patch("requests.get", _fake_get(HEALTHY)):
        result = template_health.check(force=True)

    assert result["ok"] is True
    assert result["problems"] == []
    assert result["esxi_templates"] == 1
    assert result["gns3_templates"] == 2


def test_an_unreachable_service_is_named_with_a_readable_reason():
    routes = {
        **HEALTHY,
        f"{GNS3_URL}/api/templates": requests.ConnectionError("Max retries exceeded"),
    }
    with patch("requests.get", _fake_get(routes)):
        result = template_health.check(force=True)

    assert result["ok"] is False
    assert result["gns3_templates"] is None
    assert result["problems"] == [
        (
            "The GNS3 template service (templates.test:8001) is not reachable: "
            "connection refused or host down."
        )
    ]


@pytest.mark.parametrize(
    ("error", "expected", "excluded"),
    [
        (
            "Cannot complete login due to an incorrect user name or password.",
            "Check the ESXi credentials",
            "Check whether ESXi is reachable",
        ),
        (
            "No ESXi password stored",
            "Check the ESXi credentials",
            "Check whether ESXi is reachable",
        ),
        (
            "Connection timed out",
            "Check whether ESXi is reachable",
            "Check the ESXi credentials",
        ),
    ],
)
def test_zero_templates_preserves_the_reason_and_guides_the_user(
    error, expected, excluded
):
    routes = {
        **HEALTHY,
        f"{GNS3_URL}/api/templates": {"templates": []},
        f"{GNS3_URL}/api/servers": {
            "servers": [
                {
                    "name": "gns3-server-2",
                    "status": "esxi_unreachable",
                    "error": error,
                }
            ]
        },
    }
    with (
        patch("requests.get", _fake_get(routes)),
        patch("app.gns3_vm_power.status", return_value=None),
    ):
        response = TestClient(app).get("/templates/health")

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert result["gns3_templates"] == 0 and result["refreshable"] is True
    joined = " | ".join(result["problems"])
    assert "lists 0 templates" in joined
    assert f"'gns3-server-2': esxi_unreachable - {error}" in joined
    assert expected in joined
    assert excluded not in joined


def test_a_service_without_a_servers_endpoint_still_gets_its_count_checked():
    routes = {
        **HEALTHY,
        f"{GNS3_URL}/api/templates": {"templates": []},
        f"{GNS3_URL}/api/servers": requests.HTTPError("404"),
    }
    with patch("requests.get", _fake_get(routes)):
        result = template_health.check(force=True)

    assert result["problems"] == [
        "The GNS3 template service (templates.test:8001) lists 0 templates."
    ]


def test_literal_values_mean_the_services_are_not_asked_at_all(monkeypatch):
    monkeypatch.setattr(Settings.API, "LITERAL_API_VALUES", True)

    with patch("requests.get", side_effect=AssertionError("must not be called")):
        result = template_health.check(force=True)

    assert result["ok"] is True and result["note"]


def test_the_result_is_cached_briefly_unless_forced():
    calls = []

    def get(url, timeout):
        calls.append(url)
        return _response(HEALTHY.get(url, {"templates": []}))

    with patch("requests.get", get):
        template_health.check(force=True)
        first = len(calls)
        template_health.check()
        assert len(calls) == first
        template_health.check(force=True)

    assert len(calls) == 2 * first


def test_an_image_not_found_error_gets_the_services_problem_attached():
    routes = {**HEALTHY, f"{GNS3_URL}/api/templates": {"templates": []}}
    with patch("requests.get", _fake_get(routes)):
        message = template_health.explain("Image VPCS not found on ESXi or GNS3")

    assert message.startswith("Image VPCS not found on ESXi or GNS3 - possible cause: ")
    assert "lists 0 templates" in message


def test_other_errors_and_a_healthy_service_leave_the_message_alone():
    with patch("requests.get", _fake_get(HEALTHY)):
        assert template_health.explain("something else broke") == "something else broke"
        unchanged = "Image X not found on ESXi or GNS3"
        assert template_health.explain(unchanged) == unchanged


def test_explaining_never_raises_even_if_the_check_itself_fails():
    with patch.object(template_health, "check", side_effect=RuntimeError("boom")):
        message = "Image X not found on ESXi or GNS3"
        assert template_health.explain(message) == message


def test_an_unreachable_service_is_described_by_the_services_state_not_the_raw_error():
    down = requests.ConnectionError(
        "HTTPConnectionPool(host='x', port=9): Max retries exceeded with url: /api/templates"
    )
    routes = {
        f"{ESXI_URL}/api/templates": down,
        f"{GNS3_URL}/api/templates": down,
    }
    with patch("requests.get", _fake_get(routes)):
        text = template_health.describe(down)

    assert "ESXi template service" in text and "is not reachable" in text
    assert "Max retries" not in text


def test_describe_keeps_other_errors_and_survives_a_failing_check():
    assert template_health.describe(ValueError("nope")) == "nope"
    with patch.object(template_health, "check", side_effect=RuntimeError("boom")):
        assert template_health.describe(TimeoutError("slow")) == "slow"


def _health(ok, refreshable=True):
    return {
        "ok": ok,
        "problems": [] if ok else ["GNS3 template service lists 0 templates."],
        "refreshable": refreshable,
    }


def test_heal_asks_for_a_refresh_and_reports_the_recovery():
    answers = iter([_health(False), _health(False), _health(False), _health(True)])
    with (
        patch.object(
            template_health, "check", side_effect=lambda force=False: next(answers)
        ),
        patch.object(template_health, "request_refresh") as refresh,
    ):
        assert template_health.heal(
            wait_seconds=60, interval_seconds=5, sleep=lambda s: None
        )
    refresh.assert_called_once()


def test_heal_does_nothing_when_the_services_were_healthy_all_along():
    # then the missing image really is missing - repeating the lookup would not help
    with (
        patch.object(template_health, "check", return_value=_health(True)),
        patch.object(template_health, "request_refresh") as refresh,
    ):
        assert template_health.heal(sleep=lambda s: None) is False
    refresh.assert_not_called()


def test_heal_does_not_wait_for_a_service_that_cannot_be_asked():
    with (
        patch.object(
            template_health, "check", return_value=_health(False, refreshable=False)
        ),
        patch.object(template_health, "request_refresh") as refresh,
    ):
        assert template_health.heal(sleep=lambda s: None) is False
    refresh.assert_not_called()


def test_heal_gives_up_after_the_wait_time_and_never_raises():
    sleeps: list[float] = []
    with (
        patch.object(template_health, "check", return_value=_health(False)),
        patch.object(template_health, "request_refresh"),
    ):
        assert (
            template_health.heal(
                wait_seconds=20, interval_seconds=5, sleep=sleeps.append
            )
            is False
        )
    assert sleeps == [5, 5, 5, 5]
    with patch.object(template_health, "check", side_effect=RuntimeError("boom")):
        assert template_health.heal(sleep=lambda s: None) is False
