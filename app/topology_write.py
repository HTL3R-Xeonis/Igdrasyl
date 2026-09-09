"""
Live node/link create+delete, via TopologyBuilder's own TopologyBackend
facade - the write-side counterpart to app/topology.py's read-only
load_graph(). Kept in its own module, independently testable, same
separation main.py already uses for config_push.py/vm_screen.py.

TopologyBackend constructs its own VMOrchestrator internally (its own
ESXi connection, separate from app.state.esxi_connection) - a real,
inherited second-session cost, not fixable from this repo. Built lazily
here (on first mutating call, not in main.py's lifespan) so that cost -
and any of its construction failures - is only paid if Phase B is
actually used, rather than becoming a second way for the whole app to
fail to start.
"""

from __future__ import annotations

from fastapi import FastAPI

from src.backend import TopologyBackend
from src.connections.api_handler import APIHandler
from src.settings import Settings

from app.topology import load_graph


def get_or_create_backend(app: FastAPI) -> TopologyBackend:
    backend = getattr(app.state, "topology_backend", None)
    if backend is None:
        backend = TopologyBackend(
            Settings.ESXI.IP,
            Settings.ESXI.PORT,
            Settings.ESXI.USERNAME,
            Settings.ESXI.PASSWORD,
            Settings.ESXI.GNS3_VM_NAME,
            Settings.GNS3.USERNAME,
            Settings.GNS3.PASSWORD,
            Settings.GNS3.PORT,
        )
        app.state.topology_backend = backend
    return backend


def list_templates() -> dict[str, list[str]]:
    """
    Every image name valid for add_node - the practical contract behind
    TopologyBackend.add_node's `image` argument is "matches a name from
    one of these two sets" (see Environment.get_environment, which is
    what actually decides ON_GNS3 vs ON_ESXI - not this project's role
    field).
    :raises TimeoutError: propagated from either Template-API server.
    """
    return {
        "gns3": sorted(APIHandler.get_gns3_template_names()),
        "esxi": sorted(APIHandler.get_esxi_template_names()),
    }


def add_node(app: FastAPI, name: str, role: str, image: str) -> None:
    backend = get_or_create_backend(app)
    backend.add_node(Settings.TOPOLOGY_FILE, name, role, image)
    app.state.graph = load_graph()


def remove_node(app: FastAPI, name: str) -> None:
    backend = get_or_create_backend(app)
    backend.remove_node(Settings.TOPOLOGY_FILE, name)
    app.state.graph = load_graph()


def add_link(app: FastAPI, node1: str, if1: str, node2: str, if2: str) -> None:
    backend = get_or_create_backend(app)
    backend.add_link(Settings.TOPOLOGY_FILE, node1, if1, node2, if2)
    app.state.graph = load_graph()


def remove_link(app: FastAPI, node1: str, node2: str) -> None:
    backend = get_or_create_backend(app)
    backend.remove_link(Settings.TOPOLOGY_FILE, node1, node2)
    app.state.graph = load_graph()
