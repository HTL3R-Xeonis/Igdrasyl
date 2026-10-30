from unittest.mock import MagicMock, patch

import pytest

import app.config_push as cp


def test_netmiko_device_type_maps_cisco_ios_variants():
    assert cp._netmiko_device_type("Cisco IOSv 15.6(1)T") == "cisco_ios_telnet"
    assert cp._netmiko_device_type("Cisco IOSvL2 15.2.1") == "cisco_ios_telnet"
    assert cp._netmiko_device_type("VPCS") == "generic_telnet"


def test_ios_error_detection():
    assert cp._looks_like_ios_error("% Invalid input detected") is True
    assert cp._looks_like_ios_error("Building configuration...") is False


async def _fake_ios(fail_on, reader, writer):
    writer.write(b"\r\nR1#")
    await writer.drain()
    buf = b""
    try:
        while True:
            chunk = await reader.read(1)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
            buf += chunk
            if chunk in (b"\n", b"\r"):
                line = buf.strip().decode(errors="replace")
                buf = b""
                if fail_on is not None and line == fail_on:
                    writer.write(b"\r\n% Invalid input detected\r\nR1#")
                else:
                    writer.write(b"\r\nR1#")
                await writer.drain()
    except Exception:
        pass


def test_push_via_netmiko_stops_on_first_failure(graph, fake_tcp_server):
    handler = lambda r, w: _fake_ios("bad command", r, w)
    port = fake_tcp_server(handler)
    node = graph.nodes["R1"]

    result = cp._push_via_netmiko(
        node, ["show version", "bad command", "never runs"], "127.0.0.1", port
    )

    assert result.transport == "netmiko"
    assert result.ok is False
    assert [c.command for c in result.commands] == ["show version", "bad command"]
    assert result.commands[0].ok is True
    assert result.commands[1].ok is False


def test_push_via_netmiko_reports_unreachable_console(graph):
    node = graph.nodes["R1"]
    result = cp._push_via_netmiko(node, ["show version"], "127.0.0.1", 1)
    assert result.ok is False
    assert "couldn't connect" in result.error


def test_push_via_guest_ops_happy_path(graph):
    node = graph.nodes["VM1"]
    fake_vm = MagicMock()
    fake_esxi = MagicMock()
    fake_esxi.get_vm.return_value = fake_vm
    process_manager = fake_esxi.content.guestOperationsManager.processManager
    process_manager.StartProgramInGuest.return_value = 4242
    completed = MagicMock(endTime="2026-01-01", exitCode=0)
    process_manager.ListProcessesInGuest.return_value = [completed]

    with patch.object(cp, "guest_credentials_for_image", return_value=("u", "p")):
        result = cp._push_via_guest_ops(node, ["echo hi", "echo bye"], fake_esxi)

    assert result.ok is True
    assert len(result.commands) == 2
    assert process_manager.StartProgramInGuest.call_count == 2


def test_push_via_guest_ops_stops_on_first_failure(graph):
    node = graph.nodes["VM1"]
    fake_vm = MagicMock()
    fake_esxi = MagicMock()
    fake_esxi.get_vm.return_value = fake_vm
    process_manager = fake_esxi.content.guestOperationsManager.processManager
    process_manager.StartProgramInGuest.return_value = 1
    ok_proc = MagicMock(endTime="t", exitCode=0)
    fail_proc = MagicMock(endTime="t", exitCode=1)
    process_manager.ListProcessesInGuest.side_effect = [[ok_proc], [fail_proc]]

    with patch.object(cp, "guest_credentials_for_image", return_value=("u", "p")):
        result = cp._push_via_guest_ops(node, ["ok", "bad", "never"], fake_esxi)

    assert result.ok is False
    assert len(result.commands) == 2


