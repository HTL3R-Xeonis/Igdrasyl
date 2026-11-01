"""
Interactive serial console of an ESXi VM, in the browser.

The VM's network serial port (see app/esxi_serial.py) is a TCP server on the ESXi host; this bridges it to the
browser's xterm.js terminal over a WebSocket, the same raw pipe app/console.py uses for GNS3 nodes. The line itself
is plain TCP and accepts one client at a time, so two things share it: this console, and the serial config push
(app/serial_agent.py). A second client gets no data, which is why a console that is already open is refused up
front, and why a failed connect says the port may be in use.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket
from loguru import logger
from src.settings import Settings

from app import esxi_serial
from app.tcp_bridge import bridge

router = APIRouter()

_CONNECT_TIMEOUT_SECONDS = 10
# VMs whose console is open in this process (the port takes one client at a time).
_open: set[str] = set()


@router.websocket("/ws/serial/{node_name}")
async def serial_console_proxy(websocket: WebSocket, node_name: str) -> None:
    await websocket.accept()
    app = websocket.app

    try:
        state = await asyncio.to_thread(esxi_serial.status, app, node_name)
    except esxi_serial.SerialSetupError as exc:
        await websocket.close(code=1008, reason=str(exc)[:120])
        return
    except Exception as exc:  # pyvmomi / network errors while reading the VM
        logger.warning(
            f"serial console of {node_name!r}: couldn't read the VM: {exc!r}"
        )
        await websocket.close(
            code=1011, reason="couldn't read the VM's state from ESXi"
        )
        return

    # A WebSocket close reason is limited to 123 bytes, so these stay short.
    if not state["enabled"]:
        await websocket.close(
            code=1011, reason="this VM has no serial console - enable it first"
        )
        return
    if not state["powered_on"]:
        await websocket.close(
            code=1011,
            reason="the VM is powered off - the serial line only exists while it runs",
        )
        return
    if node_name in _open:
        await websocket.close(
            code=1013, reason="a serial console of this VM is already open"
        )
        return

    host, port = Settings.ESXI.IP, state["port"]
    _open.add(node_name)
    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=_CONNECT_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError) as exc:
            logger.info(
                f"serial console of {node_name!r}: can't reach {host}:{port}: {exc!r}"
            )
            await websocket.close(
                code=1011,
                reason="can't reach the serial port - in use (config push?) or the VM was not power-cycled",
            )
            return
        logger.info(f"serial console of {node_name!r} opened ({host}:{port})")
        await bridge(
            websocket, reader, writer, label=f"serial console of {node_name!r}"
        )
        logger.info(f"serial console of {node_name!r} closed")
    finally:
        _open.discard(node_name)
