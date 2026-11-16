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
- user accounts and AI configuration: shared across every
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

from html import escape
from threading import Lock
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from loguru import logger
from pydantic import BaseModel, ConfigDict

from app import (
    ai_snippets,
    audit_log,
    backend_manager,
    backend_ports,
    backend_presence,
    connection_settings,
    error_pages,
    ova_templates,
    profile_secrets,
    settings_profiles,
    users,
)
from app.auth import SESSION_COOKIE, current_user, require_admin, verify_session_token
from app.routes import auth as auth_routes
from app.routes.common import settings_call
from app.routes.models import ConnectionSettingsUpdate

BACKEND_COOKIE = "xeonis_backend"

AdminUser = Annotated[tuple[str, str], Depends(require_admin)]
AnyUser = Annotated[tuple[str, str], Depends(current_user)]

AllowedHosts = str | list[str]


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


def _caller_username(scope) -> str | None:
    """The username from the forwarded session cookie, if any - same technique as app/request_logging.py's own _trusted_user()."""
    raw = _cookie(scope, SESSION_COOKIE)
    if not raw:
        return None
    user = verify_session_token(raw)
    return user[0] if user else None


def get_target(scope) -> tuple[str, int] | None:
    """
    app/proxy.py's GetTarget: the (host, port) of the caller's selected
    backend, or None when there isn't one picked yet or its unit isn't
    actually running or permitted - those cases get the same "pick/activate
    a backend" response from the proxy, since from a caller's point of
    view they're the same problem.
    """
    filename = selected_backend(scope)
    username = _caller_username(scope)
    if not filename or not username or not users.can_access_host(username, filename):
        return None
    port = backend_ports.get_port(filename)
    if port is None or not backend_manager.is_active(filename):
        return None
    backend_presence.record(filename, username)
    return ("127.0.0.1", port)


def _pick_backend_page(user: tuple[str, str]) -> str:
    username, role = user
    record = users.get_user(username) or {"role": role, "allowed_hosts": "ALL"}
    tiles = ""
    for profile in settings_profiles.list_profiles():
        filename = profile["filename"]
        if not users.can_access_host(record, filename):
            continue
        if profile.get("error"):
            detail = f"Can't read this profile: {profile['error']}"
        else:
            detail = profile.get("esxi_ip") or "(no ESXi address set)"
        active = backend_presence.active_users(filename)
        presence = ""
        if active:
            # A plain count for everyone; admins additionally see who, since
            # that's exactly what's useful before deciding to restart a
            # shared host someone else might be looking at right now.
            who = f": {escape(', '.join(active))}" if role == "admin" else ""
            noun = "user" if len(active) == 1 else "users"
            presence = f'<span class="backend-presence">{len(active)} active {noun}{who}</span>'
        deploy = (
            f'<button type="button" data-deploy-profile="{escape(filename)}">Deploy GNS3 VM…</button>'
            '<span data-deploy-status role="status"></span>'
            if role == "admin" and not profile.get("error")
            else ""
        )
        tiles += (
            '<div class="backend-tile">'
            f'<div class="backend-tile-info"><strong>{escape(filename)}</strong><span>{escape(str(detail))}</span>{presence}</div>'
            '<div class="backend-tile-actions"><form method="post" action="/settings-profiles/'
            f'{escape(filename)}/activate"><button type="submit">Activate</button></form>'
            f"{deploy}</div></div>"
        )
    if not tiles:
        tiles = '<p class="backend-picker-empty">No host profiles exist yet - an admin creates one under Host Settings.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ginnungagap - Choose a server</title>
