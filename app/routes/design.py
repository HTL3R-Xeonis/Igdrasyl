"""Canvas annotations and the offline design canvas (see app/design.py): a never-deployed sketch."""

from __future__ import annotations

from typing import Annotated, Any

import requests
from fastapi import File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from app import (
    design,
    design_history,
    design_store,
    generate_jobs,
    label_layout,
    node_credentials,
    node_notes,
    project_bundle,
    project_library,
    public_text,
    template_health,
    topology_write,
)
from app.application import app
from app.routes.common import (
    settings_call,
)
from app.routes.models import (
    AddLinkRequest,
    AddNodeRequest,
    DrawingsRequest,
    LabelsUpdate,
    LayoutUpdate,
    NoteUpdate,
    PositionsUpdate,
    label_changes,
)

# --- Offline design canvas (app/design.py) - a never-deployed sketch
# topology, reachable from the landing page. Same shapes as the live
# endpoints above so the frontend's one canvas renderer covers both,
# but nothing here ever touches ESXi/GNS3 or Settings.TOPOLOGY_FILE.


@app.get("/design/templates")
def design_templates() -> dict[str, Any]:
    try:
        return topology_write.list_templates()
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, template_health.describe(exc)) from exc


@app.get("/design/topology")
def design_topology() -> list[dict[str, Any]]:
    return design.to_topology_view(app.state.design)


