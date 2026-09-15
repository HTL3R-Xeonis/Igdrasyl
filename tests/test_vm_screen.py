"""
Tests app/vm_screen.py's WebSocket route. The early-exit paths (no
node, non-ESXi node, VM not found/not powered on, ticket/upstream
failures) are pure request-shape checks, same style as
test_console.py's own. The one real round-trip test proves the actual
WebSocket<->WebSocket byte-pumping (app/ws_bridge.py) against a real
local socket, not a mock - `websockets.connect` is patched to redirect
to that local server regardless of the (always wss://, self-signed-
cert) URL vm_screen.py itself builds, since the TLS/URL-construction
part is trivial string formatting already covered by live verification
(see HANDOFF.md) and not worth a fake TLS cert just to unit-test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyVmomi import vim
from starlette.websockets import WebSocketDisconnect


def _make_ticket(host: str, port: int):
    ticket = MagicMock()
    ticket.host = host
    ticket.port = port
    ticket.ticket = "fake-ticket"
    return ticket


async def _echo_ws(websocket) -> None:
    async for message in websocket:
        await websocket.send(message)


def test_unknown_node_closes_with_1008(client, app_state):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/vm-screen/GHOST") as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 1008


def test_non_esxi_node_closes_with_1008(client, app_state):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/vm-screen/R1") as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 1008


def test_vm_not_found_on_esxi_closes_with_1008(client, app_state):
    app_state.esxi_connection.get_vm.return_value = None
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/vm-screen/VM1") as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 1008


def test_vm_not_powered_on_closes_with_1008(client, app_state):
    vm = MagicMock()
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOff
    app_state.esxi_connection.get_vm.return_value = vm

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/vm-screen/VM1") as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 1008


def test_ticket_acquisition_failure_closes_with_1011(client, app_state):
    vm = MagicMock()
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOn
    vm.AcquireTicket.side_effect = RuntimeError("vCenter unreachable")
    app_state.esxi_connection.get_vm.return_value = vm

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/vm-screen/VM1") as ws:
            ws.receive_bytes()
    assert exc_info.value.code == 1011


def test_upstream_connect_failure_closes_with_1011(client, app_state):
    vm = MagicMock()
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOn
    vm.AcquireTicket.return_value = _make_ticket("10.20.20.202", 443)
    app_state.esxi_connection.get_vm.return_value = vm

    with patch(
        "app.vm_screen.websockets.connect",
        new=AsyncMock(side_effect=OSError("connection refused")),
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/vm-screen/VM1") as ws:
                ws.receive_bytes()
    assert exc_info.value.code == 1011


def test_round_trip_through_real_websocket_upstream(client, app_state, fake_ws_server):
    """
    Real bytes, both directions, through the actual bridge_ws() code
    path - not just that a connection gets accepted.
    """
    port = fake_ws_server(_echo_ws)

    vm = MagicMock()
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOn
    vm.AcquireTicket.return_value = _make_ticket("10.20.20.202", 443)
    app_state.esxi_connection.get_vm.return_value = vm

    import websockets

    real_connect = websockets.connect

    async def _redirect_to_fake_server(_url, **_kwargs):
        return await real_connect(f"ws://127.0.0.1:{port}")

    with patch("app.vm_screen.websockets.connect", new=_redirect_to_fake_server):
        with client.websocket_connect("/ws/vm-screen/VM1") as ws:
            ws.send_bytes(b"RFB 003.008\n")
            assert ws.receive_bytes() == b"RFB 003.008\n"
