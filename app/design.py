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

import copy
from dataclasses import dataclass, field
from typing import Any

import yaml
from src.graph.environment import Environment
from src.graph.layout import compute_node_positions

from app import label_layout, node_names

_CANVAS_WIDTH = 1600.0
_CANVAS_HEIGHT = 900.0


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
    # user-moved port/name labels, see app/label_layout.py
    labels: dict[str, dict[str, float]] = field(default_factory=dict)
    # free-text notes per node name, see app/node_notes.py
    notes: dict[str, str] = field(default_factory=dict)


def new_state() -> DesignState:
    return DesignState()


def add_node(state: DesignState, name: str, role: str, image: str) -> None:
    node_names.validate(name, state.nodes)
    environment = Environment.get_environment(image)
    if environment == Environment.ON_NOTHING:
        raise ValueError(
            f"unknown image '{image}' - not a valid GNS3 or ESXi template name"
        )
    state.nodes[name] = DesignNode(
        name=name, role=role, image=image, environment=environment.name
    )


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
    label_layout.drop_node(state.labels, name)
    state.notes.pop(name, None)


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


def remove_link(
    state: DesignState,
    node1: str,
    node2: str,
    if1: str | None = None,
    if2: str | None = None,
) -> None:
    """
    Removes the link between two nodes. Two nodes can be linked more than
    once over different interfaces: ``if1``/``if2`` (the interface on
    node1/node2) pick the one to remove; without them the first link
    between the two goes, as before.
    :raises KeyError: no such link
    """

    def matches(link: tuple[str, str, str, str]) -> bool:
        if (link[0], link[2]) == (node1, node2):
            near, far = link[1], link[3]
        elif (link[0], link[2]) == (node2, node1):
            near, far = link[3], link[1]
        else:
            return False
        return (if1 is None or near == if1) and (if2 is None or far == if2)

    match = next((link for link in state.links if matches(link)), None)
    if match is None:
        wanted = f" using {if1 or '*'} and {if2 or '*'}" if (if1 or if2) else ""
        raise KeyError(f"no link between '{node1}' and '{node2}'{wanted}")
    state.links.remove(match)
    n1, if_1, n2, if_2 = match
    state.nodes[n1].interfaces.pop(if_1, None)
    state.nodes[n2].interfaces.pop(if_2, None)


def remove_many(
    state: DesignState, nodes: list[str], links: list[tuple[str, str]]
) -> None:
    """
    Removes the given links, then the given nodes (with their remaining
    links), as one change: everything is checked first, so an unknown
    name leaves the sketch untouched.
    :raises KeyError: a node or link that doesn't exist
    """
    for name in nodes:
        if name not in state.nodes:
            raise KeyError(f"no node named '{name}'")
    for node1, node2 in links:
        if not any({link[0], link[2]} == {node1, node2} for link in state.links):
            raise KeyError(f"no link between '{node1}' and '{node2}'")
    for node1, node2 in links:
        # An earlier entry (or a node removed below) may already have taken it.
        if any({link[0], link[2]} == {node1, node2} for link in state.links):
            remove_link(state, node1, node2)
    for name in nodes:
        remove_node(state, name)


def paste(
    state: DesignState,
    nodes: list[dict[str, Any]],
    links: list[tuple[str, str, str, str]],
) -> None:
    """
    Adds copied nodes ({name, role, image, x, y}) and the links between
    them as one change: on any error the sketch is put back exactly as it
    was, so a half-pasted selection never remains.
    :raises ValueError: a name already in use, an unknown image, an
        interface already used, ... (add_node/add_link's own messages)
    """
    before = copy.deepcopy(to_dict(state))
    try:
        for entry in nodes:
            add_node(state, entry["name"], entry["role"], entry["image"])
            state.layout[entry["name"]] = {
                "x": float(entry["x"]),
                "y": float(entry["y"]),
            }
        for node1, if1, node2, if2 in links:
            add_link(state, node1, if1, node2, if2)
    except Exception:
        restored = from_dict(before)
        state.nodes = restored.nodes
        state.links = restored.links
        state.layout = restored.layout
        state.drawings = restored.drawings
        state.labels = restored.labels
        state.notes = restored.notes
        raise


