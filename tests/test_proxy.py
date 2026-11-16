"""
app/proxy.py: the gateway's HTTP+WebSocket forwarding mount. Runs
against a real local stub server (not a mock) on both legs, since the
whole point of this module is real socket I/O via `requests`/
`websockets` - a `fastapi.testclient`-only test would never exercise
the actual thread/event-loop bridging this module exists for.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import websockets
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.proxy import make_proxy_app


class _Echo(BaseHTTPRequestHandler):
    """Echoes method, path, a chosen response header set and the request body back as JSON-ish text."""

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        reply = (
            f"{self.command} {self.path} seen={self.headers.get('X-Seen', '-')} "
            f"len={len(body)}".encode()
            + b"|"
            + body
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-Stub-Header", "yes")
        self.send_header(
            "X-Upstream-Framing", self.headers.get("Transfer-Encoding", "fixed")
        )
        # A Content-Length is required here regardless: without one, HTTP/1.x has no way to
        # frame the body other than closing the connection, which "Connection: keep-alive"
        # (deliberately sent so the test below can check it's stripped) would then prevent -
        # that is a real protocol ambiguity in this test double, not something app/proxy.py
        # could paper over, so it must not be reintroduced here.
        self.send_header("Content-Length", str(len(reply)))
        self.send_header(
            "Connection", "keep-alive"
        )  # hop-by-hop - must not reach the client
        self.end_headers()
        self.wfile.write(reply)

    do_GET = do_POST = do_PUT = do_DELETE = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def echo_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


def _proxy_client(
    port: int | None, *, forwarded_header: str | None = "X-Forwarded-For"
):
    app = FastAPI()
    target = ("127.0.0.1", port) if port is not None else None
    app.mount("/", make_proxy_app(lambda scope: target))
    return TestClient(app)


def test_forwards_method_path_and_body_and_strips_hop_by_hop(echo_server):
    client = _proxy_client(echo_server)
    response = client.post(
        "/some/path?x=1",
        content=b"hello world",
        headers={"Connection": "close", "X-Seen": "yep"},
    )
    assert response.status_code == 200
    assert response.headers.get("X-Stub-Header") == "yes"
    assert response.headers["X-Upstream-Framing"] == "fixed"
    assert "Connection" not in response.headers  # stripped on the way back
    text = response.text
    assert text.startswith("POST /some/path?x=1 seen=yep")
    assert text.endswith("|hello world")


def test_streams_a_large_body_byte_exact(echo_server):
    client = _proxy_client(echo_server)
    payload = (
        b"0123456789" * 200_000
    )  # ~2MB, several chunks at the proxy's own 64KiB read size
    response = client.post("/big", content=payload)
    assert response.status_code == 200
    assert response.content.endswith(b"|" + payload)
    assert f"len={len(payload)}".encode() in response.content


def test_forwards_client_ip_as_x_forwarded_for(echo_server):
    app = FastAPI()
    captured: dict[str, str] = {}

    class _Capture(BaseHTTPRequestHandler):
        def do_GET(self):
            captured["xff"] = self.headers.get("X-Forwarded-For", "")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        app.mount(
            "/", make_proxy_app(lambda scope: ("127.0.0.1", server.server_address[1]))
        )
        TestClient(app, client=("203.0.113.5", 1234)).get("/anything")
    finally:
        server.shutdown()
    assert captured["xff"] == "203.0.113.5"


def test_no_target_answers_409(echo_server):
    client = _proxy_client(None)
    response = client.get("/anything")
    assert response.status_code == 409


def test_no_target_redirects_a_browser_get_when_configured():
    app = FastAPI()
    app.mount("/", make_proxy_app(lambda scope: None, redirect_html_to="/pick-backend"))
    client = TestClient(app)
    response = client.get(
        "/anything", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/pick-backend"


def test_no_target_still_answers_409_json_for_a_non_html_caller_even_with_redirect_configured():
    app = FastAPI()
    app.mount("/", make_proxy_app(lambda scope: None, redirect_html_to="/pick-backend"))
    client = TestClient(app)
    response = client.get("/anything", follow_redirects=False)
    assert response.status_code == 409


def test_upstream_down_answers_502():
    client = _proxy_client(1)  # port 1: nothing listens there
    response = client.get("/anything")
    assert response.status_code == 502


@pytest.fixture
def ws_echo_server():
    loop_ready = threading.Event()
    state: dict[str, object] = {}
    cookies = []

    async def echo(connection):
        cookies.append(connection.request.headers.get("Cookie"))
        async for message in connection:
            await connection.send(message)

    def run():
        async def main():
            server = await websockets.serve(
                echo, "127.0.0.1", 0, subprotocols=["binary"]
            )
            state["server"] = server
            state["port"] = server.sockets[0].getsockname()[1]
            loop_ready.set()
            await server.wait_closed()

        asyncio.run(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    loop_ready.wait(timeout=5)
    try:
        yield state["port"], cookies
    finally:
        server = state.get("server")
        if server is not None:
            server.close()


def test_websocket_proxy_bridges_frame_types_subprotocol_and_session_cookie(
    ws_echo_server,
):
    port, cookies = ws_echo_server
    app = FastAPI()
    app.mount("/", make_proxy_app(lambda scope: ("127.0.0.1", port)))
    client = TestClient(app)
    with client.websocket_connect(
        "/ws", subprotocols=["binary"], headers={"cookie": "xeonis_session=test"}
    ) as websocket:
        websocket.send_bytes(b"ping")
        assert websocket.receive_bytes() == b"ping"
        websocket.send_text("ping")
        assert websocket.receive_text() == "ping"
    assert cookies == ["xeonis_session=test"]


def test_websocket_proxy_closes_when_no_target():
    app = FastAPI()
    app.mount("/", make_proxy_app(lambda scope: None))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_text()
