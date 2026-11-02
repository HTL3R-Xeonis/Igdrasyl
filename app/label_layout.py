"""
User-moved port and node-name labels on the canvas.

A label's default spot comes from the geometry (next to its link endpoint, under its icon); what is stored here is
only the user's offset from that spot, so a label keeps its place relative to its node when the node is dragged. Keys
are "<node>|<interface>" for a port label and "<node>|" for the node's name. Same treatment as app/layout.py: a
per-deployment JSON file, git-ignored, never topology truth. The design sketch keeps its own copy in
DesignState.labels (see app/design.py), so undo/redo and the autosave cover it.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

_LABELS_FILE_ENV_VAR = "TOPOLOGYOPERATOR_LABELS_FILE"
_DEFAULT_LABELS_FILE = "label_layout.json"
# Generous for a 1600x900 canvas; stops a typo or a runaway drag from storing something absurd.
_MAX_OFFSET = 4000.0


def _labels_path() -> Path:
    return Path(os.getenv(_LABELS_FILE_ENV_VAR, _DEFAULT_LABELS_FILE))


def node_of(key: str) -> str:
    """The node a label key belongs to ("" when the key has no separator, which is invalid)."""
    return key.rpartition("|")[0]


def apply_changes(
    labels: dict[str, dict[str, float]],
    changes: dict[str, dict[str, float] | None],
) -> None:
    """
    Merges `changes` into `labels` in place. A None value or an all-zero offset removes the entry (the label goes back
    to its default spot); offsets are clamped to +-_MAX_OFFSET.
    :raises ValueError: a key without "<node>|", or an offset that is not a finite number.
    """
    for key, offset in changes.items():
        if not node_of(key):
            raise ValueError(
                f"{key!r} is not a label key (expected '<node>|<interface>')"
            )
        if offset is None:
            labels.pop(key, None)
            continue
        dx, dy = float(offset["dx"]), float(offset["dy"])
        if not (math.isfinite(dx) and math.isfinite(dy)):
            raise ValueError(f"offset of {key!r} is not a finite number")
        dx = max(-_MAX_OFFSET, min(_MAX_OFFSET, dx))
        dy = max(-_MAX_OFFSET, min(_MAX_OFFSET, dy))
        if dx == 0 and dy == 0:
            labels.pop(key, None)
        else:
            labels[key] = {"dx": dx, "dy": dy}


def load() -> dict[str, dict[str, float]]:
    """Every saved label offset; empty when nothing was ever moved or the file is unreadable."""
    path = _labels_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_changes(changes: dict[str, dict[str, float] | None]) -> dict[str, Any]:
    """One read-modify-write of the labels file; returns the full set afterwards."""
    labels = load()
    apply_changes(labels, changes)
    _labels_path().write_text(json.dumps(labels))
    return labels


def restrict(labels: dict[str, Any], known_nodes: Any) -> dict[str, dict[str, float]]:
    """
    `labels` cleaned for a project: only entries of nodes in `known_nodes` and with a usable offset. Anything else
    (a node that is gone, a hand-edited bundle's garbage) is dropped instead of failing the whole export or import.
    """
    clean: dict[str, dict[str, float]] = {}
    for key, offset in (labels or {}).items():
        if node_of(str(key)) not in known_nodes or not isinstance(offset, dict):
            continue
        try:
            apply_changes(clean, {str(key): offset})
        except (ValueError, KeyError, TypeError):
            continue
    return clean


def replace_all(
    labels: dict[str, Any], known_nodes: Any
) -> dict[str, dict[str, float]]:
    """
    Makes `labels` (a bundle's) the whole saved set, for the nodes that exist after an import. Same reasoning as
    app/layout.py's apply_imported_layout: an offset left over from an earlier project that happened to reuse a node
    name must not survive into this one.
    """
    clean = restrict(labels, known_nodes)
    _labels_path().write_text(json.dumps(clean))
    return clean


def drop_node(labels: dict[str, dict[str, float]], name: str) -> None:
    """Forgets every label of a node that is gone (a deleted node must not hand its offsets to a new one)."""
    for key in [k for k in labels if node_of(k) == name]:
        del labels[key]
