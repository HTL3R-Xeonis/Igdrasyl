"""
Goes one level lower than raw_ws_test.py: opens a raw TLS socket, sends a
hand-built HTTP WebSocket-upgrade request byte-for-byte, and prints
exactly what comes back (or the exact point it fails) - no HTTP/WebSocket
library parsing involved at all, so nothing can be hidden behind an
abstraction's own error message.

Usage:
    python raw_http_test.py 10.20.20.202 443 /ticket/<ticket>
"""

from __future__ import annotations

import socket
import ssl
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: python raw_http_test.py <host> <port> <path>", file=sys.stderr)
        return 1

    host, port_str, path = sys.argv[1], sys.argv[2], sys.argv[3]
    port = int(port_str)

    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")

    print(f"Sending raw request to {host}:{port}:")
    print(request.decode("ascii"))

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    raw_sock = socket.create_connection((host, port), timeout=10)
    try:
        sock = ssl_context.wrap_socket(raw_sock, server_hostname=host)
        print(f"TLS handshake OK. Negotiated: {sock.version()} / {sock.cipher()}\n")

        sock.sendall(request)

        sock.settimeout(5)
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except TimeoutError:
            pass

        data = b"".join(chunks)
        print(f"Received {len(data)} byte(s) total.")
        if data:
            print("--- raw response ---")
            print(data.decode("latin-1"))
            print("--- end ---")
        else:
            print(
                "Nothing came back at all before the connection closed/timed out - "
                "the server accepted the TLS connection but never sent an HTTP "
                "response to this request."
            )
        return 0
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        raw_sock.close()


if __name__ == "__main__":
    sys.exit(main())
