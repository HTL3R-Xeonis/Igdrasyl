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


class _SuspendedVm:
    def __init__(self, resume_ok=True, ready_after=2):
        from pyVmomi import vim

        self.name = "WINPC"
        self.runtime = MagicMock(powerState="suspended")
        self.guest = MagicMock(
            toolsRunningStatus="guestToolsNotRunning", guestOperationsReady=False
        )
        self.polls = 0
        self.ready_after = ready_after
        self.resume_ok = resume_ok
        self.task = MagicMock()
        self.task.info.state = (
            vim.TaskInfo.State.success if resume_ok else vim.TaskInfo.State.error
        )

    def PowerOnVM_Task(self):
        self.runtime.powerState = "poweredOn"
        return self.task

    def tick(self):
        self.polls += 1
        if self.polls >= self.ready_after:
            self.guest.toolsRunningStatus = "guestToolsRunning"
            self.guest.guestOperationsReady = True


def _resume(vm):
    ticks = {"now": 0.0}

    def sleep(seconds):
        ticks["now"] += seconds
        vm.tick()

    cp._resume_suspended(vm, sleep=sleep, clock=lambda: ticks["now"])
    return ticks["now"]


def test_a_suspended_vm_is_resumed_and_waited_for():
    vm = _SuspendedVm()
    waited = _resume(vm)
    assert vm.runtime.powerState == "poweredOn"
    assert vm.guest.toolsRunningStatus == "guestToolsRunning"
    assert waited < cp._RESUME_WAIT_SECONDS


def test_a_failed_resume_does_not_stop_the_push():
    vm = _SuspendedVm(resume_ok=False)
    _resume(vm)  # logs, returns - the push then reports the real state


def test_a_vm_that_is_not_suspended_is_left_alone():
    vm = _SuspendedVm()
    vm.runtime.powerState = "poweredOn"
    vm.PowerOnVM_Task = MagicMock()
    _resume(vm)
    vm.PowerOnVM_Task.assert_not_called()


def test_an_unreachable_guest_is_explained_instead_of_dumping_the_fault():
    vm = MagicMock()
    vm.runtime.powerState = "poweredOn"
    vm.guest.toolsRunningStatus = "guestToolsNotRunning"
    text = cp._guest_not_reachable_text(
        vm, "(vim.fault.GuestOperationsUnavailable) {...}"
    )
    assert "VMware Tools is not running" in text and "open the VM's screen" in text
    assert "GuestOperationsUnavailable" in text

    vm.runtime.powerState = "poweredOff"
    assert "the VM is poweredOff" in cp._guest_not_reachable_text(vm, "x")


def test_the_first_failing_guest_command_gets_the_explanation():
    vm = MagicMock()
    vm.runtime.powerState = "poweredOn"
    vm.guest.toolsRunningStatus = "guestToolsNotRunning"
    esxi = MagicMock()
    esxi.get_vm.return_value = vm
    node = MagicMock(image="Rocky 9.2")
    node.name = "VM1"
    with (
        patch.object(cp, "guest_credentials_for_image", return_value=("u", "p")),
        patch.object(cp, "run_command_in_guest", return_value=(None, "fault dump")),
    ):
        result = cp._push_via_guest_ops(node, ["hostname"], esxi)
    assert result.not_ready and not result.ok
    assert "VMware Tools is not running" in result.commands[0].output


def test_a_starved_gns3_vm_is_named_as_the_reason_for_an_unreachable_console():
    app = MagicMock()
    app.state.gns3_vm_ip = "10.0.0.5"
    with patch.object(cp, "compute_load", return_value={"cpu": 100.0, "memory": 52.0}):
        text = cp._with_load_hint("couldn't connect: Login failed", app)
    assert "CPU is at 100%" in text and "more vCPUs" in text
    assert text.startswith("couldn't connect: Login failed")


def test_a_normally_loaded_or_unreadable_gns3_vm_leaves_the_error_alone():
    app = MagicMock()
    app.state.gns3_vm_ip = "10.0.0.5"
    for load in ({"cpu": 20.0, "memory": 10.0}, {"cpu": None, "memory": None}, None):
        with patch.object(cp, "compute_load", return_value=load):
            assert cp._with_load_hint("couldn't connect", app) == "couldn't connect"


def test_a_netmiko_read_timeout_while_connecting_is_not_ready_not_a_crash(graph):
    from netmiko.exceptions import ReadTimeout

    with patch(
        "netmiko.ConnectHandler", side_effect=ReadTimeout("Pattern not detected")
    ):
        result = cp._push_via_netmiko(
            graph.nodes["R1"], ["show version"], "127.0.0.1", 1
        )
    assert not result.ok and result.not_ready
    assert "still booting" in result.error


def test_one_device_that_raises_does_not_take_the_others_down(graph):
    app = MagicMock()
    app.state.graph = graph
    app.state.esxi_connection.get_vm.return_value = MagicMock()
    boom = {"VM1": RuntimeError("kaboom")}

    def fake_guest_ops(node, commands, esxi, progress=None):
        if node.name in boom:
            raise boom[node.name]
        return cp.DeviceResult(device=node.name, transport="guest_ops", ok=True)

    names = [n for n, node in graph.nodes.items() if node.env.name == "ON_ESXI"]
    assert "VM1" in names
    with (
        patch.object(cp, "_push_via_guest_ops", fake_guest_ops),
        patch.object(cp, "list_live_nodes", return_value=[]),
        patch.object(cp, "_esxi_transport", return_value="guest_ops"),
    ):
        results = cp.push_all({n: ["x"] for n in names}, app)
    by_name = {r.device: r for r in results}
    assert not by_name["VM1"].ok and "kaboom" in by_name["VM1"].error
    assert all(r.ok for n, r in by_name.items() if n != "VM1")
    assert len(results) == len(names)


def test_a_console_that_stops_answering_before_the_first_command_is_not_ready(graph):
    from netmiko.exceptions import ReadTimeout

    conn = MagicMock()
    conn.send_command_timing.side_effect = ReadTimeout("no answer")
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = cp._push_via_netmiko(
            graph.nodes["R1"], ["enable", "show x"], "127.0.0.1", 1
        )
    assert not result.ok and result.not_ready and result.executed == 0
    assert "still booting" in result.error
    conn.disconnect.assert_called_once()


def test_a_console_that_stops_answering_later_fails_that_command_and_is_not_repeated(
    graph,
):
    from netmiko.exceptions import ReadTimeout

    conn = MagicMock()
    conn.send_command_timing.side_effect = ["R1#", ReadTimeout("no answer")]
    with patch("netmiko.ConnectHandler", return_value=conn):
        result = cp._push_via_netmiko(
            graph.nodes["R1"], ["enable", "show x", "show y"], "127.0.0.1", 1
        )
    assert not result.ok and not result.not_ready and result.executed == 2
    assert result.commands[1].ok is False and "no answer" in result.commands[1].output
