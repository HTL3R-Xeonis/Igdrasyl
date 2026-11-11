"""
Live-edit staging: stage several node/link changes against the live
topology, review one diff, apply them all in one reviewed sequence -
see app/staging.py and app/batch_apply.py for why.
"""

from __future__ import annotations

from typing import Any

import requests
from fastapi import HTTPException
from pydantic import BaseModel

from app import batch_apply, design, staging, template_health
from app.application import app
from app.routes.models import AddLinkRequest, AddNodeRequest


def _require_staging() -> design.DesignState:
    try:
        return staging.get(app)
    except staging.NoActiveStaging as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/live/stage")
def get_staging() -> dict[str, Any]:
    return {"active": staging.is_active(app)}


@app.post("/live/stage")
def start_staging() -> dict[str, Any]:
    try:
        staging.start(app)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"status": "ok"}


@app.post("/live/stage/discard")
def discard_staging() -> dict[str, str]:
    staging.discard(app)
    return {"status": "ok"}


@app.get("/live/stage/topology")
def staged_topology() -> list[dict[str, Any]]:
    return design.to_topology_view(_require_staging())


@app.post("/live/stage/nodes")
def stage_add_node(request: AddNodeRequest) -> dict[str, str]:
    state = _require_staging()
    try:
        design.add_node(state, request.name, request.role, request.image)
    except ValueError as exc:
        raise HTTPException(400, template_health.explain(str(exc))) from exc
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, template_health.describe(exc)) from exc
    return {"status": "ok"}


@app.delete("/live/stage/nodes/{name}")
def stage_remove_node(name: str) -> dict[str, str]:
    state = _require_staging()
    try:
        design.remove_node(state, name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "ok"}


@app.post("/live/stage/links")
def stage_add_link(request: AddLinkRequest) -> dict[str, str]:
    state = _require_staging()
    try:
        design.add_link(state, request.node1, request.if1, request.node2, request.if2)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


@app.delete("/live/stage/links/{node1}/{node2}")
def stage_remove_link(
    node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> dict[str, str]:
    state = _require_staging()
    try:
        design.remove_link(state, node1, node2, if1, if2)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "ok"}


@app.get("/live/stage/diff")
def staging_diff() -> dict[str, Any]:
    state = _require_staging()
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    return batch_apply.compute_diff(state, app.state.graph).as_dict()


class ApplyConfirmation(BaseModel):
    confirm: bool = False


@app.post("/live/stage/apply")
def staging_apply(request: ApplyConfirmation) -> dict[str, Any]:
    """Applies the staged diff live, one change at a time; see batch_apply.apply_diff for ordering/failure handling."""
    if not request.confirm:
        raise HTTPException(400, 'Pass {"confirm": true} to apply the staged changes')
    state = _require_staging()
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    diff = batch_apply.compute_diff(state, app.state.graph)
    if diff.is_empty():
        staging.discard(app)
        return {"results": [], "applied": True}
    results = batch_apply.apply_diff(app, diff)
    applied = all(result["ok"] for result in results)
    if applied:
        staging.discard(app)
    return {"results": results, "applied": applied}
