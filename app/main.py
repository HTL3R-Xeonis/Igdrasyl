"""
TopologyOperator's FastAPI backend - Phase 0/1 read-only foundation.
Everything else (GNS3 console proxy, config push, embedded VM screens)
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

from src.connections.esxi_connection import ESXiConnection
from src.connections.gns3_connection import GNS3Connection
from src.graph.environment import Environment
from src.settings import Settings

from app import console
from app.gns3_state import NoLiveProject, list_live_nodes
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
    Topology nodes merged with live GNS3 state (status, console port/
    type - GNS3's node API already returns these on every call, just
    never surfaced anywhere until now). ESXi-hosted VM nodes are
    included with only their static info for now - live VM power state/
    guest info isn't wired up yet, that's a separate piece of work.
    """
    try:
        live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    except NoLiveProject as exc:
        raise HTTPException(status_code=503, detail=str(exc))

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
            entry["status"] = None
        result.append(entry)
    return result


# Mounted last, deliberately - Starlette tries explicit routes (above) in
# registration order before falling through to a mount, so this catch-all
# for "/" can't shadow the API/WS routes as long as it's registered after
# them.
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")
