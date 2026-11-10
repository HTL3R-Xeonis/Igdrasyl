"""
Ginnungagap (TopologyOperator) - the FastAPI process: wiring only.

The endpoints live in app/routes/, the startup in app/startup.py, the FastAPI instance in
app/application.py. This module attaches the middleware, the error handlers, the WebSocket
routes, every route module and the frontend, and nothing else.
Everything else (GNS3 console proxy, config push, embedded VM screens)
plugs into the connections/graph this module resolves once at startup.

Run with: uvicorn app.main:app --reload
Point TOPOLOGYOPERATOR_SETTINGS at a settings.yml first (see
settings.example.yml) - without one, TopologyBuilder's own hardcoded
defaults apply, which won't match this project's real infrastructure.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

from fastapi import Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import (
    console,
    error_pages,
    esxi_session,
    public_text,
    serial_console,
    vm_screen,
)
from app.application import app
from app.request_logging import RequestLoggingMiddleware

# Importing a route module registers its routes. Starlette tries routes in registration order, so this
# order is the matching order; the frontend mount below must stay last.
for _module in (
    "system",
    "live",
    "push",
    "design",
    "projects",
    "maintenance",
    "host_settings",
):
    importlib.import_module(f"app.routes.{_module}")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_LICENSE_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "LICENSE")


app.add_middleware(RequestLoggingMiddleware)
app.include_router(console.router)
app.include_router(serial_console.router)
app.include_router(vm_screen.router)


@app.exception_handler(StarletteHTTPException)
async def path_safe_http_error(request: Request, exc: StarletteHTTPException):
    # Covers FastAPI's HTTPException and Starlette's own (unknown route,
    # wrong method, missing static file). Browsers navigating get a page.
    if error_pages.wants_html(request):
        return error_pages.render(
            exc.status_code, public_text.redact_paths(str(exc.detail))
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": public_text.redact_paths(exc.detail)},
        headers=exc.headers,
    )


@app.exception_handler(esxi_session.EsxiUnavailable)
async def esxi_unavailable(request: Request, exc: esxi_session.EsxiUnavailable):
    # Our own wording, no internals: the host can't be reached right now.
    if error_pages.wants_html(request):
        return error_pages.render(503, str(exc))
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unexpected_error(request: Request, _exc: Exception):
    # Already logged with its traceback by RequestLoggingMiddleware.
    if error_pages.wants_html(request):
        return error_pages.render(500)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/license")
def get_license() -> Response:
    """
    The repo-root LICENSE file as plain text, read fresh on every
    request rather than duplicated into app/static - for the landing
    page's own "View License" link.
    """
    with open(_LICENSE_PATH) as file:
        return Response(content=file.read(), media_type="text/plain")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Browsers ask for /favicon.ico on pages without an icon link (the log page, error pages): answer with the mark."""
    return FileResponse(
        Path(_STATIC_DIR) / "ginnungagap-mark.svg", media_type="image/svg+xml"
    )


# Mounted last, deliberately - Starlette tries explicit routes (above) in
# registration order before falling through to a mount, so this catch-all
# for "/" can't shadow the API/WS routes as long as it's registered after
# them.
class _RevalidatedStaticFiles(StaticFiles):
    """
    The page and its scripts are checked with the server on every load (a cheap 304 when nothing changed). Without it a
    browser may keep serving an old index.html for a while, and with it the old script, after the app was updated.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount(
    "/", _RevalidatedStaticFiles(directory=_STATIC_DIR, html=True), name="frontend"
)
