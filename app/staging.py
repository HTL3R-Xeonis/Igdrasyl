"""Keep staged live edits tied to the project and topology they came from."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from src.settings import Settings

from app import design
from app.batch_apply import link_key


class NoActiveStaging(RuntimeError):
    """No live-edit staging session is open."""


class StaleStaging(RuntimeError):
    """The current live topology no longer matches the staging session."""


@dataclass(frozen=True)
class LiveRevision:
    target: tuple[str | None, ...]
    nodes: frozenset[tuple[str, str, str]]
    links: frozenset[tuple[str, str, str, str]]


@dataclass
class Session:
    draft: design.DesignState
    baseline: LiveRevision
    review_id: str | None = None


def live_revision(app: FastAPI) -> LiveRevision:
    graph: Any = app.state.graph
    if graph is None:
        raise RuntimeError(app.state.startup_warning or "topology unavailable")
    return LiveRevision(
        target=(
            str(Settings.TOPOLOGY_FILE),
            Settings.ESXI.IP,
            app.state.gns3_vm_ip,
            app.state.project_name,
            app.state.project_id,
        ),
        nodes=frozenset(
            (name, node.role, node.image) for name, node in graph.nodes.items()
        ),
        links=frozenset(link_key(*edge) for edge in graph.edges),
    )


def is_active(app: FastAPI) -> bool:
    return getattr(app.state, "staged_edit", None) is not None


def get(app: FastAPI) -> Session:
    session = getattr(app.state, "staged_edit", None)
    if session is None:
        raise NoActiveStaging("no live-edit staging session is open")
    return session


def start(app: FastAPI) -> Session:
    baseline = live_revision(app)
    session = Session(design.from_graph(app.state.graph), baseline)
    app.state.staged_edit = session
    return session


def require_current(app: FastAPI, session: Session) -> None:
    if live_revision(app) != session.baseline:
        raise StaleStaging(
            "The live topology or selected project changed. Discard this draft and start staging again."
        )


def discard(app: FastAPI) -> None:
    app.state.staged_edit = None
