"""
Ginnungagap (TopologyOperator) - the gateway process.

Owns login and the one shared user store exclusively (see
app/routes_gateway.py's own docstring for the full list of what moved
here and why) and forwards everything else to whichever backend the
caller has picked, via app/proxy.py's ASGI mount - registered last, so
it only catches what none of this app's own explicit routes already
claimed.

Run with: uvicorn app.gateway_main:app
Needs TOPOLOGYOPERATOR_USERS_FILE/TOPOLOGYOPERATOR_SESSION_SECRET_FILE/
TOPOLOGYOPERATOR_AI_CONFIG_FILE/
TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR pinned to the same absolute paths
every backend uses (see deploy/README.md). The gateway creates the initial
admin in the shared store. Backends launched by the gateway read that store.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

# Import order matters here: TopologyBuilder's own src.graph.environment and src.connections
# modules import from each other, and whichever gets touched first ends up only partially
# initialized for the other - src.connections first (as app/main.py's own import chain happens
# to do, via app/esxi_session.py and friends) avoids it. app/ai_snippets.py (pulled in by
# app/routes_gateway.py below) is the first thing in this process that would otherwise hit it.
import src.connections  # noqa: F401
from fastapi import FastAPI
from fastapi.responses import FileResponse
from loguru import logger
from src.settings import Settings

from app import (
    internal_credentials,
    logging_setup,
    routes_gateway,
    shared_state,
    users,
)
from app.auth_middleware import AuthMiddleware
from app.proxy import make_proxy_app
from app.request_logging import RequestLoggingMiddleware
from app.routes.template_share import router as template_share_router

_STATIC_DIR = Path(__file__).resolve().parent / "static"
# Exactly the static paths app/auth_middleware.py's _ALLOWED_PATHS lets through without a
# session - the login page needs to render before anyone has picked a backend to proxy to, and
# there is no backend to serve them from yet at that point; everything else (the real app's own
# .js/.css/images) is served by whichever backend the proxy forwards to, same as in a single-process install.
_PRE_LOGIN_ASSETS = {
    "/styles.css": "styles.css",
    "/ginnungagap-mark.svg": "ginnungagap-mark.svg",
    "/favicon.ico": "ginnungagap-mark.svg",
    "/login-bg.jpg": "login-bg.jpg",
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # The gateway only exists in the gateway/backend deployment, so it always needs the shared files (no flag needed).
    shared_state.require_shared_state_paths()
    # The gateway never calls Settings.initialise_settings for any one profile - it isn't any
    # backend's process. Only its own log file path is worth overriding, so backend and gateway
    # logs never collide when both default to a relative "./logs/..." path in the same directory.
    Settings.LOG_FILE_PATH = os.getenv(
        "TOPOLOGYOPERATOR_GATEWAY_LOG_FILE", "./logs/gateway.log"
    )
    logging_setup.configure_logging()
    internal_credentials.log_status()

    bootstrap_password = users.ensure_bootstrap_admin()
    if bootstrap_password:
        logger.warning(
            f"Created initial admin account - username 'admin', password: "
            f"{bootstrap_password} (the default - change it via the admin "
            f"panel as soon as possible) - this message will not repeat"
        )
    yield


app = FastAPI(title="Ginnungagap Gateway", lifespan=_lifespan)
routes_gateway.register(app)
app.include_router(template_share_router)


def _serve_static(filename: str):
    media_type = "image/svg+xml" if filename.endswith(".svg") else None
    return lambda: FileResponse(_STATIC_DIR / filename, media_type=media_type)


for _path, _filename in _PRE_LOGIN_ASSETS.items():
    app.get(_path, include_in_schema=False)(_serve_static(_filename))

app.get("/gateway/host-deploy.js", include_in_schema=False)(
    _serve_static("gateway-host-deploy.js")
)
app.get("/admin.html", include_in_schema=False)(_serve_static("admin.html"))
app.get("/admin.js", include_in_schema=False)(_serve_static("admin.js"))
app.get("/admin-template-share.js", include_in_schema=False)(
    _serve_static("admin-template-share.js")
)

app.add_middleware(AuthMiddleware)
app.add_middleware(RequestLoggingMiddleware)

# Registered last, deliberately - see this module's own docstring and
# app/main.py's identical reasoning for its static-file mount.
app.mount(
    "/",
    make_proxy_app(
        routes_gateway.get_target,
        redirect_html_to="/pick-backend",
        describe_unavailable=routes_gateway.describe_unavailable,
    ),
)
