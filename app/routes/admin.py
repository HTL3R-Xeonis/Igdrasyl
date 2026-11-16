"""Admin-only user accounts, AI configuration policy and log summaries."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app import ai_snippets, audit_log, log_summary, users
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


@app.get("/admin/ai-config")
def get_ai_config(_admin: AdminUser) -> dict[str, bool]:
    return {"enabled": ai_snippets.get_enabled(), "available": ai_snippets.available()}


@app.put("/admin/ai-config")
def set_ai_config(body: AiConfigRequest, _admin: AdminUser) -> dict[str, bool]:
    ai_snippets.set_enabled(body.enabled)
    audit_log.record("ai_snippet_topics_changed", _admin[0], f"enabled={body.enabled}")
    return get_ai_config(_admin)


@app.get("/admin/audit-log")
def get_audit_log(_admin: AdminUser) -> list[dict[str, Any]]:
    return audit_log.recent()


@app.post("/admin/logs/summarize")
def summarize_logs(_admin: AdminUser) -> dict[str, str]:
    try:
        return {"summary": log_summary.summarize(log_summary.recent_log_text())}
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
