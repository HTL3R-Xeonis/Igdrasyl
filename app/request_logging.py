"""
Logs every HTTP request and WebSocket session the app serves: method,
path, status, how long it took and who asked - nothing else. Never
headers, query strings or bodies, since those can carry credentials
(e.g. the host connection settings).

The level carries the importance so the file stays readable even though
it is verbose: failures are ERROR (5xx or an exception) / WARNING (4xx),
anything that changes state INFO, plain reads DEBUG - the UI polls
several of them every few seconds - and static assets TRACE, which the
file sink leaves out. The log viewer's own requests are skipped entirely,
otherwise watching the log would fill it.

A pure ASGI middleware rather than Starlette's BaseHTTPMiddleware, which
buffers streaming responses - the packet capture stream is one.
"""

from __future__ import annotations

import time

from loguru import logger

from app.auth import SESSION_COOKIE, verify_session_token

_SKIPPED_PREFIXES = ("/logs",)
_STATIC_SUFFIXES = (
    ".js",
    ".css",
    ".svg",
    ".png",
    ".jpg",
    ".ico",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
)
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _http_level(method: str, path: str, status: int | None) -> str | None:
    """The level a finished request is logged at; None means not at all."""
    if path.startswith(_SKIPPED_PREFIXES):
        return None
    if status is None or status >= 500:
        return "ERROR"
    if status >= 400:
        return "WARNING"
    if path.endswith(_STATIC_SUFFIXES):
        return "TRACE"
    return "DEBUG" if method in _READ_METHODS else "INFO"


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _client(scope) -> str:
    # Behind the gateway (app/gateway_main.py's proxy): every request a backend sees otherwise comes from the
    # gateway's own loopback address, which would lose per-user audit fidelity in exactly the logs
    # the admin panel summarizes - the gateway sets this header, trusted since a backend is only
    # ever reachable through it. app/proxy.py sets the same header when forwarding.
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        return forwarded
    client = scope.get("client")
    return client[0] if client else "-"


def _trusted_user(scope) -> str | None:
    # Behind the gateway (app/gateway_main.py's proxy): a backend is only ever reachable through the gateway,
    # and the gateway forwards the original session cookie unchanged (it isn't hop-by-hop), so the
    # same cookie this process would otherwise use for its own login check also tells it who's
    # asking - cheaper and simpler than a separate trust header, and it works unchanged in Phase
    # 1/2's single-process deployment too (there the cookie was always local to begin with).
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(f"{SESSION_COOKIE}="):
            user = verify_session_token(part[len(SESSION_COOKIE) + 1 :])
            return user[0] if user else None
    return None


def _who(scope) -> str:
    user = _trusted_user(scope)
    client = _client(scope)
    return f"{client}, user {user}" if user else client


class RequestLoggingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _http(self, scope, receive, send) -> None:
        method = scope["method"]
        path = scope["path"]
        started = time.monotonic()
        status: int | None = None

        async def send_wrapper(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if not path.startswith(_SKIPPED_PREFIXES):
                logger.opt(exception=exc).error(
                    f"HTTP {method} {path} raised {type(exc).__name__}: {exc} "
                    f"after {time.monotonic() - started:.3f}s (client {_who(scope)})"
                )
            raise

        level = _http_level(method, path, status)
        if level is not None:
            logger.log(
                level,
                f"HTTP {method} {path} -> {status} in "
                f"{time.monotonic() - started:.3f}s (client {_who(scope)})",
            )

    async def _websocket(self, scope, receive, send) -> None:
        path = scope["path"]
        started = time.monotonic()
        client = _who(scope)
        close_code: int | None = None

        async def send_wrapper(message) -> None:
            nonlocal close_code
            if message["type"] == "websocket.accept":
                logger.info(f"WS {path} accepted (client {client})")
            elif message["type"] == "websocket.close":
                close_code = message.get("code", 1000)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            logger.opt(exception=exc).error(
                f"WS {path} raised {type(exc).__name__}: {exc} "
                f"after {time.monotonic() - started:.1f}s (client {client})"
            )
            raise
        logger.info(
            f"WS {path} ended after {time.monotonic() - started:.1f}s"
            + (f", closed with code {close_code}" if close_code is not None else "")
            + f" (client {client})"
        )