def node_links(links: Any, name: str) -> list[dict[str, str]]:
    """
    The links of one node as {interface, peer, peer_interface}, in the order of
    the (node1, if1, node2, if2) list. Unlike the interfaces map (interface ->
    neighbour node) this tells apart several links between the same two nodes.
    """
    result = []
    for node1, if1, node2, if2 in links:
        if node1 == name:
            result.append({"interface": if1, "peer": node2, "peer_interface": if2})
        elif node2 == name:
            result.append({"interface": if2, "peer": node1, "peer_interface": if1})
    return result


def to_topology_view(state: DesignState) -> list[dict[str, Any]]:
    """Same shape as GET /topology, for the frontend's shared renderer."""
    return [
        {
            "name": node.name,
            "role": node.role,
            "image": node.image,
            "environment": node.environment,
            "interfaces": dict(node.interfaces),
            "links": node_links(state.links, node.name),
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


def from_topology_yaml(text: str) -> DesignState:
    """Build an editable design from a TopologyBuilder topology document."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid topology YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("topology YAML must be a mapping")
    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise TypeError("topology must contain nodes and edges lists")

    state = new_state()
    for group in nodes:
        if not isinstance(group, dict):
            raise TypeError("each node group must be a mapping")
        names = group.get("names")
        role = group.get("role")
        image = group.get("image")
        if not isinstance(names, list) or not all(
            isinstance(name, str) for name in names
        ):
            raise ValueError("node names must be a list of strings")
        if not isinstance(role, str) or not isinstance(image, str):
            raise TypeError("each node group needs string role and image values")
        for name in names:
            add_node(state, name, role, image)
    for edge in edges:
        if not isinstance(edge, list) or len(edge) != 4:
            raise ValueError("each edge must contain node, interface, node, interface")
        add_link(state, *edge)
    auto_layout(state)
    return state


def to_dict(state: DesignState) -> dict[str, Any]:
    """
    A plain-dict snapshot of the whole sketch for app/design_store.py to
    persist to disk - includes each node's already-resolved
    `environment`, unlike to_topology_yaml_dict, so from_dict can restore
    it without re-asking the template service (see from_dict).
    """
    return {
        "nodes": [
            {
                "name": node.name,
                "role": node.role,
                "image": node.image,
                "environment": node.environment,
                "interfaces": dict(node.interfaces),
            }
            for node in state.nodes.values()
        ],
        "links": [list(link) for link in state.links],
        "layout": {name: dict(pos) for name, pos in state.layout.items()},
        "drawings": list(state.drawings),
        "labels": {key: dict(offset) for key, offset in state.labels.items()},
        "notes": dict(state.notes),
    }


def from_dict(data: dict[str, Any]) -> DesignState:
    """
    Restores a sketch from to_dict's own shape, constructing DesignNode
    directly rather than going through add_node/add_link - deliberately
    bypasses Environment.get_environment (a live template-service call)
    since a saved sketch's environment was already resolved once, and
    restoring it must keep working even while that service is down
    (the whole reason app/design_store.py exists: surviving a restart
    during exactly that kind of outage without losing the sketch).
    """
    state = new_state()
    for entry in data.get("nodes", []):
        state.nodes[entry["name"]] = DesignNode(
            name=entry["name"],
            role=entry["role"],
            image=entry["image"],
            environment=entry["environment"],
            interfaces=dict(entry.get("interfaces", {})),
        )
    state.links = [tuple(link) for link in data.get("links", [])]
    state.layout = {name: dict(pos) for name, pos in data.get("layout", {}).items()}
    state.drawings = list(data.get("drawings", []))
    state.labels = {key: dict(offset) for key, offset in data.get("labels", {}).items()}
    state.notes = dict(data.get("notes", {}))
    return state


class _NeighbourView:
    """Just enough of GenericNode's shape for _build_adjacency's own `neighbour.name` lookup."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class _NodeView:
    """
    Minimal GenericNode-shaped read-only view over a DesignNode, for
    src.graph.layout.compute_node_positions's own adjacency-building
    (_build_adjacency calls node.get_neighbour(interface_name) and
    expects a `.name` back) - DesignNode itself stores a neighbour as
    just a name string, not a live node reference, so it has no real
    get_neighbour() of its own to call directly.
    """

    __slots__ = ("_state", "interfaces", "name")

    def __init__(self, node: DesignNode, state: DesignState) -> None:
        self.name = node.name
        self.interfaces = node.interfaces
        self._state = state

    def get_neighbour(self, interface_name: str) -> _NeighbourView | None:
        neighbour_name = self.interfaces.get(interface_name)
        if neighbour_name is None or neighbour_name not in self._state.nodes:
            return None
        return _NeighbourView(neighbour_name)


def auto_layout(state: DesignState) -> None:
    """
    Computes a real force-directed layout for every node in state - the
    same algorithm (and canvas size) a deployed topology's own missing
    positions already fall back to (see app/layout.py's load_layout) -
    and writes it into state.layout. Used when a whole topology is
    created at once with nowhere for the operator to click each node
    into place individually (see app/generate_jobs.py, right after a
    successful LLM generation): without an explicit layout, every node
    would otherwise land on the same frontend-side default position and
    render as one fully overlapping stack.
    """
    views = {name: _NodeView(node, state) for name, node in state.nodes.items()}
    positions = compute_node_positions(views, _CANVAS_WIDTH, _CANVAS_HEIGHT)
    for name, (x, y) in positions.items():
        state.layout[name] = {"x": x, "y": y}


def apply_screenshot_layout(state: DesignState, layout: object) -> None:
    if not isinstance(layout, dict):
        raise TypeError("The generator did not provide a screenshot layout.")
    width, height = layout.get("width"), layout.get("height")
    positions = layout.get("positions")
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or not isinstance(positions, dict)
        or positions.keys() != state.nodes.keys()
    ):
        raise ValueError(
            "The screenshot layout must include a position for every device."
        )

    scale = min(_CANVAS_WIDTH / width, _CANVAS_HEIGHT / height)
    canvas_width, canvas_height = width * scale, height * scale
    offset_x = (_CANVAS_WIDTH - canvas_width) / 2
    offset_y = (_CANVAS_HEIGHT - canvas_height) / 2
    node_layout = {}
    for name, pair in positions.items():
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(
                type(value) in (int, float) and 0 <= value <= 1000 for value in pair
            )
        ):
            raise ValueError(f"Invalid screenshot position for {name!r}.")
        x, y = pair
        node_layout[name] = {
            "x": offset_x + canvas_width * x / 1000,
            "y": offset_y + canvas_height * y / 1000,
        }
    state.layout = node_layout


def layout_with_defaults(state: DesignState) -> dict[str, dict[str, float]]:
    """
    The saved positions, plus a force-directed default for every node that has none (a design opened from a stored
    project or created by a script, where nobody placed the nodes by clicking) - so they do not all pile up on one
    spot. Changes nothing in `state`.
    """
    result = {
        name: dict(pos) for name, pos in state.layout.items() if name in state.nodes
    }
    missing = [name for name in state.nodes if name not in result]
    if missing:
        views = {name: _NodeView(node, state) for name, node in state.nodes.items()}
        positions = compute_node_positions(views, _CANVAS_WIDTH, _CANVAS_HEIGHT)
        for name in missing:
            x, y = positions[name]
            result[name] = {"x": x, "y": y}
    return result
