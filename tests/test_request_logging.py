"""
Tests app/request_logging.py against a tiny app of its own: what is
logged per request (and at which level), and - as important - what is
deliberately never logged.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from loguru import logger
from starlette.websockets import WebSocketDisconnect

from app.request_logging import RequestLoggingMiddleware


@pytest.fixture
def records():
    collected: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: collected.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="TRACE",
    )
    yield collected
    logger.remove(sink_id)


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/nodes")
    def read():
        return {"ok": True}

    @app.post("/nodes")
    def write():
        return {"ok": True}

    @app.get("/missing-thing")
    def missing():
        raise HTTPException(404, "nope")

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaputt")

    @app.get("/logs")
    def logs():
        return []

    @app.get("/app.js")
    def static_asset():
        return {"js": True}

    @app.get("/stream")
    def stream():
        return StreamingResponse(iter([b"one,", b"two,", b"three"]))

    @app.websocket("/ws/console/R1")
    async def console(websocket: WebSocket):
        await websocket.accept()
        await websocket.close(code=1013)

    return TestClient(app, raise_server_exceptions=False)


def _messages(records, level=None):
    return [m for lvl, m in records if level is None or lvl == level]


def test_a_read_is_logged_at_debug_with_status_duration_and_client(client, records):
    client.get("/nodes")

    [(level, message)] = [r for r in records if "HTTP GET /nodes" in r[1]]
    assert level == "DEBUG"
    assert "-> 200 in " in message and "s (client " in message


def test_a_state_changing_request_is_logged_at_info(client, records):
    client.post("/nodes")

    assert any(lvl == "INFO" and "HTTP POST /nodes -> 200" in m for lvl, m in records)


def test_a_client_error_is_a_warning(client, records):
    client.get("/missing-thing")

    assert any(
        lvl == "WARNING" and "HTTP GET /missing-thing -> 404" in m for lvl, m in records
    )


def test_an_unhandled_exception_is_an_error_with_its_traceback(client, records):
    response = client.get("/boom")

    assert response.status_code == 500
    errors = _messages(records, "ERROR")
    assert any("HTTP GET /boom raised RuntimeError: kaputt" in m for m in errors)


def test_the_query_string_is_never_logged(client, records):
    client.get("/nodes?password=hunter2&token=abc")

    assert not any("hunter2" in m or "token" in m or "?" in m for _, m in records)


def test_the_log_viewers_own_requests_are_not_logged(client, records):
    client.get("/logs?lines=300")

    assert not any("/logs" in m for _, m in records)


def test_a_static_asset_is_trace_so_the_file_sink_leaves_it_out(client, records):
    client.get("/app.js")

    assert [lvl for lvl, m in records if "/app.js" in m] == ["TRACE"]


def test_a_streaming_response_arrives_whole_and_is_logged_once(client, records):
    response = client.get("/stream")

    assert response.content == b"one,two,three"
    assert len([m for m in _messages(records) if "HTTP GET /stream" in m]) == 1


def test_a_websocket_session_is_logged_on_accept_and_end(client, records):
    with client.websocket_connect("/ws/console/R1") as socket:
        with pytest.raises(WebSocketDisconnect):  # closed by the server right away
            socket.receive_text()

    messages = _messages(records, "INFO")
    assert any("WS /ws/console/R1 accepted" in m for m in messages)
    assert any(
        "WS /ws/console/R1 ended after" in m and "closed with code 1013" in m
        for m in messages
    )


def test_x_forwarded_for_replaces_the_loopback_address_a_proxy_would_otherwise_show(
    client, records
):
    # A backend behind app/gateway_main.py's proxy would otherwise only ever see the
    # gateway's own loopback address as scope["client"] - this header is how it learns the real one.
    client.get("/nodes", headers={"X-Forwarded-For": "203.0.113.7"})

    [(_, message)] = [r for r in records if "HTTP GET /nodes" in r[1]]
    assert "(client 203.0.113.7)" in message


def test_the_session_cookie_is_shown_as_the_user_when_present(client, records):
    from app.auth import SESSION_COOKIE, create_session_token

    token = create_session_token("alice", "admin")
    client.cookies.set(SESSION_COOKIE, token)
    client.get("/nodes")

    [(_, message)] = [r for r in records if "HTTP GET /nodes" in r[1]]
    assert ", user alice)" in message


def test_a_tampered_session_cookie_is_ignored_rather_than_shown_as_a_user(
    client, records
):
    from app.auth import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, "not-a-real-token")
    client.get("/nodes")

    [(_, message)] = [r for r in records if "HTTP GET /nodes" in r[1]]
    assert "user" not in message
