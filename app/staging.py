"""
Live-edit staging: a draft copy of the live topology an operator can
edit (add/remove nodes and links) without touching ESXi/GNS3, then
review as a diff against the real live topology and apply as one
back-to-back sequence - instead of each change deploying immediately
and disruptively on its own (see app/topology_write.py's add_node/
add_link docstrings). Held in app.state.staged_edit, a DesignState
built by design.from_graph - this module only wraps design.py's own
add_node/remove_node/add_link/remove_link against that one state, it
does not reimplement them.

Deliberately narrow (nodes and links only, no drawings/notes/labels/
undo-redo): those belong to the offline design sketch
(app.state.design), a separate, much older and richer feature: staging
a live edit is about the topology graph itself, not annotating it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from app import design


class NoActiveStaging(RuntimeError):
    """No staging session is currently open - start() wasn't called, or it was discarded/applied already."""


def is_active(app: FastAPI) -> bool:
    return getattr(app.state, "staged_edit", None) is not None


def get(app: FastAPI) -> design.DesignState:
    state = getattr(app.state, "staged_edit", None)
    if state is None:
        raise NoActiveStaging("no live-edit staging session is open")
    return state


def start(app: FastAPI) -> design.DesignState:
    """
    Opens a new staging session from the current live topology,
    discarding any previous one. :raises RuntimeError: no live topology
    loaded (app.state.graph is None) - same guard as every other live
    endpoint.
    """
    if app.state.graph is None:
        raise RuntimeError(app.state.startup_warning or "topology unavailable")
    app.state.staged_edit = design.from_graph(app.state.graph)
    return app.state.staged_edit


def discard(app: FastAPI) -> None:
    app.state.staged_edit = None


def to_topology_view(app: FastAPI) -> list[dict[str, Any]]:
    return design.to_topology_view(get(app))
