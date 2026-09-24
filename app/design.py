"""
An offline "design" topology - a scratch sketch built through the same
canvas UI as a real project, but held entirely in this process's memory
(app.state.design) and never touching ESXi/GNS3 or the real
Settings.TOPOLOGY_FILE. Reachable from the landing page, independent of
whichever host profile happens to be loaded - lets an operator lay out
a topology before any host is even chosen, then hand it off via
GET /design/export (a .topoproj bundle, importable later against a real
host through the existing POST /project/import).

Unlike app/topology_write.py's live add_node/add_link (which mutate the
real topology file and deploy for real via TopologyBackend), everything
here is a pure in-memory data structure - there is no live GNS3/ESXi
connection anywhere in this module. Interface names are whatever the
operator types (see app/main.py's GET /design/nodes/{name}/ports,
which always 404s - the frontend already falls back to free text for
that, the same path an ESXi VM's live-less interfaces use today), since
there's no live GNS3 template to enumerate a device's real adapters
from without ever deploying it.

Cleared only by a process restart or POST /design/reset - never
auto-cleared by navigating to/from the landing page, so switching hosts
and coming back doesn't lose an in-progress sketch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.graph.environment import Environment


@dataclass
class DesignNode:
    name: str
    role: str
    image: str
    environment: str
    interfaces: dict[str, str | None] = field(default_factory=dict)


@dataclass
class DesignState:
    nodes: dict[str, DesignNode] = field(default_factory=dict)
    links: list[tuple[str, str, str, str]] = field(default_factory=list)
    layout: dict[str, dict[str, float]] = field(default_factory=dict)
    drawings: list[dict[str, Any]] = field(default_factory=list)


def new_state() -> DesignState:
    return DesignState()


def add_node(state: DesignState, name: str, role: str, image: str) -> None:
    if not name.strip():
        raise ValueError("name must not be empty")
    if name in state.nodes:
        raise ValueError(f"a node named '{name}' already exists")
    environment = Environment.get_environment(image)
    if environment == Environment.ON_NOTHING:
        raise ValueError(f"unknown image '{image}' - not a valid GNS3 or ESXi template name")
    state.nodes[name] = DesignNode(name=name, role=role, image=image, environment=environment.name)


def remove_node(state: DesignState, name: str) -> None:
    if name not in state.nodes:
        raise KeyError(f"no node named '{name}'")
    del state.nodes[name]
    state.links = [link for link in state.links if name not in (link[0], link[2])]
    for node in state.nodes.values():
        for if_name, neighbour in list(node.interfaces.items()):
            if neighbour == name:
                del node.interfaces[if_name]
    state.layout.pop(name, None)


def add_link(state: DesignState, node1: str, if1: str, node2: str, if2: str) -> None:
    if node1 not in state.nodes:
        raise ValueError(f"no node named '{node1}'")
    if node2 not in state.nodes:
        raise ValueError(f"no node named '{node2}'")
    if node1 == node2:
        raise ValueError("a node can't link to itself")
    if if1 in state.nodes[node1].interfaces:
        raise ValueError(f"{node1}:{if1} is already in use")
    if if2 in state.nodes[node2].interfaces:
        raise ValueError(f"{node2}:{if2} is already in use")
    state.nodes[node1].interfaces[if1] = node2
    state.nodes[node2].interfaces[if2] = node1
    state.links.append((node1, if1, node2, if2))


def remove_link(state: DesignState, node1: str, node2: str) -> None:
    match = next(
        (link for link in state.links if {link[0], link[2]} == {node1, node2}), None
    )
    if match is None:
        raise KeyError(f"no link between '{node1}' and '{node2}'")
    state.links.remove(match)
    n1, if1, n2, if2 = match
    state.nodes[n1].interfaces.pop(if1, None)
    state.nodes[n2].interfaces.pop(if2, None)


def to_topology_view(state: DesignState) -> list[dict[str, Any]]:
    """Same shape as GET /topology, for the frontend's shared renderer."""
    return [
        {
            "name": node.name,
            "role": node.role,
            "image": node.image,
            "environment": node.environment,
            "interfaces": dict(node.interfaces),
        }
        for node in state.nodes.values()
    ]


def to_topology_yaml_dict(state: DesignState) -> dict[str, Any]:
    """
    The same {nodes, edges} shape TopologyBuilder's own config file
    format uses (see ConfigFileHandler) - grouping by (role, image) into
    a single `names:` list is cosmetic (a single-node group works fine
    too), matching how a hand-written config file typically looks.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for node in state.nodes.values():
        groups.setdefault((node.role, node.image), []).append(node.name)
    return {
        "nodes": [
            {"names": names, "role": role, "image": image}
            for (role, image), names in groups.items()
        ],
        "edges": [[n1, if1, n2, if2] for n1, if1, n2, if2 in state.links],
    }
