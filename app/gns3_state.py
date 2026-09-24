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
