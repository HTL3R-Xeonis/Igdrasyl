"""
Impairments on a live link, the way GNS3's own GUI offers them: latency
(+ jitter), packet loss, corruption, and suspending the link - all through
the controller's link "filters"/"suspend" fields (PUT .../links/{link_id}),
which GNS3 applies to the running link without touching the nodes. Handy
for showing what a lossy or slow line does to a protocol.

Same precedent as app/capture.py: a TopologyOperator-only operational
concern, calling GNS3's own v2 endpoint directly rather than through
TopologyBuilder. Nothing here is stored by this app - the live link object
is the only source of truth, read back on every GET /links.
"""

from __future__ import annotations

from typing import Any

import requests
from fastapi import FastAPI
from loguru import logger
from src.connections.gns3_connection import GNS3Connection
from src.settings import Settings

_TIMEOUT_SECONDS = 15
MAX_MILLISECONDS = 10_000


def filters_from_options(
    delay_ms: int, jitter_ms: int, loss_percent: int, corrupt_percent: int
) -> dict[str, list[int]]:
    """
    GNS3's filter object for the given impairments - a filter that is off
    (0) is left out entirely, so "no impairments" is an empty object.
    :raises ValueError: a value outside its range
    """
    for label, value, maximum in (
        ("delay", delay_ms, MAX_MILLISECONDS),
        ("jitter", jitter_ms, MAX_MILLISECONDS),
        ("packet loss", loss_percent, 100),
        ("corruption", corrupt_percent, 100),
    ):
        if not 0 <= value <= maximum:
            raise ValueError(f"{label} must be between 0 and {maximum}")
    if jitter_ms and not delay_ms:
        raise ValueError("jitter needs a delay")
    filters: dict[str, list[int]] = {}
    if delay_ms:
        filters["delay"] = [delay_ms, jitter_ms]
    if loss_percent:
        filters["packet_loss"] = [loss_percent]
    if corrupt_percent:
        filters["corrupt"] = [corrupt_percent]
    return filters


def options_from_link(link: dict[str, Any]) -> dict[str, Any]:
    """The impairments currently set on a GNS3 link object, in this app's flat shape."""
    filters = link.get("filters") or {}
    delay = filters.get("delay") or [0, 0]
    return {
        "delay_ms": int(delay[0]) if delay else 0,
        "jitter_ms": int(delay[1]) if len(delay) > 1 else 0,
        "loss_percent": int((filters.get("packet_loss") or [0])[0]),
        "corrupt_percent": int((filters.get("corrupt") or [0])[0]),
        "suspended": bool(link.get("suspend")),
        # Filters this app has no control for (bpf, frequency_drop) - kept
        # visible so applying new options doesn't silently wipe them.
        "other_filters": sorted(set(filters) - {"delay", "packet_loss", "corrupt"}),
    }


def set_options(
    app: FastAPI,
    node1: str,
    node2: str,
    *,
    delay_ms: int,
    jitter_ms: int,
    loss_percent: int,
    corrupt_percent: int,
    suspended: bool,
) -> dict[str, Any]:
    """
    Applies the impairments to the live link between two nodes; anything
    off is cleared. Filters this app doesn't offer (bpf, frequency_drop)
    that are already on the link are preserved.
    :raises NoLiveProject: no live project resolved
    :raises ValueError: no live link between these nodes, or a value out of range
    :raises requests.exceptions.RequestException: GNS3 refused or didn't answer
    """
    # Imported here: app.capture itself uses this module's options_from_link.
    from app.capture import _base_url, _live_link

    filters = filters_from_options(delay_ms, jitter_ms, loss_percent, corrupt_percent)
    link = _live_link(app, node1, node2)
    for key, value in (link.get("filters") or {}).items():
        if key not in ("delay", "packet_loss", "corrupt"):
            filters[key] = value
    logger.info(
        f"Setting link options on {node1} <-> {node2}: delay {delay_ms}ms "
        f"(+-{jitter_ms}ms), loss {loss_percent}%, corruption {corrupt_percent}%, "
        f"suspended {suspended}"
    )
    GNS3Connection.ensure_project_open(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    response = requests.put(
        f"{_base_url(app)}/links/{link['link_id']}",
        json={"filters": filters, "suspend": suspended},
        timeout=_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return options_from_link(response.json())
