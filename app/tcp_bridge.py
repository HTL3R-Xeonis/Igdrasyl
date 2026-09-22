"""
Shared TCP<->WebSocket byte-pumping for app/console.py's GNS3 Telnet
console proxy: a plain-text TCP stream bridged to a browser xterm.js
terminal over a WebSocket. Originally shared with a second consumer, a
now-removed ESXi VM serial-console feature that was the same shape of
thing underneath - kept as its own module rather than folded back into
console.py, since a second consumer could reasonably need it again.
"""

from __future__ import annotations

import asyncio

from fastapi import WebSocket
from loguru import logger

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


async def bridge(
    websocket: WebSocket,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    label: str,
) -> None:
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
                logger.debug(f"{label} proxy ended with: {exc!r}")
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
