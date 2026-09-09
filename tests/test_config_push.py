from unittest.mock import MagicMock, patch

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

    with patch.object(cp, "_guest_credentials_for_image", return_value=("u", "p")):
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

    with patch.object(cp, "_guest_credentials_for_image", return_value=("u", "p")):
        result = cp._push_via_guest_ops(node, ["ok", "bad", "never"], fake_esxi)

    assert result.ok is False
    assert len(result.commands) == 2


def test_push_via_guest_ops_no_credentials_found(graph):
    node = graph.nodes["VM1"]
    fake_esxi = MagicMock()
    fake_esxi.get_vm.return_value = MagicMock()

    with patch.object(cp, "_guest_credentials_for_image", return_value=None):
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
    with patch("app.config_push.list_live_nodes", return_value=live_nodes), patch.object(
        cp, "_guest_credentials_for_image", return_value=("u", "p")
    ):
        fake_vm = MagicMock()
        app_state.esxi_connection.get_vm.return_value = fake_vm
        process_manager = app_state.esxi_connection.content.guestOperationsManager.processManager
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
