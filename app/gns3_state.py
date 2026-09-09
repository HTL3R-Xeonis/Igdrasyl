"""
Shared helper for anything needing live GNS3 project state - both the
/nodes HTTP endpoint and the console WebSocket proxy need "the current
list of live GNS3 nodes, or a clear error if no project is resolved yet",
so it's factored out here instead of duplicated in both places.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from src.connections.gns3_connection import GNS3Connection
from src.settings import Settings


class NoLiveProject(RuntimeError):
    """Raised when app.state has no resolved GNS3 project to query yet."""


def list_live_nodes(app: FastAPI) -> list[dict[str, Any]]:
    if app.state.project_id is None:
        raise NoLiveProject(
            app.state.startup_warning or "no live GNS3 project resolved"
        )
    return GNS3Connection.list_project_nodes(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
