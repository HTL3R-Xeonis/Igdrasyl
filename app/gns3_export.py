"""Stream GNS3's native project archive through the Operator."""

from __future__ import annotations

from urllib.parse import quote

import requests
from fastapi import FastAPI
from src.settings import Settings

from app.gns3_state import NoLiveProject

_STREAM_TIMEOUT_SECONDS = 15


def export_stream(app: FastAPI) -> requests.Response:
    """Open the native GNS3 export without buffering it. The caller owns the response."""
    host = getattr(app.state, "gns3_vm_ip", None)
    project_id = getattr(app.state, "project_id", None)
    if not host or not project_id:
        raise NoLiveProject("No live GNS3 project selected.")
    project = quote(project_id, safe="")
    response = requests.get(
        f"http://{host}:{Settings.GNS3.PORT}/v2/projects/{project}/export",
        stream=True,
        timeout=_STREAM_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if response.status_code != 200:
        response.close()
        raise requests.HTTPError("GNS3 did not return an archive", response=response)
    return response