def test_push_via_guest_ops_no_credentials_found(graph):
    node = graph.nodes["VM1"]
    fake_esxi = MagicMock()
    fake_esxi.get_vm.return_value = MagicMock()

    with patch.object(cp, "guest_credentials_for_image", return_value=None):
        result = cp._push_via_guest_ops(node, ["echo hi"], fake_esxi)

    assert result.ok is False
    assert "no guest credentials" in result.error


def test_push_all_dispatches_by_environment_and_reports_unknown_device(
    app_state, fake_tcp_server
):
    handler = lambda r, w: _fake_ios(None, r, w)
    port = fake_tcp_server(handler)
    app_state.project_id = "proj-1"

    live_nodes = [
        {"name": "R1", "console": port, "console_type": "telnet", "status": "started"}
    ]
    with (
        patch("app.config_push.list_live_nodes", return_value=live_nodes),
        patch.object(cp, "guest_credentials_for_image", return_value=("u", "p")),
    ):
        fake_vm = MagicMock()
        app_state.esxi_connection.get_vm.return_value = fake_vm
        process_manager = (
            app_state.esxi_connection.content.guestOperationsManager.processManager
        )
        process_manager.StartProgramInGuest.return_value = 1
        process_manager.ListProcessesInGuest.return_value = [
            MagicMock(endTime="t", exitCode=0)
        ]

        class FakeApp:
            state = app_state

        results = cp.push_all(
            {"R1": ["show version"], "VM1": ["echo hi"], "GHOST": ["x"]}, FakeApp()
        )

    by_device = {r.device: r for r in results}
    assert by_device["R1"].transport == "netmiko"
    assert by_device["R1"].ok is True
    assert by_device["VM1"].transport == "guest_ops"
    assert by_device["GHOST"].ok is False
    assert by_device["GHOST"].error == "not found in topology"


def _result(name, ok=False, not_ready=False):
    return cp.DeviceResult(
        device=name, transport="x", ok=ok, not_ready=not_ready, error=None
    )


def test_push_all_waiting_retries_only_devices_that_were_not_reachable():
    calls: list[list[str]] = []
    scripted = {
        1: [_result("R1", not_ready=True), _result("VM1", ok=True)],
        2: [_result("R1", not_ready=True)],
        3: [_result("R1", ok=True)],
    }

    def fake_push_all(devices, app, on_progress=None):
        calls.append(sorted(devices))
        return scripted[len(calls)]

    sleeps: list[int] = []
    with patch.object(cp, "push_all", side_effect=fake_push_all):
        results = cp.push_all_waiting(
            {"R1": ["a"], "VM1": ["b"]},
            MagicMock(),
            wait_seconds=600,
            interval_seconds=15,
            sleep=sleeps.append,
        )

    assert calls == [["R1", "VM1"], ["R1"], ["R1"]]
    assert sleeps == [15, 15]
    assert [(r.device, r.ok) for r in results] == [("R1", True), ("VM1", True)]


def test_push_all_waiting_never_repeats_a_device_that_ran_a_command():
    calls = []

    def fake_push_all(devices, app, on_progress=None):
        calls.append(sorted(devices))
        return [_result("R1", ok=False, not_ready=False)]

    with patch.object(cp, "push_all", side_effect=fake_push_all):
        results = cp.push_all_waiting({"R1": ["a"]}, MagicMock(), sleep=lambda s: None)
    assert len(calls) == 1 and not results[0].ok


def test_push_all_waiting_gives_up_after_the_wait_time():
    with (
        patch.object(
            cp, "push_all", return_value=[_result("R1", not_ready=True)]
        ) as push,
        patch.object(cp.time, "monotonic", side_effect=[0, 0, 20, 20, 400, 400]),
    ):
        results = cp.push_all_waiting(
            {"R1": ["a"]},
            MagicMock(),
            wait_seconds=30,
            interval_seconds=15,
            sleep=lambda s: None,
        )
    assert push.call_count >= 1
    assert results[0].not_ready is True


