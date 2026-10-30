"""
Shared helper for anything needing live GNS3 project state - both the
/nodes HTTP endpoint and the console WebSocket proxy need "the current
list of live GNS3 nodes, or a clear error if no project is resolved yet",
so it's factored out here instead of duplicated in both places.
"""

from __future__ import annotations

from typing import Any

import requests
from fastapi import FastAPI
from src.connections.gns3_connection import GNS3Connection
from src.settings import Settings


class NoLiveProject(RuntimeError):
    """Raised when app.state has no resolved GNS3 project to query yet."""


def resolve_project_id(gns3_vm_ip: str, project_name: str) -> str | None:
    """
    The current GNS3 project id for `project_name`, or None if no such
    project exists right now. Same lookup `main.py`'s lifespan does once
    at startup, but callable again later - a non-incremental deploy can
    delete and recreate the whole GNS3 project (not just its nodes)
    under a brand new id, even though the name is unchanged, so a
    project_id cached at startup can go stale mid-run (confirmed live:
    the cached id 404s on every GNS3 call afterward). See
    project_bundle.apply_import, which re-resolves it after every
    deploy for exactly this reason.
    """
    projects = GNS3Connection.list_all_projects(gns3_vm_ip, Settings.GNS3.PORT)
    project = next((p for p in projects if p["name"] == project_name), None)
    return project["project_id"] if project is not None else None


def compute_load(gns3_vm_ip: str, port: int) -> dict[str, float | None] | None:
    """
    What the GNS3 server itself reports about its load ({"cpu": %, "memory": %}), None if it
    cannot be asked. A GNS3 VM with too few vCPUs runs at 100 % while router/switch images
    boot - which is why their consoles stay silent for hours.
    """
    try:
        compute = GNS3Connection.get(f"http://{gns3_vm_ip}:{port}/v2/computes/local")
    except Exception:
        return None
    return {
        "cpu": compute.get("cpu_usage_percent"),
        "memory": compute.get("memory_usage_percent"),
    }


def reload_project(gns3_vm_ip: str, port: int, project_id: str) -> None:
    """
    Closes then reopens the GNS3 project - refreshes the controller's
    own in-memory state for it (e.g. picking up changes made directly
    in GNS3's own web UI, or recovering from a project stuck in a
    half-loaded state) without touching ESXi or redeploying anything.
    Real GNS3 v2 API endpoints (.../close, .../open) - not wrapped by
    TopologyBuilder's own GNS3Connection as a named method, since this
    is purely an operational/TopologyOperator concern, called directly
    here via its inherited (static) APIHandler.post.

    GNS3 itself stops every node as part of closing a project, and does
    NOT restart them on reopen - confirmed live: close() left every
    node, including ESXi-hosted ones' Cloud-node wrappers, reporting
    status None/stopped. So this remembers which nodes were actually
    started beforehand and restarts exactly those after reopening,
    otherwise "reload" would silently power off the whole topology
    instead of just refreshing GNS3's state. ESXi VMs are unaffected
    either way - their real power state lives on ESXi, never on GNS3's
    own Cloud-node status field (see /nodes's own docstring on why that
    field is never trusted here) - restarting their Cloud-node wrapper
    is a no-op for the underlying VM. A single node that fails to
    restart doesn't stop the rest from being attempted.
    :raises requests.HTTPError: propagated from close/open.
    :raises TimeoutError: propagated from close/open.
    """
    base = f"http://{gns3_vm_ip}:{port}/v2/projects/{project_id}"
    nodes_before = GNS3Connection.list_project_nodes(gns3_vm_ip, port, project_id)
    started_node_ids = [
        n["node_id"] for n in nodes_before if n.get("status") == "started"
    ]

    GNS3Connection.post(f"{base}/close")
    GNS3Connection.post(f"{base}/open")

    for node_id in started_node_ids:
        try:
            GNS3Connection.start_node(gns3_vm_ip, port, project_id, node_id)
        except (requests.RequestException, TimeoutError, RuntimeError):
            pass


def list_live_nodes(app: FastAPI) -> list[dict[str, Any]]:
    if app.state.project_id is None:
        raise NoLiveProject(
            app.state.startup_warning or "no live GNS3 project resolved"
        )
    try:
        return GNS3Connection.list_project_nodes(
            app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
        )
    except requests.HTTPError as exc:
        # A cached project_id that no longer exists on the GNS3 server
        # (see resolve_project_id's docstring) 404s here - degrade the
        # same as "no project resolved yet" instead of a raw 500, same
        # spirit as this module's whole reason to exist (a GNS3 hiccup
        # shouldn't be an unhandled crash for callers - see /nodes's own
        # docstring on ESXi status staying independent of this).
        if exc.response is not None and exc.response.status_code == 404:
            raise NoLiveProject(
                f"GNS3 project {app.state.project_id!r} no longer exists"
            ) from exc
        raise


def list_node_ports(app: FastAPI, name: str) -> list[dict[str, Any]]:
    """
    Every port GNS3 knows about for this live node - name plus whether
    it's already connected to another node - for Add Link's own
    interface picker to enumerate real options instead of free text.
    ESXi VM interfaces have no live GNS3-side equivalent - not this
    function's concern, callers 404 for those.
    :raises ValueError: if the node isn't live on GNS3 right now.
    """
    live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    node = live_by_name.get(name)
    if node is None or "ports" not in node:
        raise ValueError(f"node {name!r} has no live GNS3 port information")

    links = GNS3Connection.list_project_links(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    used = {
        (endpoint["node_id"], endpoint["adapter_number"], endpoint["port_number"])
        for link in links
        for endpoint in link["nodes"]
        if endpoint["node_id"] == node["node_id"]
    }

    return [
        {
            "name": port["name"],
            "in_use": (node["node_id"], port["adapter_number"], port["port_number"])
            in used,
        }
        for port in node["ports"]
    ]
