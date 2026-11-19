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
only this backend (running in the lab network) is guaranteed to reach the
GNS3 VM.

Known upstream flakiness (confirmed live, not guessed): GNS3's own /pcap
endpoint intermittently never sends a response at all - the exact same
request that worked a moment earlier (verified with plain curl, not just
this app's own client) can hang indefinitely with zero bytes back, a
known class of issue in gns3-server's pcap streaming (e.g. GNS3/gns3-
server#2513). _STREAM_CONNECT_TIMEOUT_SECONDS below bounds how long a
hung attempt ties up a worker thread; it can't make GNS3's own endpoint
actually respond.
"""

from __future__ import annotations

import re
from typing import Any

import requests
from fastapi import FastAPI
from loguru import logger
from src.connections.gns3_connection import GNS3Connection
from src.settings import Settings

# How long to wait for GNS3's /pcap endpoint to respond at all before
# giving up - see this module's own docstring on its known flakiness.
# Applies to requests' initial connect+first-byte wait; a request that
# gets past that and is genuinely streaming has no further timeout, same
# as GNS3's own desktop client leaving a capture open indefinitely.
_STREAM_CONNECT_TIMEOUT_SECONDS = 15

from app import link_options
from app.gns3_state import list_live_nodes


def _port_name(node: dict[str, Any], endpoint: dict[str, Any]) -> str | None:
    for port in node.get("ports", []):
        if port.get("adapter_number") == endpoint.get("adapter_number") and port.get(
            "port_number"
        ) == endpoint.get("port_number"):
            return port.get("name")
    return None


def _live_link(
    app: FastAPI, node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> dict[str, Any]:
    """
    Resolves the real GNS3 link between two node *names* by
    cross-referencing list_live_nodes (name -> node_id) with
    GNS3Connection.list_project_links, the same join
    app/gns3_state.py's list_node_ports already does for ports.
    Two nodes can be linked more than once over different interfaces:
    ``if1``/``if2`` (the GNS3 port name on node1/node2, which equals the
    interface name in the topology file) pick one of them; a Cloud node's
    port name says nothing about the VM's interface and never narrows the
    choice down. Without interfaces the first link is used. If the
    interfaces match no link, nothing is guessed - a topology file can list
    a link GNS3 never got, and acting on the one that exists would hit
    the wrong link.
    :raises NoLiveProject: propagated from list_live_nodes - no live GNS3
        project resolved at all.
    :raises ValueError: either node isn't live right now, GNS3 has no
        link between them (e.g. one endpoint is an ESXi VM with no
        GNS3-side port to capture on), or the interfaces match no link.
    """
    live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    n1, n2 = live_by_name.get(node1), live_by_name.get(node2)
    if n1 is None or n2 is None:
        raise ValueError(f"no live GNS3 node for '{node1}' and/or '{node2}'")

    links = GNS3Connection.list_project_links(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    ids = {n1["node_id"], n2["node_id"]}
    candidates = [
        link
        for link in links
        if {endpoint["node_id"] for endpoint in link["nodes"]} == ids
    ]
    if not candidates:
        raise ValueError(f"no live link between '{node1}' and '{node2}'")
    if if1 is None and if2 is None:
        return candidates[0]

    def uses(link: dict[str, Any], node: dict[str, Any], interface: str | None) -> bool:
        # A Cloud node (the GNS3 side of an ESXi VM) names its port like the host NIC, not like the
        # VM's interface in the topology file, so its port name says nothing about the interface.
        if interface is None or node.get("node_type") == "cloud":
            return True
        endpoint = next(e for e in link["nodes"] if e["node_id"] == node["node_id"])
        name = _port_name(node, endpoint)
        return name is None or name == interface

    matching = [
        link for link in candidates if uses(link, n1, if1) and uses(link, n2, if2)
    ]
    if len(matching) == 1:
        return matching[0]
    raise ValueError(
        f"no single live link between '{node1}' and '{node2}' uses the interfaces {if1 or '*'} and {if2 or '*'}"
    )


def _capture_file_name(node1: str, node2: str, if1: str | None, if2: str | None) -> str:
    """One file per link, so two captures on parallel links between the same nodes do not overwrite each other."""
    parts = [node1] + ([if1] if if1 else []) + [node2] + ([if2] if if2 else [])
    return "_".join(part.replace("/", "-") for part in parts) + ".pcap"


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
    node_by_id = {n["node_id"]: n for n in live_nodes}
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
                "port1": _port_name(node_by_id[e1["node_id"]], e1),
                "port2": _port_name(node_by_id[e2["node_id"]], e2),
                "capturing": bool(link.get("capturing")),
                **link_options.options_from_link(link),
            }
        )
    return result


def _refusal_parts(exc: requests.HTTPError) -> tuple[Any, str]:
    """(status, GNS3's own explanation) of a refused call; the explanation is empty when the body has none."""
    response = exc.response
    if response is None:
        return "error", ""
    try:
        body = response.json()
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
    except ValueError:
        message = (response.text or "").strip()[:300]
    return response.status_code, message


def _controller_refusal(exc: requests.HTTPError) -> RuntimeError:
    """
    GNS3 explains a refusal in the body of its answer ({"message": ...}); requests' own text only says "409 Conflict for
    url ...", which hides the reason. The reason is what the person in front of the canvas needs.
    """
    status, message = _refusal_parts(exc)
    return RuntimeError(f"GNS3 refused ({status}): {message}" if message else str(exc))


# What GNS3 answers when its uBridge process has lost the bridge of a Cloud node (it was restarted, e.g. while a link was
# added to the running project): the bridge module is "unknown" to the fresh process.
_LOST_BRIDGE = re.compile(r"unknown module 'bridge'", re.IGNORECASE)


def _restart_cloud_nodes(app: FastAPI, link: dict[str, Any]) -> list[str]:
    """
    Stops and starts the Cloud nodes at the ends of a link, which makes GNS3 build their bridges again. A Cloud node has no
    state of its own (the machine behind it is on ESXi), so this only interrupts that one VM's connection for a moment -
    unlike reloading the project, which restarts every router and loses what is not saved on it. Returns their names.
    """
    live = {n["node_id"]: n for n in list_live_nodes(app)}
    clouds = [
        live[end["node_id"]]
        for end in link["nodes"]
        if live.get(end["node_id"], {}).get("node_type") == "cloud"
    ]
    for node in clouds:
        GNS3Connection.stop_node(
            app.state.gns3_vm_ip,
            Settings.GNS3.PORT,
            app.state.project_id,
            node["node_id"],
        )
        GNS3Connection.start_node(
            app.state.gns3_vm_ip,
            Settings.GNS3.PORT,
            app.state.project_id,
            node["node_id"],
        )
    return [node["name"] for node in clouds]


def start_capture(
    app: FastAPI, node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> dict[str, Any]:
    link = _live_link(app, node1, node2, if1, if2)
    logger.info(f"Starting a packet capture on {node1} <-> {node2}")
    # GNS3's own controller state can silently drift from "opened" back
    # to "closed" independently of whether the project's nodes are still
    # running (confirmed live, not guessed - see GNS3Connection.
    # ensure_project_open's own docstring) - every mutating per-link call
    # below then fails outright with a real 403 "The project is not
    # opened" otherwise. A no-op when already open, the common case.
    GNS3Connection.ensure_project_open(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    url = f"{_base_url(app)}/links/{link['link_id']}/start_capture"
    body = {
        "capture_file_name": _capture_file_name(node1, node2, if1, if2),
        "data_link_type": "DLT_EN10MB",
    }
    try:
        return GNS3Connection.post(url, json=body)
    except requests.HTTPError as exc:
        _, message = _refusal_parts(exc)
        if not _LOST_BRIDGE.search(message):
            raise _controller_refusal(exc) from exc
        # known, repairable state of GNS3: restart the Cloud node(s) of this link and try once more
        restarted = _restart_cloud_nodes(app, link)
        if not restarted:
            raise _controller_refusal(exc) from exc
        logger.warning(
            f"GNS3 lost the bridge of {', '.join(restarted)}; restarted it and trying the capture again"
        )
    try:
        return GNS3Connection.post(url, json=body)
    except requests.HTTPError as exc:
        refusal = _controller_refusal(exc)
        raise RuntimeError(
            f"{refusal} (the Cloud node {', '.join(restarted)} was restarted once and it still fails - "
            "Reload project in the menu resets GNS3's bridges for the whole project)"
        ) from exc


def stop_capture(
    app: FastAPI, node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> None:
    link = _live_link(app, node1, node2, if1, if2)
    logger.info(f"Stopping the packet capture on {node1} <-> {node2}")
    GNS3Connection.ensure_project_open(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id
    )
    try:
        GNS3Connection.post(f"{_base_url(app)}/links/{link['link_id']}/stop_capture")
    except requests.HTTPError as exc:
        raise _controller_refusal(exc) from exc


def capture_stream(
    app: FastAPI, node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> requests.Response:
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
    link = _live_link(app, node1, node2, if1, if2)
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
