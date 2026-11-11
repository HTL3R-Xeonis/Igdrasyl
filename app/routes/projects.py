"""Projects: export/import of bundles, the project library, hold/resume, device configs, deploy."""

from __future__ import annotations

from typing import Any

import requests
from fastapi import HTTPException, Response, UploadFile
from loguru import logger
from pydantic import BaseModel
from src.settings import Settings

from app import (
    holds,
    import_jobs,
    project_bundle,
    project_library,
    project_selection,
    public_text,
    push_jobs,
)
from app.application import app
from app.config_push import DeviceSpec
from app.gns3_state import (
    reload_project,
)
from app.routes.common import (
    LIVE_WRITE_ERRORS,
    settings_call,
)
from app.startup import (
    active_host_filename,
)


@app.get("/project/export")
def export_project() -> Response:
    """
    Downloads the current topology as a `.topoproj` bundle (a plain
    zip - topology.yaml, a sanitized settings.yml, configs.json from
    the last config-push, drawings.json, layout.json (node canvas
    positions), labels.json (moved port/name labels), and a manifest) - see app/project_bundle.py.
    """
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    data = project_bundle.build_export(app)
    filename = f"{app.state.project_name or 'topology'}.topoproj"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/project/reload-gns3")
def reload_gns3_project() -> dict[str, str]:
    """
    Closes and reopens the live GNS3 project - refreshes GNS3's own
    in-memory state for it (e.g. after changes made directly in GNS3's
    web UI) without redeploying anything. Doesn't touch ESXi VMs'
    actual power state, but does briefly stop every GNS3-native node
    (GNS3 itself stops everything on close) - restarted automatically
    once the project reopens, so the net effect is a refresh, not a
    lasting shutdown. See gns3_state.reload_project's own docstring for
    the real bug this fixes.
    """
    if app.state.gns3_vm_ip is None or app.state.project_id is None:
        raise HTTPException(
            503, app.state.startup_warning or "no live GNS3 project resolved"
        )
    try:
        reload_project(app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id)
    except (requests.RequestException, TimeoutError) as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.post("/project/import")
async def import_project(file: UploadFile) -> dict[str, Any]:
    """
    Kicks off a `.topoproj` bundle's live deploy as a background job and
    returns immediately - the deploy itself (incrementally, same as
    POST /nodes/POST /links: reuses anything already live by name, never
    deletes anything not in the bundle, unless the bundle is a full
    non-incremental one) can take minutes, so the caller polls GET
    /project/import/{job_id} for progress and the final result instead
    of blocking on one long request. See app/import_jobs.py for why the
    reported progress is an approximation, not a real callback from the
    deploy itself.
    """
    if not Settings.TOPOLOGY_FILE:
        raise HTTPException(
            409, "Select a project for this host before importing a deployment"
        )
    data = await file.read()
    job = import_jobs.start_job(app, data)
    return {"job_id": job.id, "total": job.total}


@app.post("/project/import/preview")
async def preview_project_import(file: UploadFile) -> dict[str, Any]:
    """
    What importing this bundle would take away, asked before the import starts. On a host that deploys non-incrementally
    (`incremental_deploy: false`) the import removes every device of the current project that the bundle does not contain
    - and the VMs this tool created earlier - so the page names them in its confirmation. Changes nothing.
    """
    names = import_jobs.peek_target_node_names(await file.read())
    graph = app.state.graph
    current = sorted(graph.nodes) if graph is not None else []
    return {
        "incremental": bool(Settings.INCREMENTAL_DEPLOY),
        "bundle_nodes": sorted(names),
        "removed": []
        if Settings.INCREMENTAL_DEPLOY
        else [name for name in current if name not in names],
    }


