"""
app/backend_manager.py never touches a real systemd - every test here
passes its own fake Runner/Inspect, which is the whole point of that
module taking them as parameters.
"""

from __future__ import annotations

import pytest

from app import backend_manager, backend_ports


@pytest.fixture(autouse=True)
def _isolated_ports_file(tmp_path, monkeypatch):
    monkeypatch.setenv(backend_ports._PORTS_FILE_ENV_VAR, str(tmp_path / "ports.json"))
    yield


def test_unit_name_strips_the_yaml_suffix_and_keeps_the_rest():
    assert backend_manager.unit_name("lab-a.yml") == "xeonis-backend@lab-a.service"
    assert backend_manager.unit_name("lab-b.yaml") == "xeonis-backend@lab-b.service"


def test_is_active_reflects_whatever_the_inspect_callable_reports():
    assert backend_manager.is_active("lab-a.yml", inspect=lambda args: "active")
    assert not backend_manager.is_active("lab-a.yml", inspect=lambda args: "inactive")


def test_ensure_running_starts_the_unit_only_when_not_already_active():
    calls: list[list[str]] = []

    def inspect(args):
        calls.append(("inspect", args))
        return "inactive"

    def runner(args):
        calls.append(("run", args))
        return 0

    port = backend_manager.ensure_running("lab-a.yml", runner=runner, inspect=inspect)

    assert port == backend_ports.get_port("lab-a.yml")
    started = [
        c
        for c in calls
        if c[0] == "run" and c[1][:3] == ["systemctl", "--user", "start"]
    ]
    assert len(started) == 1
    assert started[0][1][-1] == "xeonis-backend@lab-a.service"


def test_ensure_running_does_not_start_an_already_active_unit():
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        return 0

    backend_manager.ensure_running(
        "lab-a.yml", runner=runner, inspect=lambda args: "active"
    )

    assert calls == []


def test_ensure_running_keeps_the_same_port_across_calls():
    def inspect(args):
        return "active"

    first = backend_manager.ensure_running(
        "lab-a.yml", inspect=inspect, runner=lambda a: 0
    )
    second = backend_manager.ensure_running(
        "lab-a.yml", inspect=inspect, runner=lambda a: 0
    )
    assert first == second


def test_restart_and_stop_target_the_right_unit():
    calls: list[list[str]] = []

    backend_manager.restart("lab-a.yml", runner=lambda args: calls.append(args))
    backend_manager.stop("lab-a.yml", runner=lambda args: calls.append(args))

    assert calls[0] == [
        "systemctl",
        "--user",
        "restart",
        "xeonis-backend@lab-a.service",
    ]
    assert calls[1] == ["systemctl", "--user", "stop", "xeonis-backend@lab-a.service"]


def test_wait_healthy_true_once_health_answers_200():
    class FakeResponse:
        status_code = 200

    assert backend_manager.wait_healthy(
        9200, timeout_seconds=1, http_get=lambda url, timeout: FakeResponse()
    )


def test_wait_healthy_false_when_it_only_ever_errors():
    import requests

    def always_fails(url, timeout):
        raise requests.ConnectionError("refused")

    assert not backend_manager.wait_healthy(
        9200, timeout_seconds=0.2, http_get=always_fails
    )


def test_wait_healthy_false_on_a_non_200_status():
    class FakeResponse:
        status_code = 503

    assert not backend_manager.wait_healthy(
        9200, timeout_seconds=0.2, http_get=lambda url, timeout: FakeResponse()
    )