def test_guest_ops_first_command_that_cannot_start_is_not_ready(graph):
    node = graph.nodes["VM1"]
    fake_esxi = MagicMock()
    with (
        patch.object(cp, "guest_credentials_for_image", return_value=("root", "pw")),
        patch.object(
            cp, "run_command_in_guest", return_value=(None, "tools not running")
        ),
    ):
        result = cp._push_via_guest_ops(node, ["a", "b"], fake_esxi)
    assert result.not_ready is True and not result.ok


def test_guest_ops_failure_after_a_command_ran_is_not_retryable(graph):
    node = graph.nodes["VM1"]
    fake_esxi = MagicMock()
    outputs = iter([(0, "ok"), (None, "lost the guest")])
    with (
        patch.object(cp, "guest_credentials_for_image", return_value=("root", "pw")),
        patch.object(
            cp, "run_command_in_guest", side_effect=lambda *a, **k: next(outputs)
        ),
    ):
        result = cp._push_via_guest_ops(node, ["a", "b"], fake_esxi)
    assert result.not_ready is False and not result.ok


class _SleepingVm:
    """A VM whose Tools come back after `wake_after` polls once a key was pressed."""

    def __init__(self, power="poweredOn", tools="guestToolsNotRunning", wake_after=2):
        self.name = "WINPC"
        self.runtime = MagicMock(powerState=power)
        self.guest = MagicMock(toolsRunningStatus=tools, guestOperationsReady=False)
        self.pressed = []
        self._polls_left = wake_after

    def PutUsbScanCodes(self, spec):
        self.pressed.append(spec.keyEvents[0].usbHidCode)

    def poll(self):
        if self.pressed:
            self._polls_left -= 1
            if self._polls_left <= 0:
                self.guest.toolsRunningStatus = "guestToolsRunning"
                self.guest.guestOperationsReady = True


def _wake(vm):
    ticks = {"now": 0.0}

    def sleep(seconds):
        ticks["now"] += seconds
        vm.poll()

    cp._wake_sleeping_windows(vm, sleep=sleep, clock=lambda: ticks["now"])
    return ticks["now"]


def test_a_sleeping_windows_vm_is_woken_with_a_key_and_waited_for():
    vm = _SleepingVm()
    waited = _wake(vm)
    assert vm.pressed == [(0xE1 << 16) | 7]
    assert vm.guest.toolsRunningStatus == "guestToolsRunning"
    assert waited < cp._WAKE_WAIT_SECONDS


def test_a_vm_that_does_not_wake_up_is_given_up_on_after_the_wait():
    vm = _SleepingVm(wake_after=10_000)
    assert _wake(vm) >= cp._WAKE_WAIT_SECONDS
    assert len(vm.pressed) == 1


@pytest.mark.parametrize(
    ("power", "tools"),
    [
        ("poweredOff", "guestToolsNotRunning"),
        ("poweredOn", "guestToolsRunning"),
        ("poweredOn", "guestToolsExecutingScripts"),
    ],
)
def test_no_key_is_pressed_unless_a_running_vm_has_lost_its_tools(power, tools):
    vm = _SleepingVm(power=power, tools=tools)
    _wake(vm)
    assert vm.pressed == []


def test_only_windows_vms_are_woken_before_a_guest_ops_push(graph):
    with patch.object(cp, "_wake_sleeping_windows") as wake:
        fake_esxi = MagicMock()
        process_manager = fake_esxi.content.guestOperationsManager.processManager
        process_manager.StartProgramInGuest.return_value = 1
        process_manager.ListProcessesInGuest.return_value = [
            MagicMock(endTime="t", exitCode=0)
        ]
        with patch.object(cp, "guest_credentials_for_image", return_value=("u", "p")):
            linux = MagicMock(image="Rocky 9.2")
            linux.name = "VM1"
            windows = MagicMock(image="Windows 10 Pro Education")
            windows.name = "VM1"
            cp._push_via_guest_ops(linux, ["a"], fake_esxi)
            wake.assert_not_called()
            cp._push_via_guest_ops(windows, ["a"], fake_esxi)
            wake.assert_called_once()
