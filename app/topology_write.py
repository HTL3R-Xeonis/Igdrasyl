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

import requests
from fastapi import FastAPI
from loguru import logger

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


def _gns3_template_categories() -> dict[str, str]:
    """
    Best-effort name -> GNS3 category ("router"/"switch"/"guard"/
    "guest"/...) lookup, straight from the same Template-API server
    APIHandler.get_gns3_template_names() already talks to but discards
    everything except the name. Lets the frontend's Add Node Role
    filter go by GNS3's own real categorization instead of guessing
    from a template's display name (which the name alone can't reliably
    tell apart - e.g. Cisco's "IOSv" router and "IOSvL2" switch images
    only differ by an "L2" substring).

    Deliberately tolerant of the Template-API server being unreachable,
    slow, or returning a differently-shaped body (that exact server has
    had a real outage this project already hit and diagnosed - see
    HANDOFF.md) - any failure here just means the frontend falls back
    to its own name-keyword guess for every GNS3 template, a degraded
    UI, not a broken /templates endpoint.
    """
    if Settings.API.LITERAL_API_VALUES:
        return {}
    try:
        data = APIHandler.get(f"{Settings.API.GNS3_TEMPLATE_SERVER_URL}/api/templates")
        return {
            template["name"]: template["category"]
            for template in data.get("templates", [])
            if "name" in template and "category" in template
        }
    except (requests.exceptions.RequestException, TimeoutError, ValueError, KeyError) as exc:
        logger.warning(f"Couldn't fetch GNS3 template categories, frontend will fall back to guessing: {exc}")
        return {}


def list_templates() -> dict[str, list[str] | dict[str, str]]:
    """
    Every image name valid for add_node - the practical contract behind
    TopologyBackend.add_node's `image` argument is "matches a name from
    one of these two sets" (see Environment.get_environment, which is
    what actually decides ON_GNS3 vs ON_ESXI - not this project's role
    field). `gns3_categories` is purely an informational hint for the
    frontend's own Role filter/guess - never used for validation here
    or on the TopologyBuilder side.
    :raises TimeoutError: propagated from either Template-API server.
    """
    return {
        "gns3": sorted(APIHandler.get_gns3_template_names()),
        "esxi": sorted(APIHandler.get_esxi_template_names()),
        "gns3_categories": _gns3_template_categories(),
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
