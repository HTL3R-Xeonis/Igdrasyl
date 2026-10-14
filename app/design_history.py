"""
Undo/redo for the offline design sketch (app.state.design).

Every change to the sketch is wrapped in `step(app)`: it snapshots the
sketch (design.to_dict, deep-copied) before the change and, only if the
change succeeded, pushes that snapshot on the undo stack and empties the
redo stack. Undo/redo swap the current sketch for a snapshot via
design.from_dict - which never asks the template service anything, so
history keeps working during a template outage, exactly like the autosave
in app/design_store.py.

Snapshot-based on purpose: one mechanism covers node/link add and remove,
moves, drawings, paste, delete, reset, a generated topology and an opened
library project, instead of an inverse operation per endpoint. Strings
(the big data: URLs of image drawings) are shared between snapshots, not
copied, so the 100-step depth stays cheap.

Only the design sketch has history. A live project's nodes and links are
real VMs - those changes are deliberately not undoable (see the plan in
handoff/PLAN_gui_gns3_parity.md).
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from loguru import logger

from app import design, design_store

MAX_STEPS = 100


@dataclass
class _History:
    undo: list[dict[str, Any]] = field(default_factory=list)
    redo: list[dict[str, Any]] = field(default_factory=list)


def _history(app: FastAPI) -> _History:
    history = getattr(app.state, "design_history", None)
    if history is None:
        history = app.state.design_history = _History()
    return history


def _snapshot(app: FastAPI) -> dict[str, Any]:
    return copy.deepcopy(design.to_dict(app.state.design))


def push(app: FastAPI, snapshot: dict[str, Any]) -> None:
    history = _history(app)
    history.undo.append(snapshot)
    del history.undo[:-MAX_STEPS]
    history.redo.clear()


@contextmanager
def step(app: FastAPI):
    """One undoable change: `with design_history.step(app): <mutate app.state.design>`."""
    snapshot = _snapshot(app)
    yield
    push(app, snapshot)


def _restore(app: FastAPI, snapshot: dict[str, Any]) -> None:
    app.state.design = design.from_dict(snapshot)
    design_store.save(app.state.design)


def undo(app: FastAPI) -> bool:
    history = _history(app)
    if not history.undo:
        return False
    history.redo.append(_snapshot(app))
    _restore(app, history.undo.pop())
    logger.info(f"Design undo ({len(history.undo)} step(s) left to undo)")
    return True


def redo(app: FastAPI) -> bool:
    history = _history(app)
    if not history.redo:
        return False
    history.undo.append(_snapshot(app))
    _restore(app, history.redo.pop())
    logger.info(f"Design redo ({len(history.redo)} step(s) left to redo)")
    return True


def status(app: FastAPI) -> dict[str, bool]:
    history = _history(app)
    return {"can_undo": bool(history.undo), "can_redo": bool(history.redo)}


def clear(app: FastAPI) -> None:
    app.state.design_history = _History()
