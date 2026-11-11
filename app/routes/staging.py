"""Review and apply staged live edits against the topology they came from."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import requests
from fastapi import HTTPException
from pydantic import BaseModel

from app import batch_apply, design, staging, template_health
from app.application import app
from app.live_lock import exclusive
from app.routes.models import AddLinkRequest, AddNodeRequest


@contextmanager
def _staging_session(*, current: bool = False) -> Iterator[staging.Session]:
    with exclusive("Live staging"):
        try:
            session = staging.get(app)
            if current:
                staging.require_current(app, session)
        except (staging.NoActiveStaging, staging.StaleStaging) as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        yield session


@app.get("/live/stage")
def get_staging() -> dict[str, Any]:
    return {"active": staging.is_active(app)}


@app.post("/live/stage")
def start_staging() -> dict[str, Any]:
    with exclusive("Start live staging"):
        try:
            staging.start(app)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    return {"status": "ok"}


@app.post("/live/stage/discard")
def discard_staging() -> dict[str, str]:
    with exclusive("Discard live staging"):
        staging.discard(app)
    return {"status": "ok"}


@app.get("/live/stage/topology")
def staged_topology() -> list[dict[str, Any]]:
    with _staging_session() as session:
        return design.to_topology_view(session.draft)


@app.post("/live/stage/nodes")
def stage_add_node(request: AddNodeRequest) -> dict[str, str]:
    with _staging_session() as session:
        try:
            design.add_node(session.draft, request.name, request.role, request.image)
        except ValueError as exc:
            raise HTTPException(400, template_health.explain(str(exc))) from exc
        except (
            TimeoutError,
            RuntimeError,
            requests.exceptions.RequestException,
        ) as exc:
            raise HTTPException(503, template_health.describe(exc)) from exc
        session.review_id = None
    return {"status": "ok"}


@app.delete("/live/stage/nodes/{name}")
def stage_remove_node(name: str) -> dict[str, str]:
    with _staging_session() as session:
        try:
            design.remove_node(session.draft, name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        session.review_id = None
    return {"status": "ok"}


@app.post("/live/stage/links")
def stage_add_link(request: AddLinkRequest) -> dict[str, str]:
    with _staging_session() as session:
        try:
            design.add_link(
                session.draft, request.node1, request.if1, request.node2, request.if2
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        session.review_id = None
    return {"status": "ok"}


@app.delete("/live/stage/links/{node1}/{node2}")
def stage_remove_link(
    node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> dict[str, str]:
    with _staging_session() as session:
        try:
            design.remove_link(session.draft, node1, node2, if1, if2)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        session.review_id = None
    return {"status": "ok"}


@app.get("/live/stage/diff")
def staging_diff() -> dict[str, Any]:
    with _staging_session(current=True) as session:
        diff = batch_apply.compute_diff(session.draft, app.state.graph)
        session.review_id = uuid4().hex
        return {**diff.as_dict(), "review_id": session.review_id}


class ApplyConfirmation(BaseModel):
    confirm: bool = False
    review_id: str | None = None


@app.post("/live/stage/apply")
def staging_apply(request: ApplyConfirmation) -> dict[str, Any]:
    if not request.confirm:
        raise HTTPException(
            400, "Explicit confirmation is required to apply staged changes."
        )

    with _staging_session(current=True) as session:
        if session.review_id is None or request.review_id != session.review_id:
            raise HTTPException(
                409, "Review the current staged changes before applying them."
            )
        session.review_id = None
        diff = batch_apply.compute_diff(session.draft, app.state.graph)
        results = batch_apply.apply_diff(app, diff)
        applied = all(result["ok"] for result in results)
        if applied:
            staging.discard(app)
        elif app.state.graph is not None:
            # The successful steps of this batch are already part of live state.
            # A new preview covers only what remains to be applied.
            session.baseline = staging.live_revision(app)
        return {"results": results, "applied": applied}