@app.post("/design/nodes")
def design_add_node(request: AddNodeRequest) -> dict[str, str]:
    try:
        with design_history.step(app):
            design.add_node(app.state.design, request.name, request.role, request.image)
    except ValueError as exc:
        raise HTTPException(400, template_health.explain(str(exc))) from exc
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        # Environment.get_environment couldn't reach the template
        # service - same degraded-mode story as GET /templates above,
        # not a 500: the sketch itself is untouched, only classifying a
        # brand new node needs that service to be up.
        raise HTTPException(503, template_health.describe(exc)) from exc
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.delete("/design/nodes/{name}")
def design_remove_node(name: str) -> dict[str, str]:
    try:
        with design_history.step(app):
            design.remove_node(app.state.design, name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.post("/design/links")
def design_add_link(request: AddLinkRequest) -> dict[str, str]:
    try:
        with design_history.step(app):
            design.add_link(
                app.state.design,
                request.node1,
                request.if1,
                request.node2,
                request.if2,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.delete("/design/links/{node1}/{node2}")
def design_remove_link(
    node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> dict[str, str]:
    try:
        with design_history.step(app):
            design.remove_link(app.state.design, node1, node2, if1, if2)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.get("/design/layout")
def design_layout() -> dict[str, dict[str, float]]:
    return design.layout_with_defaults(app.state.design)


@app.post("/design/layout")
def design_update_layout(update: LayoutUpdate) -> dict[str, str]:
    if update.name not in app.state.design.nodes:
        raise HTTPException(404, f"no node named {update.name!r}")
    with design_history.step(app):
        app.state.design.layout[update.name] = {"x": update.x, "y": update.y}
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.post("/design/layout/batch")
def design_update_layout_batch(update: PositionsUpdate) -> dict[str, str]:
    """Moves several nodes at once - one undo step for the whole group move."""
    unknown = [n for n in update.positions if n not in app.state.design.nodes]
    if unknown:
        raise HTTPException(404, f"no node named {unknown[0]!r}")
    with design_history.step(app):
        for name, pos in update.positions.items():
            app.state.design.layout[name] = {"x": pos.x, "y": pos.y}
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.get("/design/labels")
def design_labels() -> dict[str, dict[str, float]]:
    return app.state.design.labels


@app.post("/design/labels")
def design_update_labels(update: LabelsUpdate) -> dict[str, dict[str, float]]:
    changes = label_changes(update, app.state.design.nodes)
    try:
        with design_history.step(app):
            label_layout.apply_changes(app.state.design.labels, changes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    design_store.save(app.state.design)
    return app.state.design.labels


@app.get("/design/notes")
def design_notes() -> dict[str, str]:
    return app.state.design.notes


@app.put("/design/notes/{name}")
def design_update_note(name: str, update: NoteUpdate) -> dict[str, str]:
    if name not in app.state.design.nodes:
        raise HTTPException(404, f"no node named {name!r}")
    with design_history.step(app):
        node_notes.apply_note(app.state.design.notes, name, update.text)
    design_store.save(app.state.design)
    return app.state.design.notes


@app.get("/design/nodes/{name}/credentials")
def design_node_default_credentials(name: str) -> dict[str, Any]:
    node = app.state.design.nodes.get(name)
    if node is None:
        raise HTTPException(404, f"no node named {name!r}")
    return node_credentials.for_image(node.image, node.environment)


class DesignDelete(BaseModel):
    nodes: list[str] = []
    links: list[tuple[str, str]] = []


@app.post("/design/delete")
def design_delete(request: DesignDelete) -> dict[str, str]:
    """Deletes a selection (nodes and/or links) as one undo step; unknown names change nothing."""
    try:
        with design_history.step(app):
            design.remove_many(app.state.design, request.nodes, request.links)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    design_store.save(app.state.design)
    return {"status": "ok"}


class PastedNode(BaseModel):
    name: str
    role: str
    image: str
    x: float
    y: float


class DesignPaste(BaseModel):
    nodes: list[PastedNode]
    links: list[AddLinkRequest] = []


@app.post("/design/paste")
def design_paste(request: DesignPaste) -> dict[str, str]:
    """Adds copied nodes and the links between them as one undo step; all or nothing."""
    try:
        with design_history.step(app):
            design.paste(
                app.state.design,
                [node.model_dump() for node in request.nodes],
                [(l.node1, l.if1, l.node2, l.if2) for l in request.links],
            )
    except ValueError as exc:
        raise HTTPException(400, template_health.explain(str(exc))) from exc
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, str(exc)) from exc
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.get("/design/history")
def design_history_status() -> dict[str, bool]:
    return design_history.status(app)


@app.post("/design/undo")
def design_undo() -> dict[str, bool]:
    if not design_history.undo(app):
        raise HTTPException(409, "nothing to undo")
    return design_history.status(app)


@app.post("/design/redo")
def design_redo() -> dict[str, bool]:
    if not design_history.redo(app):
        raise HTTPException(409, "nothing to redo")
    return design_history.status(app)


@app.get("/design/drawings")
def design_get_drawings() -> list[dict[str, Any]]:
    return app.state.design.drawings


@app.post("/design/drawings")
def design_set_drawings(request: DrawingsRequest) -> dict[str, str]:
    with design_history.step(app):
        app.state.design.drawings = request.drawings
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.post("/design/reset")
def design_reset() -> dict[str, str]:
    """Clears the sketch and starts a fresh, empty one."""
    with design_history.step(app):
        app.state.design = design.new_state()
    app.state.design_project_id = None
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.get("/design/project")
def current_design_project() -> dict[str, Any]:
    project_id = getattr(app.state, "design_project_id", None)
    return {
        "project": settings_call(project_library.get, project_id)
        if project_id
        else None
    }


@app.post("/design/project/{project_id}/open")
def open_library_project(project_id: str) -> dict[str, Any]:
    try:
        state = settings_call(project_library.load_design, project_id)
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, f"Template service unavailable: {exc}") from exc
    with design_history.step(app):
        app.state.design = state
    app.state.design_project_id = project_id
    design_store.save(state)
    return {"project": settings_call(project_library.get, project_id)}


class SaveDesignAs(BaseModel):
    name: str


@app.post("/design/project", status_code=201)
def create_library_project_from_design(request: SaveDesignAs) -> dict[str, Any]:
    """Saves the current sketch as a NEW library project under the given name."""
    project = settings_call(
        project_library.create_from_design, request.name, app.state.design
    )
    app.state.design_project_id = project["id"]
    return {"project": project}


@app.put("/design/project/{project_id}")
def save_library_project(project_id: str) -> dict[str, Any]:
    project = settings_call(project_library.save_design, project_id, app.state.design)
    app.state.design_project_id = project_id
    design_store.save(app.state.design)
    return {"project": project}


@app.get("/design/export")
def design_export() -> Response:
    """
    Downloads the sketch as a `.topoproj` bundle carrying no ESXi/GNS3
    target - importable later against whichever host is active via the
    existing POST /project/import.
    """
    data = project_bundle.build_design_export(app.state.design, "design")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="design.topoproj"'},
    )


class GenerateRequest(BaseModel):
    prompt: str


@app.post("/design/generate")
def generate_design(request: GenerateRequest) -> dict[str, str]:
    """
    Kicks off a background job that asks the Topology Generator API (an
    LLM-backed service, see src.connections.topology_generator_client)
    for a topology matching the given natural-language prompt, retrying
    on an invalid result the same way TopologyBuilder's own `generate`
    CLI command does - and on success, REPLACES the current design
    sketch with the generated one (see app/generate_jobs.py). Poll
    GET /design/generate/{job_id} for progress.
    """
    if not request.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    job = generate_jobs.start_job(app, request.prompt)
    return {"job_id": job.id}


MAX_GENERATE_ATTACHMENT_COUNT = 5
MAX_GENERATE_ATTACHMENT_BYTES = 10 * 1024 * 1024


@app.post("/design/generate/attachments")
async def generate_design_from_attachments(
    files: Annotated[list[UploadFile], File()],
    prompt: Annotated[str, Form()] = "",
    ollama_only: Annotated[bool, Form()] = False,
    match_screenshot_layout: Annotated[bool, Form()] = False,
) -> dict[str, str]:
    if not 1 <= len(files) <= MAX_GENERATE_ATTACHMENT_COUNT:
        raise HTTPException(400, "Choose between 1 and 5 files.")

    attachments = []
    remaining = MAX_GENERATE_ATTACHMENT_BYTES
    for upload in files:
        content = await upload.read(remaining + 1)
        remaining -= len(content)
        if remaining < 0:
            raise HTTPException(413, "Attachments may total at most 10 MiB.")
        if not content:
            raise HTTPException(400, "Attachments must not be empty.")
        attachments.append((upload.filename or "attachment", content))

    job = generate_jobs.start_job(
        app,
        prompt,
        attachments=attachments,
        ollama_only=ollama_only,
        match_screenshot_layout=match_screenshot_layout,
    )
    return {"job_id": job.id}


@app.get("/design/generate/{job_id}")
def generate_design_status(job_id: str) -> dict[str, Any]:
    """
    Poll target for POST /design/generate's background job. "status" is
    "running", "done", or "error". While running: "attempt"/
    "max_attempts" and any "warnings" the most recent attempt reported.
    Once "error": "error" (message).
    """
    jobs: dict[str, generate_jobs.GenerateJob] = getattr(app.state, "generate_jobs", {})
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no generate job {job_id!r}")

    response: dict[str, Any] = {
        "status": job.status,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "warnings": job.warnings,
    }
    if job.status == "error":
        response["error"] = public_text.redact_paths(job.error)
    return response
