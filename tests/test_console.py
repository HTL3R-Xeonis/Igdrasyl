from unittest.mock import patch

import pytest
from starlette.websockets import WebSocketDisconnect


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


def _live_nodes(**overrides):
    node = {
        "name": "R1",
        "node_id": "n1",
        "status": "started",
        "console": None,
        "console_type": "telnet",
    }
    node.update(overrides)
    return [node]


def test_echo_round_trip_through_real_tcp_server(client, app_state, fake_tcp_server):
    port = fake_tcp_server(_echo)
    app_state.project_id = "proj-1"
    with patch(
        "app.console.list_live_nodes", return_value=_live_nodes(console=port)
    ):
        with client.websocket_connect("/ws/console/R1") as ws:
            ws.send_bytes(b"hello console")
            assert ws.receive_bytes() == b"hello console"


def test_unknown_node_closes_with_1008(client, app_state):
    app_state.project_id = "proj-1"
    with patch("app.console.list_live_nodes", return_value=_live_nodes()):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/console/NOPE") as ws:
                ws.receive_bytes()
    assert exc_info.value.code == 1008


def test_unsupported_console_type_closes_with_1003(client, app_state):
    app_state.project_id = "proj-1"
    with patch(
        "app.console.list_live_nodes",
        return_value=_live_nodes(console_type="vnc"),
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/console/R1") as ws:
                ws.receive_bytes()
    assert exc_info.value.code == 1003


def test_missing_console_port_closes_with_1011(client, app_state):
    app_state.project_id = "proj-1"
    with patch("app.console.list_live_nodes", return_value=_live_nodes(console=None)):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/console/R1") as ws:
                ws.receive_bytes()
    assert exc_info.value.code == 1011


def test_no_live_project_closes_with_1013(client, app_state):
    from app.gns3_state import NoLiveProject

    app_state.project_id = None
    with patch(
        "app.console.list_live_nodes",
        side_effect=NoLiveProject("nothing deployed"),
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/console/R1") as ws:
                ws.receive_bytes()
    assert exc_info.value.code == 1013


def test_unreachable_console_closes_with_1011(client, app_state):
    app_state.project_id = "proj-1"
    with patch(
        "app.console.list_live_nodes", return_value=_live_nodes(console=1)
    ):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/console/R1") as ws:
                ws.receive_bytes()
    assert exc_info.value.code == 1011