@app.get("/project/import/{job_id}")
def import_job_status(job_id: str) -> dict[str, Any]:
    """
    Poll target for POST /project/import's background job.
    "status" is "running", "done", or "error". While running: "total"
    and "completed" (see app/import_jobs.py's own docstring on how
    "completed" is derived). Once "done": also carries "manifest",
    "configs", "drawings", "auto_config_push", and - when the bundle's
    own manifest.json enabled it - "push_results", exactly the same
    shape the old synchronous endpoint used to return directly. Once
    "error": also carries "error" (message) and "error_kind"
    ("bad_bundle" for a malformed/mistargeted bundle, mirroring the old
    400; "deploy_failed" for a live deploy failure, mirroring the old
    502) - the real topology file is left untouched on either.
    """
    jobs: dict[str, import_jobs.ImportJob] = getattr(app.state, "import_jobs", {})
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no import job {job_id!r}")

    completed = import_jobs.poll_progress(app, job)
    response: dict[str, Any] = {
        "status": job.status,
        "total": job.total,
        "completed": completed,
        "elapsed_seconds": job.elapsed_seconds,
        "phase": job.phase,
        "pending": job.pending if job.status == "running" else [],
    }
    if job.status == "error":
        response["error"] = public_text.redact_paths(job.error)
        response["error_kind"] = job.error_kind
    elif job.status == "done" and job.result is not None:
        response.update(job.result)
    return response


class CreateLibraryProject(BaseModel):
    name: str
    topology_yaml: str | None = None


@app.get("/projects")
def list_library_projects():
    projects = settings_call(project_library.list_projects)
    try:
        held = holds.all_entries()
    except holds.HoldError:
        held = {}
    return {
        "projects": [{**project, "held": project["id"] in held} for project in projects]
    }


@app.delete("/projects/{project_id}")
def delete_library_project(project_id: str) -> dict[str, str]:
    """
    Removes a project from the library. Refused while a host has it selected or
    it is on hold; never touches GNS3 or ESXi - whatever was deployed stays.
    """
    settings_call(project_library.get, project_id)  # 404/400 first
    if holds.entry(project_id) is not None:
        raise HTTPException(
            409, "This project is on hold - resume it first, then delete it."
        )
    hosts = project_selection.hosts_using(project_id)
    if hosts:
        raise HTTPException(
            409,
            "This project is selected for host(s) "
            + ", ".join(hosts)
            + " - select another project there first.",
        )
    settings_call(project_library.delete, project_id)
    if getattr(app.state, "design_project_id", None) == project_id:
        app.state.design_project_id = None
    logger.info(f"Deleted library project {project_id}")
    return {"status": "deleted"}


def _hold_error(exc: Exception) -> HTTPException:
    if isinstance(exc, holds.HoldError):
        return HTTPException(409, str(exc))
    return HTTPException(502, public_text.redact_paths(str(exc)))


@app.post("/project/hold")
def hold_active_project() -> dict[str, Any]:
    """
    Parks the active host's live project (see app/holds.py): VMs off and
    protected, GNS3 project closed, everything kept for a later resume.
    """
    filename = active_host_filename()
    selection = (project_selection.get_selection(filename) if filename else {}) or {}
    project_id = selection.get("project_id")
    if not filename or not project_id:
        raise HTTPException(
            409, "Only a library project selected for the active host can be held."
        )
    try:
        return {"hold": holds.hold_project(app, project_id, filename)}
    except (holds.HoldError, *LIVE_WRITE_ERRORS) as exc:
        raise _hold_error(exc) from exc


@app.post("/projects/{project_id}/resume")
def resume_held_project(project_id: str) -> dict[str, Any]:
    try:
        report = holds.resume_project(app, project_id)
    except (holds.HoldError, *LIVE_WRITE_ERRORS) as exc:
        raise _hold_error(exc) from exc
    response: dict[str, Any] = {"status": "resumed", "report": report}
    # A project that wants its configs pushed after a deploy wants them after a
    # resume too (the VMs/nodes were off, and a router may have lost its running config).
    try:
        saved = project_library.get_configs(project_id)
    except (ValueError, FileNotFoundError):
        saved = {"devices": {}, "auto_push": False}
    if saved["auto_push"] and saved["devices"]:
        try:
            # In the background (it can be long, and devices may still be booting):
            # the client follows it through GET /config-push/jobs/{id}.
            job = push_jobs.start_job(
                app, saved["devices"], audit_note="Automatic push after resume"
            )
            response["push_job_id"] = job.id
        except push_jobs.AlreadyRunning:
            response["push_job_error"] = "another config push is still running"
    return response


