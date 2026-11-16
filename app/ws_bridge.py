"""
Shared WebSocket<->WebSocket byte-pumping - app/vm_screen.py's own
counterpart to app/tcp_bridge.py's TCP<->WebSocket bridge. Used to
proxy a browser's WebSocket connection through to a real upstream
WebSocket server (ESXi's own WebMKS ticket endpoint) that the browser
can't safely connect to directly - it needs a fresh, short-lived
ticket acquired server-side right before connecting, and the host's
self-signed TLS cert a browser would otherwise refuse outright.
"""

from __future__ import annotations

import asyncio

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from websockets.exceptions import ConnectionClosed

# Swallowed silently when a pump task ends with one of these - they're the
# expected shape of "the other side hung up", not a real failure worth
# logging. Mirrors app/tcp_bridge.py's own _EXPECTED_PUMP_EXCEPTIONS.
_EXPECTED_PUMP_EXCEPTIONS = (
    ConnectionClosed,
    WebSocketDisconnect,
)


async def _pump_upstream_to_browser(
    upstream: websockets.ClientConnection, websocket: WebSocket, preserve_text: bool
) -> None:
    async for data in upstream:
        if isinstance(data, str) and preserve_text:
            await websocket.send_text(data)
        else:
            await websocket.send_bytes(data.encode() if isinstance(data, str) else data)


async def _pump_browser_to_upstream(
    websocket: WebSocket, upstream: websockets.ClientConnection, preserve_text: bool
) -> None:
    while True:
        if not preserve_text:
            await upstream.send(await websocket.receive_bytes())
            continue
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        await upstream.send(message["text"] if data is None else data)


async def bridge_ws(
    websocket: WebSocket,
    upstream: websockets.ClientConnection,
    *,
    label: str,
    preserve_text: bool = False,
) -> None:
    up_to_browser = asyncio.create_task(
        _pump_upstream_to_browser(upstream, websocket, preserve_text)
    )
    browser_to_up = asyncio.create_task(
        _pump_browser_to_upstream(websocket, upstream, preserve_text)
    )
    try:
        done, pending = await asyncio.wait(
            {up_to_browser, browser_to_up}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, _EXPECTED_PUMP_EXCEPTIONS):
                logger.debug(f"{label} proxy ended with: {exc!r}")
    finally:
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed
