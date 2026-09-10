"""
TopologyOperator's FastAPI backend - Phase 0/1 read-only foundation.
Everything else (GNS3 console proxy, config push, embedded VM consoles)
plugs into the connections/graph this module resolves once at startup.

Run with: uvicorn app.main:app --reload
Point TOPOLOGYOPERATOR_SETTINGS at a settings.yml first (see
settings.example.yml) - without one, TopologyBuilder's own hardcoded
defaults apply, which won't match this project's real infrastructure.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

from src.connections.esxi_connection import ESXiConnection
from src.connections.gns3_connection import GNS3Connection
from src.graph.environment import Environment
from src.settings import Settings

from app import console, lifecycle, topology_write, vm_console
from app.config_push import DeviceResult, push_all
from app.gns3_state import NoLiveProject, list_live_nodes, list_node_ports
from app.layout import load_layout, save_position
from app.topology import load_graph, resolve_project_name

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_SETTINGS_ENV_VAR = "TOPOLOGYOPERATOR_SETTINGS"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings_path = os.getenv(_SETTINGS_ENV_VAR)
    if settings_path:
        Settings.initialise_settings(settings_path)

    # Opens a real, persistent pyvmomi session - done once here and
    # reused for the app's lifetime rather than per-request, same
    # reasoning as TopologyBuilder's own CLI commands.
    app.state.esxi_connection = ESXiConnection(
        Settings.ESXI.IP,
        Settings.ESXI.PORT,
        Settings.ESXI.USERNAME,
        Settings.ESXI.PASSWORD,
    )

    # The graph/topology file itself is always available even if live
    # GNS3 state below can't be resolved - fine to fail startup if the
    # topology file is missing/invalid though, unlike the network calls
    # below, which can legitimately be transient (GNS3 VM's DHCP lease
    # not resolved yet, project not deployed yet).
    app.state.graph = load_graph()
    app.state.project_name = resolve_project_name()
    # vm_name -> assigned serial console port, in-memory for this
    # process's lifetime - see app/vm_console.py.
    app.state.vm_console_ports = {}

    app.state.gns3_vm_ip = None
    app.state.project_id = None
    app.state.startup_warning = None
    try:
        gns3_vm_ip = app.state.esxi_connection.get_vm_ip_address(
            Settings.ESXI.GNS3_VM_NAME
        )
        if gns3_vm_ip is None:
            raise RuntimeError(
                f"GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}' not found or has no "
                f"IP reported yet"
            )
        app.state.gns3_vm_ip = gns3_vm_ip

        projects = GNS3Connection.list_all_projects(gns3_vm_ip, Settings.GNS3.PORT)
        project = next(
            (p for p in projects if p["name"] == app.state.project_name), None
        )
        if project is None:
            raise RuntimeError(
                f"No GNS3 project named '{app.state.project_name}' - has "
                f"this topology actually been deployed?"
            )
        app.state.project_id = project["project_id"]
    except Exception as exc:
        # Non-fatal: /topology (the static graph) still works without
        # live GNS3 state, so the app comes up in a degraded mode rather
        # than refusing to start entirely. /health surfaces this, and
        # /nodes returns a 503 with the same detail.
        app.state.startup_warning = str(exc)
        logger.warning(f"Starting without live GNS3 state: {exc}")

    yield


app = FastAPI(title="TopologyOperator", lifespan=lifespan)
app.include_router(console.router)
app.include_router(vm_console.router)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "esxi_ip": Settings.ESXI.IP,
        "gns3_vm_ip": app.state.gns3_vm_ip,
        "gns3_project_name": app.state.project_name,
        "gns3_project_id": app.state.project_id,
        "warning": app.state.startup_warning,
    }


@app.get("/topology")
def topology() -> list[dict[str, Any]]:
    """The topology file's own structure - no live GNS3/ESXi state."""
    return [
        {
            "name": node.name,
            "role": node.role,
            "image": node.image,
            "environment": node.env.name,
            "interfaces": {
                if_name: (
                    intf.neighbour.name if intf.neighbour is not None else None
                )
                for if_name, intf in node.interfaces.items()
            },
        }
        for node in app.state.graph.nodes.values()
    ]


@app.get("/nodes")
def nodes() -> list[dict[str, Any]]:
    """
    Topology nodes merged with live state: GNS3 status/console port+
    type for ON_GNS3 nodes, ESXi VM power state for ON_ESXI ones - two
    independent live sources, so a missing GNS3 project (see /health's
    own warning for that) no longer blocks ESXi status from coming
    back, unlike this endpoint's very first version.
    """
    try:
        live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    except NoLiveProject:
        live_by_name = {}

    result = []
    for node in app.state.graph.nodes.values():
        entry: dict[str, Any] = {
            "name": node.name,
            "role": node.role,
            "environment": node.env.name,
        }
        live = live_by_name.get(node.name)
        if live is not None:
            entry.update(
                {
                    "gns3_node_id": live.get("node_id"),
                    "status": live.get("status"),
                    "console_port": live.get("console"),
                    "console_type": live.get("console_type"),
                }
            )
        elif node.env == Environment.ON_ESXI:
            vm = app.state.esxi_connection.get_vm(node.name)
            entry["status"] = str(vm.runtime.powerState) if vm is not None else None
        result.append(entry)
    return result


@app.get("/nodes/{name}/ports")
def node_ports(name: str) -> list[dict[str, Any]]:
    """
    Real, live interface names for a GNS3 node - Add Link's own picker
    uses this instead of free text. ESXi VM interfaces have no
    equivalent live source, so those 404 here and the frontend falls
    back to free text for them.
    """
    try:
        return list_node_ports(app, name)
    except (ValueError, NoLiveProject) as exc:
        raise HTTPException(404, str(exc))


