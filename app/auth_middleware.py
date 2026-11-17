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

import re

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

# Service-to-service GETs that skip the session check because the route itself requires a bearer token
# (app/internal_credentials.py). Any other method on these paths is treated like every protected path.
_BEARER_PATHS = frozenset({"/internal/host-credentials"})

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# A "readonly" user may mutate exactly this: picking which backend/session
# their own view points at (app/routes_gateway.py's activate - any logged-in
# user, not an infrastructure change). /logout needs no exemption here - it's
# already in _ALLOWED_PATHS above, reachable without even a valid session.
_READONLY_EXEMPT_PATTERNS = (re.compile(r"^/settings-profiles/[^/]+/activate$"),)


def _readonly_exempt(path: str) -> bool:
    return any(pattern.match(path) for pattern in _READONLY_EXEMPT_PATTERNS)


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
        if path in _ALLOWED_PATHS or (
            path in _BEARER_PATHS
            and scope["type"] == "http"
            and scope["method"] == "GET"
        ):
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

        _, role = user
        if (
            role == "readonly"
            and scope["type"] == "http"
            and scope["method"] not in _SAFE_METHODS
            and not _readonly_exempt(path)
        ):
            if _wants_html(scope):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"Read-only access"})
            else:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"detail":"Read-only access"}',
                    }
                )
            return

        await self.app(scope, receive, send)
