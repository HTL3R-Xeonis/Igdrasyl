"""
Bypasses noVNC/wmks.js entirely - tests only whether the raw WebSocket
handshake to a webmks ticket URL succeeds, to isolate whether a failure
is noVNC-specific or a more fundamental TLS/network/ESXi-side problem.

Usage:
    python raw_ws_test.py wss://10.20.20.202:443/ticket/<ticket>
"""

from __future__ import annotations

import asyncio
import ssl
import sys

import websockets


async def run(url: str) -> int:
    # Self-signed ESXi cert - same as everywhere else in this project,
    # not a shortcut specific to this test.
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    print(f"Connecting to {url} ...")
    try:
        async with websockets.connect(
            url, ssl=ssl_context, open_timeout=10
        ) as ws:
            print("WebSocket handshake succeeded.")
            print(f"  Subprotocol negotiated: {ws.subprotocol}")
            print("Waiting up to 5s for the server to send anything...")
            try:
                data = await asyncio.wait_for(ws.recv(), timeout=5)
                print(f"  Received {len(data)} byte(s): {data[:64]!r}")
            except asyncio.TimeoutError:
                print("  Nothing received in 5s (connection stayed open though).")
        return 0
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python raw_ws_test.py <wss://...ticket URL>", file=sys.stderr)
        return 1
    return asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
