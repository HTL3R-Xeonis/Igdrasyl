"""Admin-only: user accounts and the AI snippet topic allow-list. See app/users.py/app/ai_snippets.py."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app import ai_snippets, log_summary, users
from app.application import app
from app.auth import require_admin

AdminUser = Annotated[tuple[str, str], Depends(require_admin)]


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


class AiSnippetTopicsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: list[str]


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


@app.get("/admin/ai-snippet-topics")
def get_ai_snippet_topics(_admin: AdminUser) -> dict[str, Any]:
    return {"topics": ai_snippets.TOPICS, "enabled": ai_snippets.get_stored_topics()}


@app.put("/admin/ai-snippet-topics")
def set_ai_snippet_topics(
    body: AiSnippetTopicsRequest, _admin: AdminUser
) -> dict[str, Any]:
    return {"enabled": ai_snippets.set_stored_topics(body.topics)}


@app.post("/admin/logs/summarize")
def summarize_logs(_admin: AdminUser) -> dict[str, str]:
    try:
        return {"summary": log_summary.summarize(log_summary.recent_log_text())}
    except TimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
