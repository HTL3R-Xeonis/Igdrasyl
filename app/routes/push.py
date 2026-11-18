"""Config push endpoints (see app/config_push.py and app/push_jobs.py)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import ai_snippets, config_snippets, push_audit, push_jobs
from app.application import app
from app.auth import current_user
from app.config_push import DeviceResult, DeviceSpec, push_all
from app.routes.common import require_active_host


class PushRequest(BaseModel):
    devices: dict[str, list[str] | DeviceSpec]
    # How long a job keeps retrying devices that are still booting; omitted = 15 minutes, 0 = once.
    wait_seconds: int | None = Field(default=None, ge=0, le=3600)


class PushAiSnippet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_id: str = Field(pattern=r"^[a-f0-9]{32}$")


AiUser = Annotated[tuple[str, str], Depends(current_user)]


@app.get("/config-snippets/ai/status")
def ai_config_status(_user: AiUser) -> dict[str, bool]:
    return {"enabled": ai_snippets.get_enabled(), "available": ai_snippets.available()}


def _require_ai_config() -> None:
    if not ai_snippets.get_enabled():
        raise HTTPException(403, "An administrator must enable AI configuration.")
    if not ai_snippets.available():
        raise HTTPException(503, "The configuration generator is not configured.")


@app.get("/config-snippets/ai/targets")
def ai_snippet_targets(_user: AiUser) -> list[str]:
    _require_ai_config()
    return ai_snippets.targets(app)


@app.get("/config-snippets/ai/devices")
def ai_snippet_devices(_user: AiUser) -> list[dict]:
    _require_ai_config()
    return ai_snippets.devices(app)


@app.post("/config-snippets/ai/generate")
def generate_ai_snippet(request: ai_snippets.GenerateRequest, user: AiUser) -> dict:
    _require_ai_config()
    try:
        return ai_snippets.generate(app, request, user[0])
    except ai_snippets.GenerationDisabled as exc:
        raise HTTPException(403, str(exc)) from exc
    except ai_snippets.TargetChanged as exc:
        raise HTTPException(409, str(exc)) from exc
    except ai_snippets.Busy as exc:
        raise HTTPException(
            429, "The snippet generator is busy. Try again shortly."
        ) from exc
    except ai_snippets.InvalidDraft as exc:
        raise HTTPException(
            422, str(exc) or "Describe supported settings for the selected device."
        ) from exc
    except ai_snippets.GeneratorUnavailable as exc:
        raise HTTPException(502, "The snippet generator is unavailable.") from exc


@app.post("/config-snippets/ai/push", status_code=202)
def push_ai_snippet(request: PushAiSnippet, user: AiUser) -> dict[str, str]:
    require_active_host("AI configuration push")
    try:
        job = ai_snippets.push_draft(app, request.draft_id, user[0])
    except ai_snippets.GenerationDisabled as exc:
        raise HTTPException(403, str(exc)) from exc
    except ai_snippets.DraftExpired as exc:
        raise HTTPException(
            409, "The draft expired or was already used. Generate a new one."
        ) from exc
    except (ai_snippets.TargetChanged, push_jobs.AlreadyRunning) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job.id}


@app.get("/config-snippets")
def list_config_snippets() -> list[dict]:
    return config_snippets.list_snippets()


@app.get("/config-snippets/{name}")
def get_config_snippet(name: str) -> config_snippets.Snippet:
    try:
        return config_snippets.get(name)
    except config_snippets.InvalidSnippet as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"no config snippet {name!r}") from exc


@app.put("/config-snippets/{name}")
def save_config_snippet(
    name: str, request: config_snippets.Snippet
) -> config_snippets.Snippet:
    try:
        return config_snippets.save(name, request.label, request.commands)
    except config_snippets.InvalidSnippet as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/config-snippets/{name}", status_code=204)
def delete_config_snippet(name: str) -> None:
    try:
        config_snippets.delete(name)
    except config_snippets.InvalidSnippet as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, f"no config snippet {name!r}") from exc


@app.get("/config-push/audit")
def config_push_audit(
    limit: int = Query(default=push_audit.MAX_RECORDS, ge=1, le=push_audit.MAX_RECORDS),
) -> list[dict[str, Any]]:
    return push_audit.read(limit)


@app.post("/config-push/jobs", status_code=202)
def start_config_push_job(request: PushRequest) -> dict[str, Any]:
    """
    Starts a push in the background and returns at once - use this for anything long
    (up to 10 000 commands per device; see app/push_jobs.py). Poll
    GET /config-push/jobs/{job_id}.
    """
    require_active_host("configuration push job")
    try:
        job = push_jobs.start_job(
            app, request.devices, wait_seconds=request.wait_seconds
        )
    except push_jobs.AlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job.id, "total_commands": job.total_commands}


@app.get("/config-push/jobs/{job_id}")
def config_push_job_status(job_id: str) -> dict[str, Any]:
    """Progress per device while running; the per-device results once done."""
    job = push_jobs.get_job(app, job_id)
    if job is None:
        raise HTTPException(404, f"no config push job {job_id!r}")
    response = job.progress()
    if job.status == "done" and job.results is not None:
        response["results"] = [r.model_dump() for r in job.results]
    if job.status == "error":
        response["error"] = job.error
    return response


@app.post("/config-push")
def config_push(request: PushRequest) -> list[DeviceResult]:
    """
    Pushes each device's raw command list over its auto-selected
    transport (Netmiko for GNS3 console devices, guest-ops for ESXi
    VMs). Stops a device's remaining commands on its first failure, but
    always processes every device in the request - see app/config_push.py.
    Synchronous on purpose: FastAPI runs a plain `def` route in its
    threadpool, so this doesn't block the event loop the console
    WebSocket proxy also needs.
    """
    require_active_host("configuration push")
    app.state.last_config_push = request.devices
    with push_audit.track("Direct push") as audit:
        results = push_all(request.devices, app)
        audit.extend(results)
    return results
