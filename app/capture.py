"""
Wireshark-style packet capture on a live link, the same mechanism GNS3's
own GUI uses: GNS3Connection.post/get straight against the controller's
own v2 API (start_capture/stop_capture/capture/stream) - not wrapped by
TopologyBuilder's GNS3Connection since this is a TopologyOperator-only
operational concern, same precedent app/gns3_state.py's reload_project
already sets for endpoints like this.

There's no separate "finished capture file" endpoint - GET .../pcap *is*
the mechanism (a live, never-ending pcap byte stream for as long as the
capture runs), the same one GNS3's own desktop client pipes into a local
`wireshark -k -i -` process. Confirmed against the real controller (GNS3
2.2.61): the endpoint is called .../links/{link_id}/pcap on this version,
NOT .../capture/stream - that path (from GNS3's own v3 docs/some search
results) 404s here; GNS3 2.2's own endpoint reference
(api.gns3.net/en/2.2/endpoints.html) confirms /pcap for this branch.
Proxied through this app's own GET /links/{node1}/{node2}/capture/stream
(app/main.py) rather than handing the browser a direct GNS3-VM URL, since
only this backend (running on testbox) is guaranteed to reach the GNS3
VM - see handoff/INFRASTRUCTURE.md's "Known access-path limits".

Known upstream flakiness (confirmed live, not guessed): GNS3's own /pcap
endpoint intermittently never sends a response at all - the exact same
request that worked a moment earlier (verified with plain curl, not just
this app's own client) can hang indefinitely with zero bytes back, a
known class of issue in gns3-server's pcap streaming (e.g. GNS3/gns3-
server#2513). _STREAM_CONNECT_TIMEOUT_SECONDS below bounds how long a
hung attempt ties up a worker thread; it can't make GNS3's own endpoint
actually respond. See handoff/INFRASTRUCTURE.md for the operator-facing
note.
"""

from __future__ import annotations

from typing import Any

import requests
from fastapi import FastAPI
from src.connections.gns3_connection import GNS3Connection
from src.settings import Settings

# How long to wait for GNS3's /pcap endpoint to respond at all before
# giving up - see this module's own docstring on its known flakiness.
# Applies to requests' initial connect+first-byte wait; a request that
# gets past that and is genuinely streaming has no further timeout, same
# as GNS3's own desktop client leaving a capture open indefinitely.
_STREAM_CONNECT_TIMEOUT_SECONDS = 15

from app.gns3_state import list_live_nodes


def _live_link(app: FastAPI, node1: str, node2: str) -> dict[str, Any]:
    """
    Resolves the real GNS3 link between two node *names* - same
    node-name-pair addressing GET/DELETE /links/{node1}/{node2} already
    use (app/topology_write.py) - by cross-referencing list_live_nodes
    (name -> node_id) with GNS3Connection.list_project_links, the same
    join app/gns3_state.py's list_node_ports already does for ports.
    :raises NoLiveProject: propagated from list_live_nodes - no live GNS3
        project resolved at all.
    :raises ValueError: either node isn't live right now, or GNS3 has no
        link between them (e.g. one endpoint is an ESXi VM with no
        GNS3-side port to capture on).
    """
    live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    n1, n2 = live_by_name.get(node1), live_by_name.get(node2)
    if n1 is None or n2 is None:
        raise ValueError(f"no live GNS3 node for '{node1}' and/or '{node2}'")

    links = GNS3Connection.list_project_links(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    ids = {n1["node_id"], n2["node_id"]}
    for link in links:
        if {endpoint["node_id"] for endpoint in link["nodes"]} == ids:
            return link
    raise ValueError(f"no live link between '{node1}' and '{node2}'")


def _base_url(app: FastAPI) -> str:
    return f"http://{app.state.gns3_vm_ip}:{Settings.GNS3.PORT}/v2/projects/{app.state.project_id}"


def list_live_links(app: FastAPI) -> list[dict[str, Any]]:
    """
    Every live link's {node1, if1, node2, if2, capturing} -  "capturing"
    is read straight off GNS3's own link object each call, never tracked
    separately here, same "never cache what the live system already
    knows" spirit as GET /nodes's ESXi-status handling.
    :raises NoLiveProject: propagated from list_live_nodes.
    """
    live_nodes = list_live_nodes(app)
    name_by_id = {n["node_id"]: n["name"] for n in live_nodes}
    links = GNS3Connection.list_project_links(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )

    result = []
    for link in links:
        endpoints = link["nodes"]
        if len(endpoints) != 2:
            continue
        (e1, e2) = endpoints
        n1, n2 = name_by_id.get(e1["node_id"]), name_by_id.get(e2["node_id"])
        if n1 is None or n2 is None:
            continue
        result.append(
            {
                "node1": n1,
                "if1": e1.get("label", {}).get("text", ""),
                "node2": n2,
                "if2": e2.get("label", {}).get("text", ""),
                "capturing": bool(link.get("capturing")),
            }
        )
    return result


def start_capture(app: FastAPI, node1: str, node2: str) -> dict[str, Any]:
    link = _live_link(app, node1, node2)
    # GNS3's own controller state can silently drift from "opened" back
    # to "closed" independently of whether the project's nodes are still
    # running (confirmed live, not guessed - see GNS3Connection.
    # ensure_project_open's own docstring) - every mutating per-link call
    # below then fails outright with a real 403 "The project is not
    # opened" otherwise. A no-op when already open, the common case.
    GNS3Connection.ensure_project_open(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    return GNS3Connection.post(
        f"{_base_url(app)}/links/{link['link_id']}/start_capture",
        json={
            "capture_file_name": f"{node1}_{node2}.pcap",
            "data_link_type": "DLT_EN10MB",
        },
    )


def stop_capture(app: FastAPI, node1: str, node2: str) -> None:
    link = _live_link(app, node1, node2)
    GNS3Connection.ensure_project_open(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    GNS3Connection.post(f"{_base_url(app)}/links/{link['link_id']}/stop_capture")


def capture_stream(app: FastAPI, node1: str, node2: str) -> requests.Response:
    """
    The raw, still-open response for the live pcap stream - returned
    unread (stream=True) so app/main.py's endpoint can pipe it straight
    through to the browser without buffering the whole (open-ended)
    capture in memory. Not routed through APIHandler.get, which always
    fully reads the response and parses it as JSON.
    :raises requests.exceptions.RequestException: connection/HTTP failure,
        including a requests.exceptions.Timeout if GNS3's endpoint hangs
        past _STREAM_CONNECT_TIMEOUT_SECONDS - see this module's own
        docstring on that endpoint's known flakiness.
    """
    link = _live_link(app, node1, node2)
    GNS3Connection.ensure_project_open(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    response = requests.get(
        f"{_base_url(app)}/links/{link['link_id']}/pcap",
        stream=True,
        timeout=_STREAM_CONNECT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response
