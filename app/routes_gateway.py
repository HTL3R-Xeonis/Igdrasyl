"""
Phase 3 (app/gateway_main.py): every route that is install-wide rather
than specific to one backend - moved here from app/routes/admin.py and
app/routes/host_settings.py, registered on the gateway's own FastAPI
app instead of the per-profile backend's. Nothing here is new business
logic: it's the same app/users.py/app/ai_snippets.py/
app/settings_profiles.py/app/connection_settings.py/app/ova_templates.py
code those two modules already called, just reached through the
gateway now.

What moved and why, one line each (the full reasoning for each is in
the Phase 3 section of TopologyBuilder/handoff/plans/PLAN_login_multiserver.md):
- user accounts, AI snippet topics (both read+write): shared across every
  backend, so there is exactly one place to manage them - here.
- settings-profile CRUD, connection settings, host-config create/preview/
  test, OVA templates: profile *files* are install-wide, independent of
  any one running backend.
- activate: now "make sure this profile's backend is running, then point
  *my own* session at it" - any logged-in user, not admin-only (Phase 1's
  admin gate on activate existed because it used to restart the one
  shared process for everyone; it no longer does).
- restart: the new, explicit, admin-only "actually restart this one
  backend" action that replaces that old implicit behavior - affects only
  the users currently on that profile, not everyone.

What did NOT move, on purpose: GET/PUT /settings-profiles/{filename}/project
(app/routes/host_settings.py) - never admin-gated before Phase 3, stays
exactly that way and backend-side, reached through the proxy like any other
backend route. Logs (/admin/logs/summarize, /logs*) stay backend-side too -
a backend's own log file genuinely is its own, not install-wide.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from app import (
    ai_snippets,
    backend_manager,
    backend_ports,
    connection_settings,
    ova_templates,
    profile_secrets,
    settings_profiles,
    users,
)
from app.auth import current_user, require_admin
from app.routes import auth as auth_routes
from app.routes.common import settings_call
from app.routes.models import ConnectionSettingsUpdate

BACKEND_COOKIE = "xeonis_backend"

AdminUser = Annotated[tuple[str, str], Depends(require_admin)]
AnyUser = Annotated[tuple[str, str], Depends(current_user)]


def _user_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc))
    return HTTPException(400, str(exc))


def _cookie(scope, name: str) -> str | None:
    for key, value in scope.get("headers", ()):
        if key != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            part = part.strip()
            if part.startswith(f"{name}="):
                return part[len(name) + 1 :]
    return None


def selected_backend(scope) -> str | None:
    """The caller's currently-picked profile filename, or None - app/proxy.py's own get_target reads this."""
    return _cookie(scope, BACKEND_COOKIE)


def get_target(scope) -> tuple[str, int] | None:
    """
    app/proxy.py's GetTarget: the (host, port) of the caller's selected
    backend, or None when there isn't one picked yet or its unit isn't
    actually running right now - both cases get the same "pick/activate
    a backend" response from the proxy, since from a caller's point of
    view they're the same problem.
    """
    filename = selected_backend(scope)
    if not filename or not backend_manager.is_active(filename):
        return None
    port = backend_ports.get_port(filename)
    return ("127.0.0.1", port) if port is not None else None


def _pick_backend_page() -> str:
    tiles = ""
    for profile in settings_profiles.list_profiles():
        filename = profile["filename"]
        if profile.get("error"):
            detail = f"Can't read this profile: {profile['error']}"
        else:
            detail = profile.get("esxi_ip") or "(no ESXi address set)"
        tiles += (
            '<form method="post" action="/settings-profiles/'
            f'{filename}/activate" class="backend-tile">'
            f"<div><strong>{filename}</strong><br><span>{detail}</span></div>"
            '<button type="submit">Activate</button></form>'
        )
    if not tiles:
        tiles = "<p>No host profiles exist yet - an admin creates one under Host Settings.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ginnungagap - Choose a server</title>
<link rel="icon" type="image/svg+xml" href="/ginnungagap-mark.svg">
<link rel="stylesheet" href="/styles.css?v=ginnungagap-55">
<style>
  body {{ display: block; padding: 48px; }}
  h1 {{ font-size: 18px; margin-bottom: 20px; }}
  .backend-tile {{
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    max-width: 480px; margin-bottom: 10px; padding: 14px 18px; border-radius: 10px;
    background: #333; border: 1px solid #444;
  }}
  .backend-tile span {{ color: #aaa; font-size: 13px; }}
  .backend-tile button {{
    padding: 8px 14px; border-radius: 6px; border: none; cursor: pointer;
    background: #7cb342; color: #1c1c1c; font-weight: 500;
  }}
</style>
</head>
<body>
<h1>Choose which server to work on</h1>
{tiles}
</body>
</html>"""


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    role: str


class SetRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str


class SetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str


class AiConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class CreateHostConfig(ConnectionSettingsUpdate):
    filename: str


def register(app: FastAPI) -> None:
    auth_routes.register(app)

    @app.get("/health")
    def gateway_health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/pick-backend", response_class=HTMLResponse)
    def pick_backend(_user: AnyUser) -> str:
        return _pick_backend_page()

    # --- User accounts (app/users.py) -----------------------------------

    @app.get("/admin/users")
    def list_users(_admin: AdminUser) -> list[dict[str, Any]]:
        return users.list_users()

    @app.post("/admin/users", status_code=201)
    def create_user(body: CreateUserRequest, _admin: AdminUser) -> dict[str, Any]:
        try:
            return users.create_user(body.username, body.password, body.role)
        except ValueError as exc:
            raise _user_error(exc) from exc

    @app.delete("/admin/users/{username}")
    def delete_user(username: str, _admin: AdminUser) -> dict[str, str]:
        try:
            users.delete_user(username)
        except (ValueError, FileNotFoundError) as exc:
            raise _user_error(exc) from exc
        return {"status": "ok"}

    @app.put("/admin/users/{username}/role")
    def set_user_role(
        username: str, body: SetRoleRequest, _admin: AdminUser
    ) -> dict[str, Any]:
        try:
            return users.set_role(username, body.role)
        except (ValueError, FileNotFoundError) as exc:
            raise _user_error(exc) from exc

    @app.put("/admin/users/{username}/password")
    def set_user_password(
        username: str, body: SetPasswordRequest, _admin: AdminUser
    ) -> dict[str, str]:
        try:
            users.set_password(username, body.password)
        except (ValueError, FileNotFoundError) as exc:
            raise _user_error(exc) from exc
        return {"status": "ok"}

    # --- AI configuration (app/ai_snippets.py) ---------------------------
    # Mirrors app/routes/admin.py's own /admin/ai-config exactly (same
    # ai_snippets.get_enabled/set_enabled/available functions) - that one
    # stays the live route until Phase 3's migration actually happens (see
    # app/main.py's own docstring); this copy exists so the gateway isn't
    # stale once it does.

    @app.get("/admin/ai-config")
    def get_ai_config(_admin: AdminUser) -> dict[str, bool]:
        return {
            "enabled": ai_snippets.get_enabled(),
            "available": ai_snippets.available(),
        }

    @app.put("/admin/ai-config")
    def set_ai_config(body: AiConfigRequest, _admin: AdminUser) -> dict[str, bool]:
        ai_snippets.set_enabled(body.enabled)
        return get_ai_config(_admin)

    # --- Settings profiles, connections, host-config, OVA templates -----

    @app.get("/settings-profiles")
    def list_settings_profiles() -> list[dict[str, Any]]:
        return settings_profiles.list_profiles()

    @app.post("/settings-profiles")
    async def upload_settings_profile(
        file: UploadFile, _admin: AdminUser
    ) -> dict[str, str]:
        data = await file.read()
        try:
            settings_profiles.save_profile(file.filename, data)
        except settings_profiles.InvalidProfileName as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "ok"}

    @app.delete("/settings-profiles/{filename}")
    def delete_settings_profile(filename: str, _admin: AdminUser) -> dict[str, str]:
        try:
            settings_profiles.delete_profile(filename)
            profile_secrets.delete(filename)
        except settings_profiles.InvalidProfileName as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        backend_ports.forget_port(filename)
        return {"status": "ok"}

    @app.get("/host-config/schema")
    def host_config_schema():
        return {"groups": connection_settings.schema()}

    @app.get("/ova-templates")
    def list_ova_templates():
        try:
            return {"templates": ova_templates.list_public()}
        except (RuntimeError, TypeError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/host-config/preview")
    def preview_host_config(body: ConnectionSettingsUpdate):
        return {"yaml": settings_call(connection_settings.render_config, body.fields)}

    @app.post("/host-config/create")
    def create_host_config(body: CreateHostConfig, _admin: AdminUser):
        settings_call(profile_secrets.validate, body.secrets)
        try:
            created = settings_call(
                connection_settings.create_profile, body.filename, body.fields
            )
        except FileExistsError as exc:
            raise HTTPException(
                409, "A host profile with that filename already exists"
            ) from exc
        settings_call(profile_secrets.save, body.filename, body.secrets)
        return created

    @app.post("/host-config/test")
    def test_host_config(body: ConnectionSettingsUpdate):
        return {
            "results": settings_call(
                connection_settings.test_connections, None, body.fields
            )
        }

    @app.get("/settings-profiles/{filename}/connections")
    def get_connection_settings(filename: str):
        return {
            "fields": settings_call(connection_settings.read_fields, filename),
            "passwords_set": settings_call(profile_secrets.status, filename),
        }

    @app.put("/settings-profiles/{filename}/connections")
    def save_connection_settings(
        filename: str, body: ConnectionSettingsUpdate, _admin: AdminUser
    ):
        settings_call(profile_secrets.validate, body.secrets)
        settings_call(connection_settings.update_fields, filename, body.fields)
        settings_call(profile_secrets.save, filename, body.secrets)
        return {"status": "saved", "restart_required": True}

    @app.post("/settings-profiles/{filename}/connections/test")
    def test_connection_settings(filename: str, body: ConnectionSettingsUpdate):
        # Phase 1's test_connection_settings() also probed the GNS3 VM address of
        # whichever profile happened to be the *current process's* live one
        # (app.state.gns3_vm_ip) - the gateway never runs a topology/GNS3 session
        # itself, so that one sub-check is skipped here rather than guessed at.
        return {
            "results": settings_call(
                connection_settings.test_connections, filename, body.fields, None
            )
        }

    # --- Activate / restart a backend ------------------------------------

    @app.post("/settings-profiles/{filename}/activate")
    def activate_settings_profile(filename: str, _user: AnyUser) -> Response:
        """
        Makes sure this profile's backend is running (starting it if
        needed, assigning its port the first time), waits for it to
        answer healthy, then points the caller's own session at it -
        every other user stays on whatever they already had selected.
        Any logged-in user may do this (Phase 1's admin-only gate here
        existed because this used to restart the one shared process for
        everyone - it no longer does); see restart below for the
        explicit, admin-only, affects-everyone-on-it action.
        """
        try:
            settings_profiles.profile_path(filename)
        except settings_profiles.InvalidProfileName as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            port = backend_manager.ensure_running(filename)
        except backend_ports.NoFreePort as exc:
            raise HTTPException(503, str(exc)) from exc
        if not backend_manager.wait_healthy(port):
            raise HTTPException(
                503, f"{filename} started but didn't answer healthy in time"
            )
        response = JSONResponse({"status": "active"})
        response.set_cookie(BACKEND_COOKIE, filename, httponly=True, samesite="lax")
        return response

    @app.post("/settings-profiles/{filename}/restart")
    def restart_backend(filename: str, _admin: AdminUser) -> dict[str, str]:
        """Admin-only: restarts this one profile's backend - affects only whoever is currently on it, not everyone."""
        try:
            settings_profiles.profile_path(filename)
        except settings_profiles.InvalidProfileName as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        backend_manager.restart(filename)
        return {"status": "restarting"}