<link rel="icon" type="image/svg+xml" href="/ginnungagap-mark.svg">
<link rel="stylesheet" href="/styles.css?v=ginnungagap-55">
<style>
  body {{ display: block; padding: 0 16px; }}
  main.backend-picker {{ display: block; width: 100%; max-width: 640px; margin: 0 auto; padding: 56px 0 40px; }}
  .backend-picker-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }}
  .backend-picker-header img {{ width: 28px; height: 28px; }}
  .backend-picker-header h1 {{ font-size: 20px; font-weight: 600; margin: 0; flex: 1; }}
  .backend-picker-sub {{ color: #aaa; font-size: 13px; margin: 0 0 24px; }}
  .backend-tile {{
    display: flex; align-items: center; justify-content: space-between; gap: 20px;
    margin-bottom: 12px; padding: 16px 20px; border-radius: 10px;
    background: #2e2e2e; border: 1px solid #444;
  }}
  .backend-tile-info {{ min-width: 0; }}
  .backend-tile-info strong {{ display: block; font-size: 15px; font-weight: 600; overflow-wrap: anywhere; }}
  .backend-tile-info span {{ display: block; color: #aaa; font-size: 13px; margin-top: 3px; }}
  .backend-tile-info .backend-presence {{ color: #9ccc65; }}
  .backend-tile-actions {{ display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 8px; flex-shrink: 0; max-width: 60%; }}
  .backend-tile-actions form {{ margin: 0; }}
  .backend-tile button {{
    padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: 500; white-space: nowrap;
    background: #7cb342; color: #1c1c1c; border: 1px solid #7cb342;
  }}
  .backend-tile button[data-deploy-profile] {{ background: transparent; color: #ddd; border-color: #666; }}
  .backend-tile button:disabled {{ cursor: wait; opacity: .6; }}
  .backend-tile-actions [data-deploy-status] {{ flex-basis: 100%; text-align: right; color: #aaa; font-size: 12px; }}
  .backend-tile-actions [data-deploy-status]:empty {{ display: none; }}
  .backend-picker-empty {{ color: #aaa; }}
  @media (max-width: 520px) {{
    .backend-tile {{ flex-direction: column; align-items: stretch; }}
    .backend-tile-actions {{ max-width: none; justify-content: flex-start; }}
  }}
</style>
</head>
<body>
<main class="backend-picker">
<header class="backend-picker-header">
  <img src="/ginnungagap-mark.svg" alt="">
  <h1>Choose which server to work on</h1>
  <form action="/logout" method="post">
    <button type="submit" class="toolbar-btn">Log out</button>
  </form>
</header>
<p class="backend-picker-sub">Signed in as {escape(username)}. Each server runs its own session.</p>
{tiles}
</main>
<script src="/gateway/host-deploy.js?v=1" defer></script>
</body>
</html>"""


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str
    role: str
    allowed_hosts: AllowedHosts = "ALL"


class SetRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str


class SetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str


class SetAllowedHostsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: AllowedHosts


class BulkCreateUsersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: str
    role: str
    allowed_hosts: AllowedHosts = "ALL"


class AiConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class CreateHostConfig(ConnectionSettingsUpdate):
    filename: str


def register(app: FastAPI) -> None:
    auth_routes.register(app, logout_cookies=(BACKEND_COOKIE,))
    deploy_jobs: dict[str, dict[str, Any]] = {}
    deploy_lock = Lock()

    @app.get("/gateway/health")
    def gateway_health() -> dict[str, str]:
        """
        The gateway process's own liveness - deliberately NOT "/health":
        that path is an explicit route too, which would otherwise always
        win over app/proxy.py's catch-all mount and silently stop the
        real, per-backend /health (gns3_vm_ip, warning, ...) the
        frontend's own JS depends on from ever reaching a backend at
        all - found by actually running both processes together, not by
        a unit test (see deploy/README.md's own verification section).
        """
        return {"status": "ok"}

    @app.get("/pick-backend", response_class=HTMLResponse)
    def pick_backend(user: AnyUser) -> str:
        return _pick_backend_page(user)

    # --- User accounts (app/users.py) -----------------------------------

    @app.get("/admin/users")
    def list_users(_admin: AdminUser) -> list[dict[str, Any]]:
        return users.list_users()

    @app.post("/admin/users", status_code=201)
    def create_user(body: CreateUserRequest, _admin: AdminUser) -> dict[str, Any]:
        try:
            created = users.create_user(
                body.username, body.password, body.role, body.allowed_hosts
            )
        except ValueError as exc:
            raise _user_error(exc) from exc
        audit_log.record("user_created", _admin[0], body.username)
        return created

    @app.post("/admin/users/bulk")
    def create_users_bulk(
        body: BulkCreateUsersRequest, _admin: AdminUser
    ) -> list[dict[str, Any]]:
        try:
            results = users.create_users_bulk(body.lines, body.role, body.allowed_hosts)
        except ValueError as exc:
            raise _user_error(exc) from exc
        for result in results:
            if result["status"] == "created":
                audit_log.record("user_created", _admin[0], result["username"])
        return results

    @app.delete("/admin/users/{username}")
    def delete_user(username: str, _admin: AdminUser) -> dict[str, str]:
        try:
            users.delete_user(username)
        except (ValueError, FileNotFoundError) as exc:
            raise _user_error(exc) from exc
        audit_log.record("user_deleted", _admin[0], username)
        return {"status": "ok"}

    @app.put("/admin/users/{username}/role")
    def set_user_role(
        username: str, body: SetRoleRequest, _admin: AdminUser
    ) -> dict[str, Any]:
        try:
            updated = users.set_role(username, body.role)
        except (ValueError, FileNotFoundError) as exc:
            raise _user_error(exc) from exc
        audit_log.record("user_role_changed", _admin[0], f"{username} -> {body.role}")
        return updated

    @app.put("/admin/users/{username}/password")
    def set_user_password(
        username: str, body: SetPasswordRequest, _admin: AdminUser
    ) -> dict[str, str]:
        try:
            users.set_password(username, body.password)
        except (ValueError, FileNotFoundError) as exc:
            raise _user_error(exc) from exc
        audit_log.record("user_password_changed", _admin[0], username)
        return {"status": "ok"}

    @app.put("/admin/users/{username}/allowed-hosts")
    def set_user_allowed_hosts(
        username: str, body: SetAllowedHostsRequest, _admin: AdminUser
    ) -> dict[str, Any]:
        try:
            updated = users.set_allowed_hosts(username, body.allowed_hosts)
        except (ValueError, FileNotFoundError) as exc:
            raise _user_error(exc) from exc
        audit_log.record("user_allowed_hosts_changed", _admin[0], username)
        return updated

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
        audit_log.record(
            "ai_snippet_topics_changed", _admin[0], f"enabled={body.enabled}"
        )
        return get_ai_config(_admin)

    @app.get("/admin/audit-log")
    def get_audit_log(_admin: AdminUser) -> list[dict[str, Any]]:
        return audit_log.recent()

    # --- Settings profiles, connections, host-config, OVA templates -----

    @app.get("/settings-profiles")
    def list_settings_profiles(request: Request, user: AnyUser) -> list[dict[str, Any]]:
        username, role = user
        record = users.get_user(username) or {"role": role, "allowed_hosts": "ALL"}
        selected = selected_backend(request.scope)
        return [
            {**profile, "active": profile["filename"] == selected}
            for profile in settings_profiles.list_profiles()
            if users.can_access_host(record, profile["filename"])
        ]

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
        with deploy_lock:
            if deploy_jobs.get(filename, {}).get("status") == "running":
                raise HTTPException(409, "The GNS3 VM deploy check is in progress.")
            settings_call(settings_profiles.delete_profile, filename)
            settings_call(profile_secrets.delete, filename)
            backend_ports.forget_port(filename)
            deploy_jobs.pop(filename, None)
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

    # --- Provision, activate or restart a backend ------------------------

    def provision_gns3_vm(filename: str, job_id: str) -> None:
        try:
            port = backend_manager.ensure_running(filename)
            response = backend_manager.wait_for_health(port, timeout_seconds=900)
            if response is None:
                result = {
                    "status": "error",
                    "detail": f"{filename} did not answer healthy in time.",
                }
            else:
                health = response.json()
                if not isinstance(health, dict) or "gns3_vm_ip" not in health:
                    raise ValueError("Invalid backend health response")
                result = {
                    "status": "ready" if health["gns3_vm_ip"] else "unavailable",
                    "gns3_vm_ip": health["gns3_vm_ip"],
                    "gns3_vm": health.get("gns3_vm"),
                    "warning": health.get("warning"),
                }
        except backend_ports.NoFreePort as exc:
            result = {"status": "error", "detail": str(exc)}
        except Exception:
            logger.exception(f"Could not check the GNS3 VM on {filename}")
            result = {"status": "error", "detail": "Could not check the backend."}
        with deploy_lock:
            deploy_jobs[filename] = {"job_id": job_id, **result}

    @app.post("/settings-profiles/{filename}/deploy-gns3-vm", status_code=202)
    def start_profile_gns3_vm_deploy(
        filename: str, tasks: BackgroundTasks, _admin: AdminUser
    ) -> dict[str, str]:
        with deploy_lock:
            fields = settings_call(connection_settings.read_document, filename)
            job = deploy_jobs.get(filename)
            if job and job["status"] == "running":
                return {"job_id": job["job_id"]}
            esxi = fields.get("esxi") or {}
            if not (
                esxi.get("gns3_vm_template_id") or esxi.get("gns3_vm_ova_path")
            ) and not (
                backend_ports.get_port(filename) and backend_manager.is_active(filename)
            ):
                raise HTTPException(
                    409, "Select a GNS3 VM template in this host's settings first."
                )
            job_id = uuid4().hex
            deploy_jobs[filename] = {"job_id": job_id, "status": "running"}
            tasks.add_task(provision_gns3_vm, filename, job_id)
            return {"job_id": job_id}

    @app.get("/settings-profiles/{filename}/deploy-gns3-vm/{job_id}")
    def profile_gns3_vm_deploy_status(
        filename: str, job_id: str, _admin: AdminUser
    ) -> dict[str, Any]:
        with deploy_lock:
            job = deploy_jobs.get(filename)
        if not job or job["job_id"] != job_id:
            raise HTTPException(404, "No such GNS3 VM deploy job.")
        if job["status"] == "error":
            raise HTTPException(503, job["detail"])
        return job

    @app.post("/settings-profiles/{filename}/activate")
    def activate_settings_profile(
        filename: str, request: Request, user: AnyUser
    ) -> Response:
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
        username, role = user
        record = users.get_user(username) or {"role": role, "allowed_hosts": "ALL"}
        if not users.can_access_host(record, filename):
            raise HTTPException(403, f"Not allowed to use host {filename!r}")
        try:
            port = backend_manager.ensure_running(filename)
        except backend_ports.NoFreePort as exc:
            raise HTTPException(503, str(exc)) from exc
        if not backend_manager.wait_healthy(port):
            raise HTTPException(
                503, f"{filename} started but didn't answer healthy in time"
            )
        audit_log.record("host_activate", username, filename)
        response = (
            RedirectResponse("/", status_code=303)
            if error_pages.wants_html(request)
            else JSONResponse({"status": "active"})
        )
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
        audit_log.record("host_restart", _admin[0], filename)
        return {"status": "restarting"}
