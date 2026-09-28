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

from typing import Any

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


def _fetch_gns3_templates() -> list[dict]:
    """
    Raw template list from the GNS3 Template-API server (see
    Settings.API.GNS3_TEMPLATE_SERVER_URL) - the one shared fetch behind
    both _gns3_template_categories and _gns3_template_port_specs, so
    list_templates only hits this server once for both instead of twice.

    Deliberately tolerant of the Template-API server being unreachable,
    slow, or returning a differently-shaped body (that exact server has
    had a real outage this project already hit and diagnosed - see
    HANDOFF.md) - any failure here just means the frontend falls back to
    guessing (categories) or free text (ports) for every GNS3 template,
    a degraded UI, not a broken /templates endpoint.
    """
    if Settings.API.LITERAL_API_VALUES:
        return []
    try:
        data = APIHandler.get(f"{Settings.API.GNS3_TEMPLATE_SERVER_URL}/api/templates")
        return data.get("templates", [])
    except (
        requests.exceptions.RequestException,
        TimeoutError,
        ValueError,
        KeyError,
    ) as exc:
        logger.warning(
            f"Couldn't fetch GNS3 template details, frontend will fall back to guessing/free text: {exc}"
        )
        return []


def _gns3_template_categories(templates: list[dict]) -> dict[str, str]:
    """
    name -> GNS3 category ("router"/"switch"/"guard"/"guest"/...).
    Lets the frontend's Add Node Role filter go by GNS3's own real
    categorization instead of guessing from a template's display name
    (which the name alone can't reliably tell apart - e.g. Cisco's
    "IOSv" router and "IOSvL2" switch images only differ by an "L2"
    substring).
    :param templates: _fetch_gns3_templates()'s own result
    """
    return {
        template["name"]: template["category"]
        for template in templates
        if "name" in template and "category" in template
    }


def _gns3_template_port_specs(templates: list[dict]) -> dict[str, dict[str, Any]]:
    """
    name -> {"adapters", "port_name_format", "port_segment_size",
    "first_port_name"} for every GNS3 template whose real interface
    names can be computed without ever deploying it - lets Add Link
    offer the same named ports GNS3 itself would create (e.g. Cisco
    IOSv's Gi0/0..Gi0/3) for a node that was never actually deployed
    (the offline design canvas's whole point - see app/design.py), and
    as a same-quality fallback for a live node whose own GET
    /nodes/{name}/ports call fails transiently. GNS3's own port-naming
    rule, reproduced in the frontend's computeGns3PortNames(): port
    index i's name is port_name_format.format(i % port_segment_size,
    i // port_segment_size) when port_segment_size is set, otherwise
    just port_name_format.format(i, 0) - except index 0 uses
    first_port_name verbatim when the template gives one.

    A template missing "adapters" or "port_name_format" (GNS3's own
    builtin infra nodes - Cloud/NAT/VPCS/Ethernet switch|hub/Frame Relay
    switch/ATM switch - and Docker-based ones, all of which GNS3 names
    dynamically/on demand rather than declaring a fixed format) simply
    has no entry here; the frontend already falls back to free text for
    any image with none.
    :param templates: _fetch_gns3_templates()'s own result
    """
    specs = {}
    for template in templates:
        name = template.get("name")
        adapters = template.get("adapters")
        port_name_format = template.get("port_name_format")
        if name and adapters and port_name_format:
            specs[name] = {
                "adapters": adapters,
                "port_name_format": port_name_format,
                "port_segment_size": template.get("port_segment_size") or 0,
                "first_port_name": template.get("first_port_name") or "",
            }
    return specs


def list_templates() -> dict[str, Any]:
    """
    Every image name valid for add_node - the practical contract behind
    TopologyBackend.add_node's `image` argument is "matches a name from
    one of these two sets" (see Environment.get_environment, which is
    what actually decides ON_GNS3 vs ON_ESXI - not this project's role
    field). `gns3_categories` is purely an informational hint for the
    frontend's own Role filter/guess, and `gns3_port_specs` for its
    Add Link interface picker (see _gns3_template_port_specs) - neither
    is ever used for validation here or on the TopologyBuilder side.
    :raises TimeoutError: propagated from either Template-API server.
    """
    gns3_templates = _fetch_gns3_templates()
    return {
        "gns3": sorted(APIHandler.get_gns3_template_names()),
        "esxi": sorted(APIHandler.get_esxi_template_names()),
        "gns3_categories": _gns3_template_categories(gns3_templates),
        "gns3_port_specs": _gns3_template_port_specs(gns3_templates),
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
