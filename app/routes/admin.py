"""Admin-only user accounts, AI configuration policy and log summaries."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app import ai_snippets, log_summary, users
from app.application import app
from app.auth import require_admin

AdminUser = Annotated[tuple[str, str], Depends(require_admin)]

AllowedHosts = str | list[str]


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


def _user_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc))
    return HTTPException(400, str(exc))


@app.get("/admin/users")
def list_users(_admin: AdminUser) -> list[dict[str, Any]]:
    return users.list_users()


@app.post("/admin/users", status_code=201)
def create_user(body: CreateUserRequest, _admin: AdminUser) -> dict[str, Any]:
    try:
        return users.create_user(
            body.username, body.password, body.role, body.allowed_hosts
        )
    except ValueError as exc:
        raise _user_error(exc) from exc


@app.post("/admin/users/bulk")
def create_users_bulk(
    body: BulkCreateUsersRequest, _admin: AdminUser
) -> list[dict[str, Any]]:
    try:
        return users.create_users_bulk(body.lines, body.role, body.allowed_hosts)
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


@app.put("/admin/users/{username}/allowed-hosts")
def set_user_allowed_hosts(
    username: str, body: SetAllowedHostsRequest, _admin: AdminUser
) -> dict[str, Any]:
    try:
        return users.set_allowed_hosts(username, body.allowed_hosts)
    except (ValueError, FileNotFoundError) as exc:
        raise _user_error(exc) from exc


@app.get("/admin/ai-config")
def get_ai_config(_admin: AdminUser) -> dict[str, bool]:
    return {"enabled": ai_snippets.get_enabled(), "available": ai_snippets.available()}


@app.put("/admin/ai-config")
def set_ai_config(body: AiConfigRequest, _admin: AdminUser) -> dict[str, bool]:
    ai_snippets.set_enabled(body.enabled)
    return get_ai_config(_admin)


@app.post("/admin/logs/summarize")
def summarize_logs(_admin: AdminUser) -> dict[str, str]:
    try:
        return {"summary": log_summary.summarize(log_summary.recent_log_text())}
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