@app.get("/layout")
def layout() -> dict[str, dict[str, float]]:
    """
    Every node's canvas position - saved (from a prior drag) where
    available, otherwise a computed force-directed default, so the
    frontend always gets a full layout even before anyone has dragged
    anything. See app/layout.py.
    """
    return load_layout(app.state.graph.nodes)


class LayoutUpdate(BaseModel):
    name: str
    x: float
    y: float


@app.post("/layout")
def update_layout(update: LayoutUpdate) -> dict[str, str]:
    if update.name not in app.state.graph.nodes:
        raise HTTPException(404, f"no node named {update.name!r}")
    save_position(update.name, update.x, update.y)
    return {"status": "ok"}


@app.get("/templates")
def templates() -> dict[str, list[str]]:
    """
    Valid `image` names for POST /nodes, grouped by which backend they
    deploy to (GNS3 vs ESXi - see app/topology_write.py's own docstring:
    this is what actually decides a new node's environment, not its role).
    """
    try:
        return topology_write.list_templates()
    except TimeoutError as exc:
        raise HTTPException(503, str(exc))


class AddNodeRequest(BaseModel):
    name: str
    role: str
    image: str


@app.post("/nodes")
def add_node(request: AddNodeRequest) -> dict[str, str]:
    """
    Adds a node to the topology YAML and deploys it live. Slow (a real
    deploy call) and disruptive - briefly interrupts every already-
    running ESXi-bridged Cloud connection, even for this one node (see
    TopologyBackend.add_node's own docstring) - the frontend confirms
    this with the user before calling here.
    """
    try:
        topology_write.add_node(app, request.name, request.role, request.image)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (RuntimeError, ConnectionError, TimeoutError) as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.delete("/nodes/{name}")
def remove_node(name: str) -> dict[str, str]:
    try:
        topology_write.remove_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except (RuntimeError, ConnectionError, TimeoutError) as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


class AddLinkRequest(BaseModel):
    node1: str
    if1: str
    node2: str
    if2: str


@app.post("/links")
def add_link(request: AddLinkRequest) -> dict[str, str]:
    """Same deploy/disruption caveat as POST /nodes - see there."""
    try:
        topology_write.add_link(
            app, request.node1, request.if1, request.node2, request.if2
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (RuntimeError, ConnectionError, TimeoutError) as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.delete("/links/{node1}/{node2}")
def remove_link(node1: str, node2: str) -> dict[str, str]:
    try:
        topology_write.remove_link(app, node1, node2)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except (RuntimeError, ConnectionError, TimeoutError) as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.post("/nodes/{name}/start")
def start_node(name: str) -> dict[str, str]:
    try:
        lifecycle.start_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except (RuntimeError, ConnectionError, TimeoutError) as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.post("/nodes/{name}/stop")
def stop_node(name: str) -> dict[str, str]:
    try:
        lifecycle.stop_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except (RuntimeError, ConnectionError, TimeoutError) as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.post("/nodes/{name}/reload")
def reload_node(name: str) -> dict[str, str]:
    try:
        lifecycle.reload_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except (RuntimeError, ConnectionError, TimeoutError) as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


class PushRequest(BaseModel):
    devices: dict[str, list[str]]


@app.post("/config-push")
def config_push(request: PushRequest) -> list[DeviceResult]:
    """
    Pushes each device's raw command list over its auto-selected
    transport (Netmiko for GNS3 console devices, guest-ops for ESXi
    VMs). Stops a device's remaining commands on its first failure, but
    always processes every device in the request - see app/config_push.py.
    Synchronous on purpose: FastAPI runs a plain `def` route in its
    threadpool, so this doesn't block the event loop the console
    WebSocket proxy also needs.
    """
    return push_all(request.devices, app)


@app.post("/vm-console/{node_name}/prepare")
def prepare_vm_console(node_name: str) -> dict[str, Any]:
    """
    Ensures a network-backed serial console is added on this ESXi VM
    node. The first call for a given VM can take up to ~60s and power-
    cycles it - see app/vm_console.py. Also makes a best-effort attempt
    to configure the guest's own GRUB for `console=ttyS0` (needed for
    the guest to actually send anything there) - never fails the
    request if that can't be done. Call this before opening
    /ws/vm-console/{name}; that endpoint refuses to connect until the
    hardware/network part above has run at least once.
    """
    node = app.state.graph.nodes.get(node_name)
    if node is None or node.env != Environment.ON_ESXI:
        raise HTTPException(404, f"no ESXi VM node named {node_name!r}")
    vm = app.state.esxi_connection.get_vm(node_name)
    if vm is None:
        raise HTTPException(404, f"VM {node_name!r} not found on ESXi")
    try:
        _port, just_enabled = vm_console.ensure_serial_console(
            vm, app.state.vm_console_ports
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))

    # Best-effort, never fails the request - the hardware/network path
    # above is already the real value even if this can't run (Windows
    # images, missing guest credentials, guest tools not up yet, etc.).
    guest_configured = vm_console.ensure_guest_console_configured(
        vm, node.image, app.state.esxi_connection
    )
    return {"power_cycled": just_enabled, "guest_console_configured": guest_configured}


# Mounted last, deliberately - Starlette tries explicit routes (above) in
# registration order before falling through to a mount, so this catch-all
# for "/" can't shadow the API/WS routes as long as it's registered after
# them.
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")
