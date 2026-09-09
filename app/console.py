"""
GNS3 console embedding: a WebSocket that bridges a browser-side xterm.js
terminal to the raw Telnet TCP connection GNS3 exposes for a node's
console (same pattern ttyd/Wetty use - the interactive human-console path
stays a raw pipe, a human reads prompts themselves, per the project's own
design doc). Automated command push is a separate, later piece (Netmiko),
not this proxy.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket
from loguru import logger

from app.gns3_state import NoLiveProject, list_live_nodes

router = APIRouter()

_READ_CHUNK_SIZE = 4096

# Swallowed silently when a pump task ends with one of these - they're the
# expected shape of "the other side hung up", not a real failure worth
# logging.
_EXPECTED_PUMP_EXCEPTIONS = (ConnectionResetError, ConnectionAbortedError)


async def _pump_tcp_to_ws(reader: asyncio.StreamReader, websocket: WebSocket) -> None:
    while True:
        data = await reader.read(_READ_CHUNK_SIZE)
        if not data:
            return
        await websocket.send_bytes(data)


async def _pump_ws_to_tcp(websocket: WebSocket, writer: asyncio.StreamWriter) -> None:
    while True:
        data = await websocket.receive_bytes()
        writer.write(data)
        await writer.drain()


@router.websocket("/ws/console/{node_name}")
async def console_proxy(websocket: WebSocket, node_name: str) -> None:
    await websocket.accept()
    app = websocket.app

    try:
        live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    except NoLiveProject as exc:
        # 1013 = "Try Again Later" - the closest standard WS close code to
        # "no live project resolved right now", distinct from a client-side
        # mistake (1008) below.
        await websocket.close(code=1013, reason=str(exc)[:120])
        return

    node = live_by_name.get(node_name)
    if node is None:
        await websocket.close(code=1008, reason=f"no such node: {node_name}")
        return

    console_type = node.get("console_type")
    if console_type != "telnet":
        await websocket.close(
            code=1003, reason=f"unsupported console type: {console_type!r}"
        )
        return

    console_port = node.get("console")
    if console_port is None:
        await websocket.close(code=1011, reason="node has no console port assigned")
        return

    try:
        reader, writer = await asyncio.open_connection(
            app.state.gns3_vm_ip, console_port
        )
    except OSError as exc:
        await websocket.close(code=1011, reason=f"couldn't reach console: {exc}")
        return

    tcp_to_ws = asyncio.create_task(_pump_tcp_to_ws(reader, websocket))
    ws_to_tcp = asyncio.create_task(_pump_ws_to_tcp(websocket, writer))
    try:
        done, pending = await asyncio.wait(
            {tcp_to_ws, ws_to_tcp}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, _EXPECTED_PUMP_EXCEPTIONS):
                logger.debug(f"console proxy for {node_name!r} ended with: {exc!r}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed
