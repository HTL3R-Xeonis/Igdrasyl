"""
Diffs a staged draft (app/staging.py) against the live topology
(app.state.graph) and applies the difference as one reviewed, back-to-
back sequence of the existing live write calls (app/topology_write.py).

There is no multi-node batch API on TopologyBuilder's side - each
add_node/remove_node/add_link/remove_link call is still its own,
already-disruptive deploy. This only removes the friction of reviewing
and confirming every single one of them separately: stage several
changes, see one diff, confirm once, then watch them run in order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests
from fastapi import FastAPI

from app import design, topology_write

# Same tuple app/routes/common.py's LIVE_WRITE_ERRORS uses for the
# existing single-change live endpoints - a batch step can fail for
# exactly the same reasons a standalone one can.
_APPLY_ERRORS = (
    ValueError,
    RuntimeError,
    ConnectionError,
    TimeoutError,
    requests.exceptions.RequestException,
)


@dataclass
class Diff:
    added_nodes: list[dict[str, str]] = field(default_factory=list)
    removed_nodes: list[str] = field(default_factory=list)
    added_links: list[tuple[str, str, str, str]] = field(default_factory=list)
    removed_links: list[tuple[str, str, str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.added_nodes
            or self.removed_nodes
            or self.added_links
            or self.removed_links
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "added_nodes": self.added_nodes,
            "removed_nodes": self.removed_nodes,
            "added_links": [list(link) for link in self.added_links],
            "removed_links": [list(link) for link in self.removed_links],
        }


def link_key(node1: str, if1: str, node2: str, if2: str) -> tuple[str, str, str, str]:
    """Order-independent so the same link read from either side (draft vs. live) compares equal."""
    return (node1, if1, node2, if2) if node1 <= node2 else (node2, if2, node1, if1)


def compute_diff(draft: design.DesignState, graph: Any) -> Diff:
    live_names = set(graph.nodes)
    draft_names = set(draft.nodes)

    added_nodes = [
        {"name": node.name, "role": node.role, "image": node.image}
        for name, node in draft.nodes.items()
        if name not in live_names
    ]
    removed_nodes = sorted(live_names - draft_names)

    live_links = {link_key(*edge): edge for edge in graph.edges}
    draft_links = {link_key(*link): link for link in draft.links}

    added_links = [link for key, link in draft_links.items() if key not in live_links]
    removed_links = [edge for key, edge in live_links.items() if key not in draft_links]
    return Diff(
        added_nodes=added_nodes,
        removed_nodes=removed_nodes,
        added_links=added_links,
        removed_links=removed_links,
    )


def apply_diff(app: FastAPI, diff: Diff) -> list[dict[str, Any]]:
    """
    Applies removed links, then removed nodes, then added nodes, then
    added links - the same dependency order a human clicking through
    the same changes one at a time would have to follow. Stops at the
    first failing step so nothing is attempted against state the
    previous step already left inconsistent; everything before it has
    already really happened live and stays applied (same as any other
    live write failure today - there is no transactional rollback
    across separate deploys).
    :return: one {"action", "target", "ok", "error"} entry per attempted step
    """
    results: list[dict[str, Any]] = []

    def run(action: str, target: str, func, *args: Any) -> bool:
        try:
            func(*args)
        except _APPLY_ERRORS as exc:
            results.append(
                {"action": action, "target": target, "ok": False, "error": str(exc)}
            )
            return False
        results.append({"action": action, "target": target, "ok": True, "error": None})
        return True

    for node1, if1, node2, if2 in diff.removed_links:
        if not run(
            "remove_link",
            f"{node1}:{if1} <-> {node2}:{if2}",
            topology_write.remove_link,
            app,
            node1,
            node2,
            if1,
            if2,
        ):
            return results
    for name in diff.removed_nodes:
        if not run("remove_node", name, topology_write.remove_node, app, name):
            return results
    for added in diff.added_nodes:
        if not run(
            "add_node",
            added["name"],
            topology_write.add_node,
            app,
            added["name"],
            added["role"],
            added["image"],
        ):
            return results
    for node1, if1, node2, if2 in diff.added_links:
        if not run(
            "add_link",
            f"{node1}:{if1} <-> {node2}:{if2}",
            topology_write.add_link,
            app,
            node1,
            if1,
            node2,
            if2,
        ):
            return results
    return results
