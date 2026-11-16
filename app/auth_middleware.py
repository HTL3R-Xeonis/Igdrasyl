"""
Enforces a logged-in session for every request except the login page itself
and the handful of static assets it needs to render (nothing secret in any
of them). A pure ASGI middleware, same reasoning as
app/request_logging.py's RequestLoggingMiddleware - runs before routing, so
it never buffers a streaming/WebSocket response, and no individual route
needs its own login check (app/auth.py's current_user/require_admin
dependencies are for the few routes that need the *role*, e.g. GET /me and
the admin-only ones - everything else just needs "logged in at all", which
this covers once for the whole app).
"""

from __future__ import annotations

from app.auth import SESSION_COOKIE, verify_session_token

_ALLOWED_PATHS = frozenset(
    {
        "/login",
        "/logout",
        "/health",
        "/gateway/health",  # the gateway's own process liveness - see app/routes_gateway.py
        "/favicon.ico",
        "/styles.css",
        "/ginnungagap-mark.svg",
        "/login-bg.jpg",
    }
)


def _header(scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value
    return None


def _cookie(scope, name: str) -> str | None:
    raw = _header(scope, b"cookie")
    if raw is None:
        return None
    for part in raw.decode("latin-1").split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part[len(name) + 1 :]
    return None


def _wants_html(scope) -> bool:
    accept = _header(scope, b"accept")
    return bool(accept) and b"text/html" in accept


class AuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path in _ALLOWED_PATHS:
            await self.app(scope, receive, send)
            return

        token = _cookie(scope, SESSION_COOKIE)
        user = verify_session_token(token) if token else None
        if user is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            if _wants_html(scope):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 303,
                        "headers": [(b"location", b"/login")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
            else:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"detail":"Not logged in"}',
                    }
                )
            return

        await self.app(scope, receive, send)
