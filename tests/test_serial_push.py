"""Config push over the serial console: transport choice, the serial path, long lists, jobs, limits."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from pyVmomi import vim

import app.config_push as cp
import app.main as main_module
from app import esxi_serial, project_library, push_jobs
from app import serial_agent as sa


def _vm(port=None, tools="guestToolsNotRunning", powered="poweredOn"):
    vm = MagicMock()
    vm.guest.toolsRunningStatus = tools
    vm.runtime.powerState = powered
    devices = []
    if port is not None:
        backing = vim.vm.device.VirtualSerialPort.URIBackingInfo()
        backing.serviceURI = f"tcp://:{port}"
        backing.direction = "server"
        device = vim.vm.device.VirtualSerialPort()
        device.backing = backing
        devices.append(device)
    vm.config.hardware.device = devices
    return vm


def test_serial_port_is_read_from_the_vms_devices():
    assert esxi_serial.serial_port_of(_vm(2003)) == 2003
    assert esxi_serial.serial_port_of(_vm(None)) is None


def test_plain_command_lists_and_objects_are_both_accepted():
    specs = cp.normalize(
        {
            "R1": ["a", "b"],
            "VM1": {"commands": ["c"], "via": "serial", "profile": "routeros"},
        }
    )
    assert specs["R1"].via == "auto" and specs["R1"].commands == ["a", "b"]
    assert specs["VM1"].via == "serial" and specs["VM1"].profile == "routeros"


def test_an_unknown_transport_is_rejected():
    with pytest.raises(ValueError):
        cp.normalize({"VM1": {"commands": [], "via": "carrier-pigeon"}})


@pytest.mark.parametrize(
    ("via", "tools", "port", "expected"),
    [
        ("auto", "guestToolsRunning", 2000, "guest_ops"),
        ("auto", "guestToolsNotRunning", 2000, "serial"),
        ("auto", "guestToolsNotRunning", None, "guest_ops"),
        ("serial", "guestToolsRunning", 2000, "serial"),
        ("guest_ops", "guestToolsNotRunning", 2000, "guest_ops"),
    ],
)
def test_transport_choice(via, tools, port, expected, graph):
    esxi = MagicMock()
    esxi.get_vm.return_value = _vm(port, tools)
    spec = cp.DeviceSpec(commands=["x"], via=via)
    assert cp._esxi_transport(graph.nodes["VM1"], spec, esxi) == expected


def _node(image):
    node = MagicMock()
    node.name = "A1"
    node.image = image
    return node


@pytest.mark.parametrize(
    ("image", "tools", "expected"),
    [
        ("MikroTik", "guestToolsNotRunning", "serial"),
        ("pfSense", "guestToolsNotRunning", "serial"),
        ("Rocky 9.2", "guestToolsNotRunning", "guest_ops"),  # Tools may just be booting
        ("Windows 10 Pro Education", "guestToolsNotRunning", "guest_ops"),
        ("MikroTik", "guestToolsRunning", "guest_ops"),
    ],
)
def test_an_appliance_without_a_serial_port_still_goes_over_serial(
    image, tools, expected
):
    esxi = MagicMock()
    esxi.get_vm.return_value = _vm(None, tools)
    spec = cp.DeviceSpec(commands=["x"], via="auto")
    assert cp._esxi_transport(_node(image), spec, esxi) == expected


def test_ensure_serial_enables_a_missing_console_and_leaves_an_existing_one_alone():
    app = MagicMock()
    app.state.esxi_connection.get_vm.return_value = _vm(None)
    with patch.object(cp.esxi_serial, "enable") as enable:
        assert cp._ensure_serial(_node("MikroTik"), app) is None
    enable.assert_called_once_with(app, "A1")

    app.state.esxi_connection.get_vm.return_value = _vm(2002)
    with patch.object(cp.esxi_serial, "enable") as enable:
        assert cp._ensure_serial(_node("MikroTik"), app) is None
    enable.assert_not_called()


def test_ensure_serial_reports_why_it_could_not_set_the_console_up():
    app = MagicMock()
    app.state.esxi_connection.get_vm.return_value = _vm(None)
    with patch.object(
        cp.esxi_serial,
        "enable",
        side_effect=esxi_serial.SerialSetupError("no free port"),
    ):
        error = cp._ensure_serial(_node("MikroTik"), app)
    assert "could not be set up automatically" in error and "no free port" in error


def _fake_agent_run(outcomes, login_error=None):
    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self):
            if login_error:
                raise login_error

    def run_commands(agent, commands, on_progress=None, results=None):
        for i, (command, code, output) in enumerate(outcomes, 1):
            results.append((command, code, output))
            if on_progress:
                on_progress(i, len(commands))
            if code != 0:
                break
        return results

    return FakeAgent, run_commands


def _push_serial(node, commands, outcomes, login_error=None, vm=None, spec=None):
    esxi = MagicMock()
    esxi.get_vm.return_value = vm if vm is not None else _vm(2001)
    spec = spec or cp.DeviceSpec(commands=commands, via="serial")
    agent_cls, run_commands = _fake_agent_run(outcomes, login_error)
    with (
        patch.object(cp.serial_agent, "SerialAgent", agent_cls),
        patch.object(cp.serial_agent, "run_commands", run_commands),
        patch.object(cp, "guest_credentials_for_image", return_value=("admin", "pw")),
    ):
        return cp._push_via_serial(node, spec, esxi)


def test_serial_push_reports_per_command_results(graph):
    result = _push_serial(
        graph.nodes["VM1"], ["a", "b"], [("a", 0, "one"), ("b", 0, "two")]
    )
    assert result.ok and result.transport == "serial"
    assert [(c.command, c.output, c.ok) for c in result.commands] == [
        ("a", "one", True),
        ("b", "two", True),
    ]
    assert result.executed == 2 and result.total_commands == 2


def test_serial_push_stops_at_the_first_failing_command(graph):
    result = _push_serial(
        graph.nodes["VM1"], ["a", "b", "c"], [("a", 0, ""), ("b", 1, "boom")]
    )
    assert not result.ok and result.executed == 2 and result.total_commands == 3
    assert result.not_ready is False


def test_serial_login_failure_is_not_retried(graph):
    result = _push_serial(
        graph.nodes["VM1"], ["a"], [], login_error=sa.LoginFailed("nope")
    )
    assert not result.ok and result.not_ready is False and "nope" in result.error


def test_a_console_that_is_not_ready_yet_is_retryable(graph):
    result = _push_serial(
        graph.nodes["VM1"], ["a"], [], login_error=sa.AgentTimeout("silent")
    )
    assert not result.ok and result.not_ready is True


def test_serial_push_without_a_serial_port_says_how_to_enable_it(graph):
    result = _push_serial(graph.nodes["VM1"], ["a"], [], vm=_vm(None))
    assert not result.ok and "serial/enable" in result.error


def test_a_powered_off_vm_is_not_ready(graph):
    result = _push_serial(
        graph.nodes["VM1"], ["a"], [], vm=_vm(2001, powered="poweredOff")
    )
    assert result.not_ready is True


def test_profiles_without_serial_support_are_refused(graph):
    node = MagicMock()
    node.name = "VM1"
    node.image = "Windows 10 Pro Education"
    result = _push_serial(node, ["a"], [])
    assert not result.ok and "no serial profile" in result.error


def test_explicit_credentials_win_over_the_catalog(graph):
    esxi = MagicMock()
    esxi.get_vm.return_value = _vm(2001)
    seen = {}
    agent_cls, run_commands = _fake_agent_run([("a", 0, "")])

    class Spy(agent_cls):
        def __init__(self, **kwargs):
            seen.update(kwargs)
            super().__init__(**kwargs)

    spec = cp.DeviceSpec(
        commands=["a"], via="serial", username="me", password="secret", profile="linux"
    )
    with (
        patch.object(cp.serial_agent, "SerialAgent", Spy),
        patch.object(cp.serial_agent, "run_commands", run_commands),
        patch.object(cp, "guest_credentials_for_image") as catalog,
    ):
        cp._push_via_serial(graph.nodes["VM1"], spec, esxi)
    catalog.assert_not_called()
    assert (seen["username"], seen["password"]) == ("me", "secret")
    assert seen["profile"].name == "linux"


def test_a_very_long_result_is_compacted_to_failures_and_tail():
    results = [
        cp.CommandResult(command=f"c{i}", output="o" * 5000, ok=(i != 40))
        for i in range(10_000)
    ]
    kept, truncated = cp._compact(results)
    assert truncated and len(kept) <= cp.MAX_RESULT_COMMANDS
    assert any(not c.ok for c in kept) and kept[-1].command == "c9999"
    assert all(len(c.output) <= 2000 for c in kept)
    short, flag = cp._compact(results[:10])
    assert not flag and len(short) == 10


def test_ten_thousand_commands_per_device_are_accepted_and_one_more_is_not(client):
    project = project_library.create("Big")
    url = f"/projects/{project['id']}/configs"
    ok = client.put(url, json={"devices": {"VM1": ["echo x"] * 10_000}})
    assert ok.status_code == 200
    too_many = client.put(url, json={"devices": {"VM1": ["echo x"] * 10_001}})
    assert too_many.status_code == 400
    assert "10,000" in too_many.json()["detail"]


def test_device_objects_are_stored_with_the_project(client):
    project = project_library.create("Spec")
    url = f"/projects/{project['id']}/configs"
    body = {
        "devices": {
            "VM1": {
                "commands": ["/ip address print"],
                "via": "serial",
                "profile": "routeros",
            }
        }
    }
    assert client.put(url, json=body).status_code == 200
    stored = client.get(url).json()["devices"]["VM1"]
    assert stored["via"] == "serial" and stored["profile"] == "routeros"
    bad = {"devices": {"VM1": {"commands": ["x"], "via": "nonsense"}}}
    assert client.put(url, json=bad).status_code in (400, 422)


def test_push_job_reports_progress_and_results(app_state):
    release = threading.Event()

    def slow_push(devices, app, on_progress=None):
        on_progress("R1", 1, 2)
        release.wait(5)
        on_progress("R1", 2, 2)
        return [cp.DeviceResult(device="R1", transport="netmiko", ok=True)]

    with patch.object(push_jobs, "push_all", slow_push):
        job = push_jobs.start_job(main_module.app, {"R1": ["a", "b"]}, wait_seconds=0)
        time.sleep(0.2)
        assert job.status == "running"
        assert job.progress()["done_commands"] == 1
        with pytest.raises(push_jobs.AlreadyRunning):
            push_jobs.start_job(main_module.app, {"R1": ["a"]}, wait_seconds=0)
        release.set()
        for _ in range(50):
            if job.status != "running":
                break
            time.sleep(0.05)
    assert job.status == "done" and job.results[0].ok
    assert job.progress()["done_commands"] == 2


def test_push_job_endpoints(client, app_state):
    fake = [
        cp.DeviceResult(
            device="R1", transport="netmiko", ok=True, executed=1, total_commands=1
        )
    ]
    with patch.object(push_jobs, "push_all", return_value=fake):
        started = client.post("/config-push/jobs", json={"devices": {"R1": ["x"]}})
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        for _ in range(50):
            body = client.get(f"/config-push/jobs/{job_id}").json()
            if body["status"] != "running":
                break
            time.sleep(0.05)
    assert body["status"] == "done" and body["results"][0]["device"] == "R1"
    assert client.get("/config-push/jobs/nope").status_code == 404


def test_a_crashing_push_job_ends_with_an_error_instead_of_hanging(app_state):
    with patch.object(push_jobs, "push_all", side_effect=OSError("disk full")):
        job = push_jobs.start_job(main_module.app, {"R1": ["x"]}, wait_seconds=0)
        for _ in range(50):
            if job.status != "running":
                break
            time.sleep(0.05)
    assert job.status == "error" and "disk full" in job.error


def test_serial_status_and_enable_endpoints(client, app_state):
    app_state.esxi_connection.get_vm.return_value = _vm(2002, "guestToolsRunning")
    body = client.get("/nodes/VM1/serial").json()
    assert body["enabled"] and body["port"] == 2002 and body["powered_on"]
    # already enabled: nothing changes
    again = client.post("/nodes/VM1/serial/enable").json()
    assert again["changed"] is False and again["port"] == 2002
    assert client.get("/nodes/R1/serial").status_code == 404


def test_a_push_job_waits_for_a_booting_device_by_default_and_says_so(app_state):
    calls = []

    def fake_waiting(devices, app, wait_seconds, on_progress=None, on_waiting=None):
        calls.append(wait_seconds)
        on_waiting(["SW-CORE"])
        waiting_seen.append(main_module.app.state.push_job.progress()["waiting_for"])
        return [cp.DeviceResult(device="SW-CORE", transport="netmiko", ok=True)]

    waiting_seen = []
    with patch.object(push_jobs, "push_all_waiting", fake_waiting):
        job = push_jobs.start_job(main_module.app, {"SW-CORE": ["a"]})
        for _ in range(50):
            if job.status != "running":
                break
            time.sleep(0.05)
    assert calls == [cp.DEFAULT_WAIT_SECONDS] and waiting_seen == [["SW-CORE"]]
    assert job.status == "done" and job.progress()["waiting_for"] == []


def test_the_wait_of_a_push_job_can_be_chosen_or_switched_off(client, app_state):
    with patch.object(push_jobs, "start_job") as start:
        start.return_value = MagicMock(id="j1", total_commands=1)
        client.post("/config-push/jobs", json={"devices": {"R1": ["a"]}})
        client.post(
            "/config-push/jobs", json={"devices": {"R1": ["a"]}, "wait_seconds": 0}
        )
        bad = client.post(
            "/config-push/jobs", json={"devices": {"R1": ["a"]}, "wait_seconds": -5}
        )
    assert [c.kwargs["wait_seconds"] for c in start.call_args_list] == [None, 0]
    assert bad.status_code == 422


def test_push_all_waiting_tells_who_it_is_waiting_for():
    seen = []
    rounds = iter(
        [
            [
                cp.DeviceResult(
                    device="A", transport="netmiko", ok=False, not_ready=True
                )
            ],
            [cp.DeviceResult(device="A", transport="netmiko", ok=True)],
        ]
    )
    with patch.object(
        cp, "push_all", lambda devices, app, on_progress=None: next(rounds)
    ):
        results = cp.push_all_waiting(
            {"A": ["x"]},
            None,
            wait_seconds=60,
            interval_seconds=1,
            sleep=lambda s: None,
            on_waiting=lambda names: seen.append(names),
        )
    assert results[0].ok and seen == [["A"], []]
