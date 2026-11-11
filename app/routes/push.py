"""Config push endpoints (see app/config_push.py and app/push_jobs.py)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from app import config_snippets, push_audit, push_jobs
from app.application import app
from app.config_push import DeviceResult, DeviceSpec, push_all


class PushRequest(BaseModel):
    devices: dict[str, list[str] | DeviceSpec]
    # How long a job keeps retrying devices that are still booting; omitted = 15 minutes, 0 = once.
    wait_seconds: int | None = Field(default=None, ge=0, le=3600)


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
    app.state.last_config_push = request.devices
    with push_audit.track("Direct push") as audit:
        results = push_all(request.devices, app)
        audit.extend(results)
    return results
