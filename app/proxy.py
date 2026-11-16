"""
Phase 3 (gateway + per-profile backend): the raw ASGI app that forwards
everything the gateway doesn't handle itself to whichever backend the
caller is pointed at - HTTP and WebSocket alike, in one callable, the
same way app/main.py's own static-file mount is a plain ASGI app
mounted at "/" and registered last. A mount is tried only after every
explicit route, so this never shadows the gateway's own login/admin/
settings routes as long as it is added to the gateway app last.

Uses `requests` (this project's established HTTP client, see
pyproject.toml) run inside a worker thread via
starlette.concurrency.run_in_threadpool - never called directly on the
event loop, since a hand-rolled ASGI mount gets none of the automatic
threadpool-wrapping FastAPI gives an ordinary sync `def` route. Both
directions of an HTTP request are streamed (large uploads - project
import, settings-profile files - are read from ASGI `receive()` as
they arrive, not buffered whole in memory first, same as the response
side already needs for log/diagram/pcap downloads): the worker thread
calls back into the event loop via `asyncio.run_coroutine_threadsafe`
for every `receive()`/`send()` it needs, which is the only correct way
to use async ASGI primitives from a synchronous function running in a
different thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator

import requests
import websockets
from loguru import logger
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket

from app.ws_bridge import bridge_ws

Target = tuple[str, int]
# None means "no backend to send this to right now" - the caller decides what that means (pick a
# backend, or the chosen one isn't running) via the scope it was given.
GetTarget = Callable[[dict], Target | None]

# Stripped from both directions: forwarding these verbatim either duplicates what uvicorn/the
# upstream server already frames itself (Transfer-Encoding) or leaks a hop that ended at the gateway.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailers",
        "proxy-authenticate",
        "proxy-authorization",
    }
)


def _client_ip(scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


def _request_headers(scope, *, forwarded_for: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in scope["headers"]:
        name = key.decode("latin-1")
        if name.lower() in _HOP_BY_HOP:
            continue
        headers[name] = value.decode("latin-1")
    if forwarded_for:
        # app/request_logging.py's own _client() prefers this over scope["client"], which would
        # otherwise show every request as coming from the gateway's own loopback address.
        headers["X-Forwarded-For"] = forwarded_for
    return headers


def _request_body_chunks(
    receive: Callable, loop: asyncio.AbstractEventLoop
) -> Iterator[bytes]:
    """Runs in the proxy's worker thread: pulls each ASGI body chunk from the event loop, blocking this thread until it arrives."""
    more = True
    while more:
        message = asyncio.run_coroutine_threadsafe(receive(), loop).result()
        body = message.get("body", b"")
        more = message.get("more_body", False)
        if body:
            yield body


def _do_http_proxy(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    receive: Callable,
    send: Callable,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """
    The entire HTTP leg, run in a worker thread by run_in_threadpool - a
    plain synchronous function using `requests`. Every send() the
    caller sees is bounced back onto the event loop via
    run_coroutine_threadsafe, so this stays ordinary blocking code.
    """

    def send_async(message: dict) -> None:
        asyncio.run_coroutine_threadsafe(send(message), loop).result()

    body = (
        _request_body_chunks(receive, loop) if method not in ("GET", "HEAD") else None
    )
    try:
        with requests.request(
            method,
            url,
            headers=headers,
            data=body,
            stream=True,
            timeout=(5, 300),
            allow_redirects=False,
        ) as response:
            response_headers = [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in response.headers.items()
                if key.lower() not in _HOP_BY_HOP
            ]
            send_async(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": response_headers,
                }
            )
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    send_async(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    )
            send_async({"type": "http.response.body", "body": b"", "more_body": False})
    except requests.RequestException as exc:
        logger.warning(f"proxy: {method} {url} failed: {exc}")
        send_async(
            {
                "type": "http.response.start",
                "status": 502,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        send_async(
            {
                "type": "http.response.body",
                "body": f"Upstream unreachable: {exc}".encode(),
                "more_body": False,
            }
        )


async def _proxy_http(scope, receive, send, target: Target) -> None:
    host, port = target
    query_string = scope.get("query_string", b"")
    path = scope["path"] + (
        "?" + query_string.decode("latin-1") if query_string else ""
    )
    headers = _request_headers(scope, forwarded_for=_client_ip(scope))
    await run_in_threadpool(
        _do_http_proxy,
        method=scope["method"],
        url=f"http://{host}:{port}{path}",
        headers=headers,
        receive=receive,
        send=send,
        loop=asyncio.get_running_loop(),
    )


async def _proxy_websocket(scope, receive, send, target: Target) -> None:
    host, port = target
    websocket = WebSocket(scope, receive=receive, send=send)
    query_string = scope.get("query_string", b"")
    path = scope["path"] + (
        "?" + query_string.decode("latin-1") if query_string else ""
    )
    subprotocols = scope.get("subprotocols") or None
    # The backend still checks signed sessions alongside the single-process app.
    cookie = websocket.headers.get("cookie")
    try:
        upstream = await websockets.connect(
            f"ws://{host}:{port}{path}",
            subprotocols=subprotocols,
            additional_headers={"Cookie": cookie} if cookie else None,
        )
    except Exception as exc:
        # Any upstream failure (refused, wrong subprotocol, DNS, ...) must become a clean close,
        # never an unhandled exception here - the upstream is arbitrary and not ours to trust.
        logger.warning(f"proxy: WS {path} couldn't reach upstream {host}:{port}: {exc}")
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=upstream.subprotocol)
    await bridge_ws(websocket, upstream, label=f"proxy {path}", preserve_text=True)


def _wants_html(scope) -> bool:
    for key, value in scope.get("headers", ()):
        if key == b"accept":
            return b"text/html" in value
    return False


async def _unavailable(scope, send, *, redirect_html_to: str | None) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    if redirect_html_to and scope["method"] == "GET" and _wants_html(scope):
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [(b"location", redirect_html_to.encode("latin-1"))],
            }
        )
        await send({"type": "http.response.body", "body": b""})
        return
    await send(
        {
            "type": "http.response.start",
            "status": 409,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b'{"detail":"No backend selected or it is not running yet."}',
        }
    )


def make_proxy_app(get_target: GetTarget, *, redirect_html_to: str | None = None):
    """
    `get_target(scope)` decides, per request, which `(host, port)` to
    forward to - typically derived from the caller's `xeonis_backend`
    cookie - or returns None when there isn't one to send this to yet.
    `redirect_html_to`, if given, sends a browser GET with no target to
    that path (303) instead of the generic 409 JSON - e.g. the
    gateway's own "pick a backend" page; a non-GET or non-HTML caller
    (fetch/API) always gets the 409, since redirecting those would just
    break whatever called them.
    """

    async def proxy_app(scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return
        target = get_target(scope)
        if target is None:
            await _unavailable(scope, send, redirect_html_to=redirect_html_to)
            return
        if scope["type"] == "websocket":
            await _proxy_websocket(scope, receive, send, target)
        else:
            await _proxy_http(scope, receive, send, target)

    return proxy_app
