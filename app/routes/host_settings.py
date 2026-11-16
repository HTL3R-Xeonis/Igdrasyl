"""Host settings: settings profiles, host configuration, connections, OVA templates."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict

from app import (
    connection_settings,
    ova_templates,
    profile_secrets,
    project_selection,
    settings_profiles,
    users,
)
from app.application import app
from app.auth import current_user, require_admin
from app.routes.common import (
    settings_call,
)

AdminUser = Annotated[tuple[str, str], Depends(require_admin)]
AnyUser = Annotated[tuple[str, str], Depends(current_user)]
from app.routes.models import (
    ConnectionSettingsUpdate,
)


@app.get("/settings-profiles")
def list_settings_profiles(user: AnyUser) -> list[dict[str, Any]]:
    """
    Every uploaded settings.yml profile (see app/settings_profiles.py) this
    caller is allowed to use, each with a path-free preview and whether it
    is active - admins always see every profile.
    """
    username, role = user
    record = users.get_user(username) or {"role": role, "allowed_hosts": "ALL"}
    return [
        profile
        for profile in settings_profiles.list_profiles()
        if users.can_access_host(record, profile["filename"])
    ]


@app.post("/settings-profiles")
async def upload_settings_profile(
    file: UploadFile, _admin: AdminUser
) -> dict[str, str]:
    """Saves an uploaded settings.yml under its own filename - doesn't activate it."""
    data = await file.read()
    try:
        settings_profiles.save_profile(file.filename, data)
    except settings_profiles.InvalidProfileName as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


class ProjectSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    project_name: str = ""


@app.get("/settings-profiles/{filename}/project")
def get_host_project(filename: str):
    return settings_call(project_selection.get_selection, filename)


@app.put("/settings-profiles/{filename}/project")
def select_host_project(filename: str, body: ProjectSelectionRequest):
    return settings_call(
        project_selection.save_selection,
        filename,
        body.project_name,
        body.project_id,
    )


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


class CreateHostConfig(ConnectionSettingsUpdate):
    filename: str


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
    return {
        "results": settings_call(
            connection_settings.test_connections,
            filename,
            body.fields,
            getattr(app.state, "gns3_vm_ip", None),
        )
    }


@app.delete("/settings-profiles/{filename}")
def delete_settings_profile(filename: str, _admin: AdminUser) -> dict[str, str]:
    try:
        settings_profiles.delete_profile(filename)
        profile_secrets.delete(filename)
    except settings_profiles.InvalidProfileName as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "ok"}


@app.post("/settings-profiles/{filename}/activate")
def activate_settings_profile(filename: str, _admin: AdminUser) -> dict[str, str]:
    """
    Restarts this same server process against the given profile - see
    settings_profiles.restart_into_profile's own docstring for why this
    is a real process re-exec (same PID), not an in-place config swap,
    and why every open console/screen connection drops when it happens.
    """
    try:
        settings_profiles.restart_into_profile(filename)
    except settings_profiles.InvalidProfileName as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "restarting"}
