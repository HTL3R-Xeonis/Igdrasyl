"""The live lab: topology, nodes, layout, labels, notes, links, link capture, serial console, start/stop."""

from __future__ import annotations

from typing import Any

import requests
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from src.graph.environment import Environment

from app import (
    capture,
    design,
    esxi_serial,
    label_layout,
    lifecycle,
    link_options,
    node_credentials,
    node_notes,
    template_health,
    topology_write,
)
from app.application import app
from app.gns3_state import (
    NoLiveProject,
    list_live_nodes,
    list_node_ports,
)
from app.layout import load_layout, save_position, save_positions
from app.routes.common import (
    LIVE_READ_ERRORS,
    LIVE_WRITE_ERRORS,
    require_active_host,
)
from app.routes.models import (
    AddLinkRequest,
    AddNodeRequest,
    DrawingsRequest,
    LabelsUpdate,
    LayoutUpdate,
    NoteUpdate,
    PositionsUpdate,
    label_changes,
)


@app.get("/topology")
def topology() -> list[dict[str, Any]]:
    """The topology file's own structure - no live GNS3/ESXi state."""
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    edges = getattr(app.state.graph, "edges", None)
    if not isinstance(edges, (list, tuple)):
        edges = []
    return [
        {
            "name": node.name,
            "role": node.role,
            "image": node.image,
            "environment": node.env.name,
            "interfaces": {
                if_name: (intf.neighbour.name if intf.neighbour is not None else None)
                for if_name, intf in node.interfaces.items()
            },
            # every link incl. the interface on the far side, so two nodes linked more than
            # once over different interfaces can be told apart (interfaces above cannot)
            "links": design.node_links(edges, node.name),
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

    An ON_ESXI node's status always comes from the real ESXi VM lookup,
    never from a live GNS3 entry, even when one exists (its own
    `gns3_node_id` is still taken from there, when present, since
    that's still useful e.g. for delete). Real bug found live: an
    ESXi-hosted node also gets a GNS3-side Cloud node wrapper once
    linked into the topology (see TopologyBuilder's own
    _partially_link_gns3_nodes), and that wrapper's own "status" is
    just "started" for as long as it exists - it doesn't track the
    real VM's lifecycle at all. A VM destroyed independently of its
    Cloud node (e.g. by a non-incremental deploy that resets the ESXi
    vSwitch but leaves the Cloud node behind) used to still report
    "started" here, since a live entry existing at all short-circuited
    the real ESXi lookup entirely.
    """
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    try:
        live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    except NoLiveProject:
        live_by_name = {}
    except LIVE_READ_ERRORS as exc:
        # The GNS3 VM does not answer (powered off, still booting): the page polls this and falls back to "no live state"
        raise HTTPException(503, f"The GNS3 VM could not be reached: {exc}") from exc

    result = []
    for node in app.state.graph.nodes.values():
        entry: dict[str, Any] = {
            "name": node.name,
            "role": node.role,
            "environment": node.env.name,
        }
        live = live_by_name.get(node.name)
        if node.env == Environment.ON_ESXI:
            if live is not None:
                entry["gns3_node_id"] = live.get("node_id")
            try:
                vm = app.state.esxi_connection.get_vm(node.name)
            except LIVE_READ_ERRORS as exc:
                raise HTTPException(503, f"ESXi could not be reached: {exc}") from exc
            entry["status"] = str(vm.runtime.powerState) if vm is not None else None
            entry["console_port"] = None
            entry["console_type"] = None
        elif live is not None:
            entry.update(
                {
                    "gns3_node_id": live.get("node_id"),
                    "status": live.get("status"),
                    "console_port": live.get("console"),
                    "console_type": live.get("console_type"),
                }
            )
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
        raise HTTPException(404, str(exc)) from exc


@app.get("/layout")
def layout() -> dict[str, dict[str, float]]:
    """
    Every node's canvas position - saved (from a prior drag) where
    available, otherwise a computed force-directed default, so the
    frontend always gets a full layout even before anyone has dragged
    anything. See app/layout.py.
    """
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    return load_layout(app.state.graph.nodes)


@app.post("/layout")
def update_layout(update: LayoutUpdate) -> dict[str, str]:
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    if update.name not in app.state.graph.nodes:
        raise HTTPException(404, f"no node named {update.name!r}")
    save_position(update.name, update.x, update.y)
    return {"status": "ok"}


@app.post("/layout/batch")
def update_layout_batch(update: PositionsUpdate) -> dict[str, str]:
    """Moves several nodes at once (a group drag, or an undo of one) in a single write."""
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    unknown = [n for n in update.positions if n not in app.state.graph.nodes]
    if unknown:
        raise HTTPException(404, f"no node named {unknown[0]!r}")
    save_positions({n: {"x": p.x, "y": p.y} for n, p in update.positions.items()})
    return {"status": "ok"}


@app.get("/labels")
def labels() -> dict[str, dict[str, float]]:
    """Offsets of the labels the user has moved (everything else sits at its default spot). See app/label_layout.py."""
    return label_layout.load()


@app.post("/labels")
def update_labels(update: LabelsUpdate) -> dict[str, dict[str, float]]:
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    changes = label_changes(update, app.state.graph.nodes)
    try:
        return label_layout.save_changes(changes)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/notes")
def notes() -> dict[str, str]:
    """The notes the user keeps on nodes, by node name. See app/node_notes.py."""
    return node_notes.load()


@app.put("/notes/{name}")
def update_note(name: str, update: NoteUpdate) -> dict[str, str]:
    """Sets a node's note; blank text removes it."""
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    if name not in app.state.graph.nodes:
        raise HTTPException(404, f"no node named {name!r}")
    return node_notes.save_note(name, update.text)


@app.get("/nodes/{name}/credentials")
def node_default_credentials(name: str) -> dict[str, Any]:
    """The default login its template lists (ESXi: users/passwords, GNS3: the usage text), see app/node_credentials.py."""
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    node = app.state.graph.nodes.get(name)
    if node is None:
        raise HTTPException(404, f"no node named {name!r}")
    return node_credentials.for_image(node.image, str(node.env.name))


@app.get("/templates")
def templates() -> dict[str, Any]:
    """
    Valid `image` names for POST /nodes, grouped by which backend they
    deploy to (GNS3 vs ESXi - see app/topology_write.py's own docstring:
    this is what actually decides a new node's environment, not its role),
    plus `gns3_categories` - a best-effort name -> GNS3 category hint
    for the frontend's own Role filter (see list_templates's docstring).
    """
    try:
        return topology_write.list_templates()
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, template_health.describe(exc)) from exc


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
        raise HTTPException(400, template_health.explain(str(exc))) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.delete("/nodes/{name}")
def remove_node(name: str) -> dict[str, str]:
    try:
        topology_write.remove_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.post("/links")
def add_link(request: AddLinkRequest) -> dict[str, str]:
    """Same deploy/disruption caveat as POST /nodes - see there."""
    try:
        topology_write.add_link(
            app, request.node1, request.if1, request.node2, request.if2
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.delete("/links/{node1}/{node2}")
def remove_link(
    node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> dict[str, str]:
    """``if1``/``if2`` (interface on node1/node2) pick one of several links between the same two nodes."""
    try:
        topology_write.remove_link(app, node1, node2, if1, if2)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.get("/links")
def live_links() -> list[dict[str, Any]]:
    """Every live GNS3 link, for the canvas's link-actions popup to know which ones support capture. See app/capture.py."""
    try:
        return capture.list_live_links(app)
    except NoLiveProject:
        return []
    except LIVE_READ_ERRORS as exc:
        raise HTTPException(503, f"The GNS3 VM could not be reached: {exc}") from exc


class LinkOptions(BaseModel):
    delay_ms: int = 0
    jitter_ms: int = 0
    loss_percent: int = 0
    corrupt_percent: int = 0
    suspended: bool = False


@app.put("/links/{node1}/{node2}/options")
def set_link_options(
    node1: str,
    node2: str,
    options: LinkOptions,
    if1: str | None = None,
    if2: str | None = None,
) -> dict[str, Any]:
    """Latency/jitter, packet loss, corruption and suspend on a live link - see app/link_options.py."""
    try:
        link_options.filters_from_options(
            options.delay_ms,
            options.jitter_ms,
            options.loss_percent,
            options.corrupt_percent,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return link_options.set_options(
            app, node1, node2, if1=if1, if2=if2, **options.model_dump()
        )
    except (NoLiveProject, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/links/{node1}/{node2}/capture/start")
def start_link_capture(
    node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> dict[str, Any]:
    try:
        return capture.start_capture(app, node1, node2, if1, if2) or {"status": "ok"}
    except (NoLiveProject, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/links/{node1}/{node2}/capture/stop")
def stop_link_capture(
    node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> dict[str, str]:
    try:
        capture.stop_capture(app, node1, node2, if1, if2)
    except (NoLiveProject, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.get("/links/{node1}/{node2}/capture/stream")
def stream_link_capture(
    node1: str, node2: str, if1: str | None = None, if2: str | None = None
) -> StreamingResponse:
    """
    Proxies GNS3's own live pcap stream for this link straight through -
    the same mechanism GNS3's desktop client pipes into a local
    `wireshark -k -i -` process (see app/capture.py's own docstring for
    why there's no separate "finished file" to download instead).
    """
    try:
        upstream = capture.capture_stream(app, node1, node2, if1, if2)
    except (NoLiveProject, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return StreamingResponse(
        upstream.iter_content(chunk_size=4096),
        media_type="application/vnd.tcpdump.pcap",
        headers={
            "Content-Disposition": f'attachment; filename="{capture._capture_file_name(node1, node2, if1, if2)}"'
        },
    )


@app.get("/nodes/{name}/serial")
def serial_console_status(name: str) -> dict[str, Any]:
    """Whether an ESXi VM has a network serial port (and which), for the config push via serial."""
    require_active_host("serial console status")
    try:
        return esxi_serial.status(app, name)
    except esxi_serial.SerialSetupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/nodes/{name}/serial/enable")
def enable_serial_console(name: str) -> dict[str, Any]:
    """
    Adds a network serial port to an ESXi VM - powering it off and on again if it runs -
    and, for a Linux guest with VMware Tools, a login prompt on it. See app/esxi_serial.py.
    """
    require_active_host("enabling the serial console")
    try:
        return esxi_serial.enable(app, name)
    except esxi_serial.SerialSetupError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/nodes/{name}/start")
def start_node(name: str) -> dict[str, str]:
    try:
        lifecycle.start_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.post("/nodes/{name}/stop")
def stop_node(name: str) -> dict[str, str]:
    try:
        lifecycle.stop_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.post("/nodes/{name}/reload")
def reload_node(name: str) -> dict[str, str]:
    try:
        lifecycle.reload_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"status": "ok"}


@app.get("/drawings")
def get_drawings() -> list[dict[str, Any]]:
    """
    Canvas annotations (rectangles/ellipses/lines/text) - purely
    client-side data this app just stores and hands back, so a page
    reload doesn't lose them. Not validated/interpreted server-side,
    and not synced with GNS3's own real drawings.
    """
    return getattr(app.state, "last_drawings", [])


@app.post("/drawings")
def set_drawings(request: DrawingsRequest) -> dict[str, str]:
    app.state.last_drawings = request.drawings
    return {"status": "ok"}
