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

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI
from loguru import logger
from src.backend import TopologyBackend
from src.connections.api_handler import APIHandler
from src.settings import Settings

from app import node_names
from app.live_lock import exclusive
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


_HIDDEN_TEMPLATE_NAMES = frozenset({"gns3-vm", "ginnungagap-appliance"})


def _hidden_template_names() -> frozenset[str]:
    configured = os.getenv("TOPOLOGYOPERATOR_HIDDEN_TEMPLATE_NAMES", "")
    names = frozenset(
        name.strip().casefold() for name in configured.split(",") if name.strip()
    )
    return names or _HIDDEN_TEMPLATE_NAMES


def list_templates() -> dict[str, Any]:
    """List placeable images for the live and design palettes.

    Category and port metadata describe visible GNS3 entries. The underlying
    template catalog still provides all images for deployment and validation.
    :raises TimeoutError: propagated from either Template-API server.
    """
    hidden = _hidden_template_names()
    gns3_templates = [
        template
        for template in _fetch_gns3_templates()
        if str(template.get("name", "")).casefold() not in hidden
    ]
    return {
        "gns3": sorted(
            name
            for name in APIHandler.get_gns3_template_names()
            if name.casefold() not in hidden
        ),
        "esxi": sorted(
            name
            for name in APIHandler.get_esxi_template_names()
            if name.casefold() not in hidden
        ),
        "gns3_categories": _gns3_template_categories(gns3_templates),
        "gns3_port_specs": _gns3_template_port_specs(gns3_templates),
    }


_PENDING_NAMES: set[str] = set()  # lower-cased names of nodes being added right now
_PENDING_LOCK = threading.Lock()


@contextmanager
def _reserved_name(app: FastAPI, name: str) -> Iterator[None]:
    """
    Claims a node name the moment the request arrives - before it may have to
    wait for the live lock - so a second "Add node" with the same name is turned
    down at once, even while the first is still deploying and not yet in the
    topology. Case-insensitive, like ESXi VM names effectively are.
    :raises ValueError: the name is empty, in the topology, or being added
    """
    key = name.strip().lower()
    if not key:
        raise ValueError("name must not be empty")
    with _PENDING_LOCK:
        existing = (
            {n.lower() for n in app.state.graph.nodes}
            if getattr(app.state, "graph", None) is not None
            else set()
        )
        if key in existing:
            raise ValueError(f"a node named {name!r} already exists")
        if key in _PENDING_NAMES:
            raise ValueError(
                f"a node named {name!r} is just being deployed - pick another name"
            )
        _PENDING_NAMES.add(key)
    try:
        yield
    finally:
        with _PENDING_LOCK:
            _PENDING_NAMES.discard(key)


@contextmanager
def _restore_on_failure(app: FastAPI, what: str) -> Iterator[None]:
    """
    Safety net: TopologyBackend deploys first and writes the topology file only
    after that succeeded, so a failed deploy normally leaves the file alone. If
    the file was touched anyway (a partial write, a future change there), put it
    back exactly as it was and re-read the graph. Whatever a failed deploy half
    created on ESXi/GNS3 is not touched here - Find Orphans lists and removes it.
    """
    path = Path(Settings.TOPOLOGY_FILE)
    before = path.read_text(encoding="utf-8")
    try:
        yield
    except Exception:
        logger.warning(f"Failed {what}: restoring the topology file as it was")
        path.write_text(before, encoding="utf-8")
        try:
            app.state.graph = load_graph()
        except Exception as exc:
            logger.warning(f"Could not reload the topology after the rollback: {exc}")
        raise


def add_node(app: FastAPI, name: str, role: str, image: str) -> None:
    existing = app.state.graph.nodes if app.state.graph is not None else ()
    node_names.validate(name, existing)
    with _reserved_name(app, name), exclusive("Add node"):
        if not Settings.TOPOLOGY_FILE:
            raise RuntimeError("Select a project before editing a live topology")
        logger.info(f"Adding node {name!r} (role {role}, image {image!r}) live")
        backend = get_or_create_backend(app)
        with _restore_on_failure(app, f"adding node {name!r}"):
            backend.add_node(Settings.TOPOLOGY_FILE, name, role, image)
        app.state.graph = load_graph()
        logger.info(f"Node {name!r} added")


def remove_node(app: FastAPI, name: str) -> None:
    with exclusive("Remove node"):
        if not Settings.TOPOLOGY_FILE:
            raise RuntimeError("Select a project before editing a live topology")
        logger.warning(
            f"Removing node {name!r} live (GNS3 node and ESXi VM are deleted)"
        )
        backend = get_or_create_backend(app)
        backend.remove_node(Settings.TOPOLOGY_FILE, name)
        app.state.graph = load_graph()
        logger.info(f"Node {name!r} removed")


def add_link(app: FastAPI, node1: str, if1: str, node2: str, if2: str) -> None:
    with exclusive("Add link"):
        if not Settings.TOPOLOGY_FILE:
            raise RuntimeError("Select a project before editing a live topology")
        logger.info(f"Adding link {node1}:{if1} <-> {node2}:{if2} live")
        backend = get_or_create_backend(app)
        with _restore_on_failure(app, f"adding link {node1}-{node2}"):
            backend.add_link(Settings.TOPOLOGY_FILE, node1, if1, node2, if2)
        app.state.graph = load_graph()
        logger.info(f"Link {node1}:{if1} <-> {node2}:{if2} added")


def remove_link(
    app: FastAPI,
    node1: str,
    node2: str,
    if1: str | None = None,
    if2: str | None = None,
) -> None:
    """``if1``/``if2`` pick one of several links between the same two nodes (see TopologyBackend.remove_link)."""
    with exclusive("Remove link"):
        if not Settings.TOPOLOGY_FILE:
            raise RuntimeError("Select a project before editing a live topology")
        label = (
            f"{node1}{':' + if1 if if1 else ''} <-> {node2}{':' + if2 if if2 else ''}"
        )
        logger.warning(f"Removing link {label} live")
        backend = get_or_create_backend(app)
        if if1 is None and if2 is None:
            backend.remove_link(Settings.TOPOLOGY_FILE, node1, node2)
        else:
            backend.remove_link(Settings.TOPOLOGY_FILE, node1, node2, if1, if2)
        app.state.graph = load_graph()
        logger.info(f"Link {label} removed")