@app.post("/projects", status_code=201)
def create_library_project(body: CreateLibraryProject):
    return settings_call(project_library.create, body.name, body.topology_yaml)


class ProjectConfigs(BaseModel):
    devices: dict[str, list[str] | DeviceSpec] = {}
    auto_push: bool = False


@app.get("/projects/{project_id}/configs")
def get_library_project_configs(project_id: str) -> dict[str, Any]:
    """The device command lists saved with a library project (see project_library.save_configs)."""
    return settings_call(project_library.get_configs, project_id)


@app.put("/projects/{project_id}/configs")
def put_library_project_configs(
    project_id: str, body: ProjectConfigs
) -> dict[str, Any]:
    devices = {
        name: entry.model_dump(exclude_none=True)
        if isinstance(entry, DeviceSpec)
        else entry
        for name, entry in body.devices.items()
    }
    return settings_call(
        project_library.save_configs, project_id, devices, body.auto_push
    )


def _active_project_id() -> str:
    filename = active_host_filename()
    selection = (project_selection.get_selection(filename) if filename else {}) or {}
    if not selection.get("project_id"):
        raise HTTPException(409, "Select a library project for the active host first.")
    return selection["project_id"]


@app.get("/project/configs")
def get_active_project_configs() -> dict[str, Any]:
    """Same as GET /projects/{id}/configs, for the project the active host has selected."""
    return get_library_project_configs(_active_project_id())


@app.put("/project/configs")
def put_active_project_configs(body: ProjectConfigs) -> dict[str, Any]:
    return put_library_project_configs(_active_project_id(), body)


@app.get("/projects/{project_id}/download")
def download_library_project(project_id: str):
    path = settings_call(project_library.topology_path, project_id)
    return Response(
        path.read_bytes(),
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="topology.yaml"'},
    )


@app.get("/projects/{project_id}/deploy-preview/{filename}")
def preview_library_deployment(project_id: str, filename: str):
    preview = settings_call(project_library.deployment_preview, project_id, filename)
    return {**preview, "held": holds.entry(project_id) is not None}


@app.post("/projects/{project_id}/deploy/{filename}")
def deploy_library_project(project_id: str, filename: str):
    preview = settings_call(project_library.deployment_preview, project_id, filename)
    if not preview["host"]["active"]:
        raise HTTPException(409, "Activate this host before deploying")
    if not preview["selected"]:
        raise HTTPException(
            409, "Select this project for the active host and restart before deploying"
        )
    if holds.entry(project_id) is not None:
        raise HTTPException(
            409,
            "This project is on hold - resume it instead of deploying it again "
            "(its VMs and GNS3 project are kept).",
        )
    clash = holds.held_gns3_names(exclude_project_id=project_id).get(
        app.state.project_name
    )
    if clash is not None:
        raise HTTPException(
            409,
            "A project on hold already uses this GNS3 project name - rename one "
            "of them, or resume the held one.",
        )
    try:
        state = project_library.load_design(project_id)
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, f"Template service unavailable: {exc}") from exc
    saved = settings_call(project_library.get_configs, project_id)
    bundle = project_bundle.build_design_export(
        state,
        preview["project"]["name"],
        configs=saved["devices"],
        auto_config_push=saved["auto_push"],
    )
    job = import_jobs.start_job(app, bundle)
    return {"job_id": job.id, "total": job.total}
