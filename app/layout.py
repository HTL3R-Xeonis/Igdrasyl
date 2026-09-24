"""
Node canvas positions for the frontend's visual topology diagram - a
TopologyOperator-owned concern, independent of GNS3's own node x/y
(which only exists for GNS3-native nodes anyway, not for ESXi VMs with
no Cloud-node bridge). Backed by a small local JSON file, gitignored -
same treatment as settings.yml/.env: a per-deployment file, not
topology truth, never committed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.graph.blocks.generic_node import GenericNode
from src.graph.layout import compute_node_positions

_LAYOUT_FILE_ENV_VAR = "TOPOLOGYOPERATOR_LAYOUT_FILE"
_DEFAULT_LAYOUT_FILE = "layout.json"
_CANVAS_WIDTH = 1600.0
_CANVAS_HEIGHT = 900.0


def _layout_path() -> Path:
    return Path(os.getenv(_LAYOUT_FILE_ENV_VAR, _DEFAULT_LAYOUT_FILE))


def _read_saved_positions() -> dict[str, dict[str, float]]:
    path = _layout_path()
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def load_layout(nodes: dict[str, GenericNode]) -> dict[str, dict[str, float]]:
    """
    Every node's position: saved (from a prior drag) where available,
    otherwise a computed force-directed default - so the frontend always
    gets a full, sensible layout even before anyone has dragged anything.
    """
    saved = _read_saved_positions()
    computed = compute_node_positions(nodes, _CANVAS_WIDTH, _CANVAS_HEIGHT)

    result: dict[str, dict[str, float]] = {}
    for name in nodes:
        if name in saved:
            result[name] = saved[name]
        else:
            x, y = computed[name]
            result[name] = {"x": x, "y": y}
    return result


def save_position(name: str, x: float, y: float) -> None:
    save_positions({name: {"x": x, "y": y}})


def save_positions(positions_update: dict[str, dict[str, float]]) -> None:
    """Same as calling save_position for each entry, but a single read-modify-write."""
    positions = _read_saved_positions()
    positions.update(positions_update)
    path = _layout_path()
    with open(path, "w") as f:
        json.dump(positions, f)


def apply_imported_layout(
    nodes: dict[str, GenericNode], bundle_layout: dict[str, Any]
) -> None:
    """
    Sets every post-deploy node's saved position after a project import:
    the bundle's own value where it gave one (and it's well-formed),
    otherwise a freshly computed default for the CURRENT graph - never a
    stale value left over from a previously imported/deployed topology
    that happened to reuse the same node name. Real bug found live: a
    node name (e.g. "R1") reused across unrelated topology imports kept
    whatever position an earlier, structurally different topology had
    saved for it, while other node names not seen before got a freshly
    computed position instead - the canvas ended up mixing two
    unrelated coordinate systems, scattered and barely resembling
    either topology. Always covering every current node here (bundle or
    computed, never "leave whatever was there") is what prevents that.
    """
    computed = compute_node_positions(nodes, _CANVAS_WIDTH, _CANVAS_HEIGHT)
    resolved: dict[str, dict[str, float]] = {}
    for name in nodes:
        bundle_pos = bundle_layout.get(name)
        if isinstance(bundle_pos, dict) and "x" in bundle_pos and "y" in bundle_pos:
            resolved[name] = {"x": bundle_pos["x"], "y": bundle_pos["y"]}
        else:
            x, y = computed[name]
            resolved[name] = {"x": x, "y": y}
    save_positions(resolved)
