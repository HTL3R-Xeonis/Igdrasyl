from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
import requests
from src.settings import Settings

from app import fix_jobs
from app.application import app

_CHECK = "R1:gi0/0 <-> SW1:Ethernet0: link missing"
_URL = "/ai/suggest-fix"
_REPLY = {
    "valid": True,
    "warnings": ["Review device state first."],
    "commands": ["show interfaces gi0/0"],
    "explanation": "Inspect the local interface before changing it.",
}


@pytest.fixture
def generator(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = _REPLY
    post = Mock(return_value=response)
    monkeypatch.setattr(fix_jobs.requests, "post", post)
    monkeypatch.setattr(app.state, "fix_jobs", {}, raising=False)
    return post


def _start(client, **overrides):
    return client.post(_URL, json={"device": "R1", "check": _CHECK, **overrides})


def _wait(client, started):
    assert started.status_code == 202, started.text
    url = f"{_URL}/{started.json()['job_id']}"
    end = time.monotonic() + 3
    while time.monotonic() < end:
        response = client.get(url)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] != "running":
            return url, body
        time.sleep(0.01)
    pytest.fail("fix suggestion did not finish")


def test_verify_identifies_devices_for_failed_checks(client, app_state, monkeypatch):
    backend = Mock()
    backend.verify.return_value = (
        [],
        [
            (False, _CHECK),
            (False, "ESXi VM 'VM1': not powered on"),
            (False, "Trunk NIC wiring: missing"),
        ],
    )
    monkeypatch.setattr("app.maintenance.get_or_create_backend", lambda app: backend)
    result = client.post("/maintenance/verify").json()
    assert result["results"][0]["devices"] == ["R1", "SW1"]
    assert result["results"][1]["devices"] == ["VM1"]
    assert "devices" not in result["results"][2]


def test_fix_suggestion_returns_draft_without_saving_or_pushing(
    client, app_state, generator, monkeypatch
):
    previous = {"R1": ["show version"]}
    monkeypatch.setattr(app_state, "last_config_push", previous, raising=False)
    _, result = _wait(client, _start(client, goal=" Keep the VLAN plan "))
    assert result["device"] == "R1"
    assert result["commands"] == _REPLY["commands"]
    assert result["explanation"] == _REPLY["explanation"]
    assert result["warnings"] == _REPLY["warnings"]
    assert app_state.last_config_push is previous
    assert client.get("/config-push/audit").json() == []
    args, kwargs = generator.call_args
    assert args == (
        Settings.API.TOPOLOGY_GENERATOR_URL.rstrip("/") + "/api/suggest-fix",
    )
    assert kwargs["timeout"] == Settings.API.TOPOLOGY_GENERATOR_TIMEOUT_SECONDS
    assert kwargs["allow_redirects"] is False
    assert set(kwargs["json"]) == {"device_role", "device_image", "context", "goal"}
    assert kwargs["json"]["device_role"] == "ROUTER"
    assert kwargs["json"]["device_image"] == "Cisco IOSv 15.6(1)T"
    assert _CHECK in kwargs["json"]["context"]
    assert "gi0/0 to SW1:Ethernet0" in kwargs["json"]["context"]
    assert kwargs["json"]["goal"] == "Keep the VLAN plan"


@pytest.mark.parametrize(
    "invalid",
    [
        {"valid": True, "commands": "show interfaces"},
        {
            "valid": True,
            "commands": ["show version\nshow interfaces"],
            "explanation": "bad",
        },
        {"valid": True, "commands": ["@reboot"], "explanation": "bad"},
        {"valid": False, "commands": ["show version"], "warnings": ["ambiguous"]},
    ],
)
def test_invalid_reply_retries_and_never_exposes_its_commands(
    client, generator, monkeypatch, invalid
):
    monkeypatch.setattr(Settings, "GENERATE_MAX_RETRIES", 1)
    generator.return_value.json.side_effect = [invalid, _REPLY]
    _, result = _wait(client, _start(client))
    assert result["attempt"] == result["max_attempts"] == 2
    assert result["commands"] == _REPLY["commands"]


def test_malformed_responses_exhaust_budget_without_commands(
    client, generator, monkeypatch
):
    monkeypatch.setattr(Settings, "GENERATE_MAX_RETRIES", 1)
    generator.return_value.json.side_effect = ValueError("bad json")
    _, result = _wait(client, _start(client))
    assert result["status"] == "error"
    assert result["attempt"] == 2
    assert "commands" not in result


def test_infrastructure_advice_without_commands_is_still_displayable(client, generator):
    generator.return_value.json.return_value = {
        "valid": False,
        "explanation": "Restore the link in the topology editor.",
    }
    _, result = _wait(client, _start(client))
    assert result["status"] == "done"
    assert result["commands"] == []
    assert "topology editor" in result["explanation"]
    generator.assert_called_once()


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (requests.Timeout, "timed out"),
        (requests.ConnectionError, "request to the Topology Generator failed"),
    ],
)
def test_transport_failure_is_not_retried(client, generator, failure, message):
    generator.side_effect = failure("private diagnostic")
    _, result = _wait(client, _start(client))
    assert result["status"] == "error"
    assert message in result["error"]
    assert "private diagnostic" not in result["error"]
    generator.assert_called_once()


def test_older_generator_reports_feature_unavailable(client, generator):
    generator.return_value.status_code = 404
    _, result = _wait(client, _start(client))
    assert result["status"] == "error"
    assert "does not support fix suggestions" in result["error"]
    generator.assert_called_once()


def test_rejects_unknown_devices_and_project_wide_checks(client, generator):
    assert _start(client, device="unknown").status_code == 404
    assert _start(client, device="VM1").status_code == 400
    assert _start(client, check="Trunk NIC wiring: missing").status_code == 400
    assert client.get(f"{_URL}/unknown").status_code == 404
    generator.assert_not_called()


def test_a_suggestion_cannot_cross_to_a_different_live_project(
    client, app_state, generator
):
    url, result = _wait(client, _start(client))
    assert result["status"] == "done"
    app_state.project_id = "another-project"
    response = client.get(url)
    assert response.status_code == 409
    assert "commands" not in response.json()


def test_only_finished_jobs_are_pruned(app_state, generator, monkeypatch):
    monkeypatch.setattr(fix_jobs, "_MAX_FINISHED_JOBS", 3)
    monkeypatch.setattr(fix_jobs.threading, "Thread", Mock())
    target = fix_jobs.current_target(app, "R1")
    payload = target.context(_CHECK, "")
    finished = fix_jobs.start_job(app, target, payload)
    finished.status = "done"
    running = fix_jobs.start_job(app, target, payload)
    recent = fix_jobs.start_job(app, target, payload)
    recent.status = "error"
    latest = fix_jobs.start_job(app, target, payload)
    assert list(app.state.fix_jobs) == [running.id, recent.id, latest.id]
