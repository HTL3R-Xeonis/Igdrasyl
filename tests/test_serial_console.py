"""The browser's serial console of an ESXi VM: refused up front when it can't work, a raw pipe when it can."""

from unittest.mock import patch

import pytest
from src.settings import Settings
from starlette.websockets import WebSocketDisconnect

from app import esxi_serial, serial_console


async def _echo(reader, writer):
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass


def _state(**overrides):
    state = {
        "enabled": True,
        "port": 2000,
        "tools": "guestToolsRunning",
        "powered_on": True,
    }
    state.update(overrides)
    return state


@pytest.fixture(autouse=True)
def _esxi_host_is_local(monkeypatch):
    monkeypatch.setattr(Settings.ESXI, "IP", "127.0.0.1", raising=False)
    serial_console._open.clear()


def _closes_with(client, state=None, error=None, name="VM1"):
    with patch(
        "app.serial_console.esxi_serial.status", return_value=state, side_effect=error
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ws/serial/{name}") as ws:
                ws.receive_bytes()
    return exc_info.value


def test_echo_round_trip_through_the_serial_port(client, app_state, fake_tcp_server):
    port = fake_tcp_server(_echo)
    with (
        patch("app.serial_console.esxi_serial.status", return_value=_state(port=port)),
        client.websocket_connect("/ws/serial/VM1") as ws,
    ):
        ws.send_bytes(b"login: ")
        assert ws.receive_bytes() == b"login: "


def test_a_node_that_is_not_an_esxi_vm_closes_with_1008(client, app_state):
    closed = _closes_with(
        client,
        error=esxi_serial.SerialSetupError(
            "'R1' is not an ESXi VM node of this topology"
        ),
    )
    assert closed.code == 1008
    assert "not an ESXi VM" in closed.reason


def test_a_vm_without_a_serial_port_closes_with_1011_and_says_to_enable_it(
    client, app_state
):
    closed = _closes_with(client, state=_state(enabled=False, port=None))
    assert closed.code == 1011
    assert "enable it first" in closed.reason


def test_a_powered_off_vm_closes_with_1011(client, app_state):
    closed = _closes_with(client, state=_state(powered_on=False))
    assert closed.code == 1011
    assert "powered off" in closed.reason


def test_an_unreadable_vm_state_closes_with_1011(client, app_state):
    closed = _closes_with(client, error=RuntimeError("session expired"))
    assert closed.code == 1011
    assert (
        "session expired" not in closed.reason
    )  # the detail goes to the log, not to the browser


def test_an_unreachable_serial_port_closes_with_1011_and_names_the_likely_causes(
    client, app_state
):
    closed = _closes_with(client, state=_state(port=1))
    assert closed.code == 1011
    assert "in use" in closed.reason


def test_a_second_console_of_the_same_vm_is_refused_with_1013(client, app_state):
    serial_console._open.add("VM1")
    closed = _closes_with(client, state=_state())
    assert closed.code == 1013


def test_the_slot_is_released_when_the_console_closes(
    client, app_state, fake_tcp_server
):
    port = fake_tcp_server(_echo)
    with (
        patch("app.serial_console.esxi_serial.status", return_value=_state(port=port)),
        client.websocket_connect("/ws/serial/VM1") as ws,
    ):
        ws.send_bytes(b"x")
        ws.receive_bytes()
    assert "VM1" not in serial_console._open


def test_the_slot_is_released_when_connecting_fails(client, app_state):
    _closes_with(client, state=_state(port=1))
    assert "VM1" not in serial_console._open
