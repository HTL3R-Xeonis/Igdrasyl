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
from typing import Any, Literal

import requests
from fastapi import FastAPI, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel
from src.connections.esxi_connection import ESXiConnection
from src.graph.environment import Environment
from src.settings import Settings

from app import (
    capture,
    connection_settings,
    console,
    design,
    design_store,
    generate_jobs,
    gns3_vm_deploy_jobs,
    import_jobs,
    lifecycle,
    logs,
    maintenance,
    project_bundle,
    project_library,
    project_selection,
    settings_profiles,
    topology_write,
    vm_screen,
)
from app.config_push import DeviceResult, push_all
from app.gns3_state import (
    NoLiveProject,
    list_live_nodes,
    list_node_ports,
    reload_project,
    resolve_project_id,
)
from app.layout import load_layout, save_position
from app.logging_setup import configure_logging
from app.topology import load_graph, resolve_project_name

# Widened alongside (RuntimeError, ConnectionError, TimeoutError) on the
# live /nodes and /links endpoints below - a template-service outage
# surfaces as requests.exceptions.RequestException (e.g. ConnectionError,
# which is NOT the builtin ConnectionError), not just the three above.
_LIVE_WRITE_ERRORS = (
    RuntimeError,
    ConnectionError,
    TimeoutError,
    requests.exceptions.RequestException,
)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_LICENSE_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "LICENSE")

_SETTINGS_ENV_VAR = "TOPOLOGYOPERATOR_SETTINGS"


def _refresh_startup_state(app: FastAPI) -> None:
    """
    (Re)computes app.state.graph and the live GNS3 project resolution -
    called once from lifespan at process start, and again from
    POST /reload for a manual retry once a previously-down dependency
    (the template-listing service load_graph() needs, or the GNS3 VM/
    project itself) comes back, without needing a full process restart.

    Never raises: both the topology-file load and the GNS3 resolution
    are independently best-effort here (a template-service outage
    shouldn't hide a GNS3 VM being reachable, or vice versa) - failures
    are collected into app.state.startup_warning instead, same spirit as
    the GNS3-resolution-only try/except this replaces. See /topology,
    /nodes, /layout and GET /project/export, which all now check
    app.state.graph is None and 503 instead of crashing when this
    couldn't load it (previously an unhandled exception here failed the
    whole FastAPI lifespan, refusing to start the process at all - see
    handoff/INFRASTRUCTURE.md's "Template-listing API" outage note).
    """
    warnings: list[str] = []

    # No network dependency (just Settings.GNS3.PROJECT_NAME or the
    # topology file's own path stem) - resolved independently of
    # load_graph() below so a template-service outage there doesn't also
    # block the GNS3 project lookup that only needs this name.
    app.state.project_name = resolve_project_name()

    try:
        app.state.graph = load_graph()
    except Exception as exc:
        app.state.graph = None
        warnings.append(str(exc))
        logger.warning(f"Starting without a loaded topology: {exc}")

    app.state.gns3_vm_ip = None
    app.state.project_id = None
    try:
        # Auto-recovery: only when the configured GNS3 VM is genuinely
        # absent - never when one exists but just isn't reporting an IP
        # yet (e.g. still booting). See gns3_vm_deploy_jobs's own
        # docstring for the exact rule and why.
        gns3_vm_deploy_jobs.auto_deploy_if_missing(app)

        gns3_vm_ip = app.state.esxi_connection.get_vm_ip_address(
            Settings.ESXI.GNS3_VM_NAME
        )
        if gns3_vm_ip is None:
            raise RuntimeError(
                f"GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}' not found or has no "
                f"IP reported yet"
            )
        app.state.gns3_vm_ip = gns3_vm_ip

        project_id = (
            resolve_project_id(gns3_vm_ip, app.state.project_name)
            if app.state.project_name
            else None
        )
        if project_id is None and app.state.project_name:
            raise RuntimeError(
                f"No GNS3 project named '{app.state.project_name}' - has "
                f"this topology actually been deployed?"
            )
        app.state.project_id = project_id
    except Exception as exc:
        # Non-fatal: /topology (the static graph) still works without
        # live GNS3 state, so the app comes up in a degraded mode rather
        # than refusing to start entirely. /health surfaces this, and
        # /nodes returns a 503 with the same detail.
        warnings.append(str(exc))
        logger.warning(f"Starting without live GNS3 state: {exc}")

    app.state.startup_warning = "; ".join(warnings) or None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings_path = os.getenv(_SETTINGS_ENV_VAR)
    if settings_path:
        Settings.initialise_settings(settings_path)
    project_selection.initialise(settings_path)

    # After initialise_settings, so a settings.yml's own log_file_path
    # override (if any) is already in effect - see app/logging_setup.py.
    configure_logging()

    # Opens a real, persistent pyvmomi session - done once here and
    # reused for the app's lifetime rather than per-request, same
    # reasoning as TopologyBuilder's own CLI commands.
    app.state.esxi_connection = ESXiConnection(
        Settings.ESXI.IP,
        Settings.ESXI.PORT,
        Settings.ESXI.USERNAME,
        Settings.ESXI.PASSWORD,
    )

    # Most recent /config-push request body - project_bundle.py's own
    # GET /project/export includes this as the bundle's configs.json,
    # since nothing else persists config-push data anywhere.
    app.state.last_config_push = {}
    # Canvas drawings (rectangles/ellipses/lines/text annotations) -
    # client-side state the frontend pushes here via POST /drawings
    # whenever it changes, so a reload restores them and
    # project_bundle.py's own export can bundle them too. Not synced
    # with GNS3's own real drawings API - see app/static/index.html's
    # own notes on that.
    app.state.last_drawings = []

    # A separate, never-deployed sketch topology (see app/design.py),
    # reachable from the landing page independent of whichever host
    # profile this process happens to be running. Restored from disk
    # (see app/design_store.py) when a prior session saved one, so a
    # restart doesn't silently wipe an in-progress sketch - otherwise
    # cleared only by POST /design/reset. Never touches the template
    # service (see design.from_dict), so this always succeeds even
    # while that service is down.
    try:
        app.state.design = design_store.load() or design.new_state()
    except Exception as exc:
        logger.warning(f"Couldn't restore saved design sketch, starting empty: {exc}")
        app.state.design = design.new_state()
    app.state.design_project_id = None

    app.state.gns3_vm_deploy_job = None
    _refresh_startup_state(app)

    yield


app = FastAPI(title="TopologyOperator", lifespan=lifespan)
app.include_router(console.router)
app.include_router(vm_screen.router)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "topology_file": Settings.TOPOLOGY_FILE,
        "esxi_ip": Settings.ESXI.IP,
        "gns3_vm_ip": app.state.gns3_vm_ip,
        "gns3_port": Settings.GNS3.PORT,
        "gns3_project_name": app.state.project_name,
        "gns3_project_id": app.state.project_id,
        "warning": app.state.startup_warning,
        "incremental_deploy": Settings.INCREMENTAL_DEPLOY,
    }


@app.post("/reload")
def reload_startup_state() -> dict[str, Any]:
    """
    Manual retry for whatever _refresh_startup_state couldn't resolve at
    startup (template service was down, GNS3 VM/project not up yet) -
    the frontend's warning banner offers this instead of requiring a full
    process restart once the dependency is back. Always 200; the result
    is the same shape as GET /health so the frontend can refresh from it
    directly.
    """
    _refresh_startup_state(app)
    return health()


@app.get("/logs")
def get_logs(lines: int = 300) -> list[dict[str, Any]]:
    """
    The most recent `lines` entries from Settings.LOG_FILE_PATH,
    parsed for the View menu's Logs panel (see app/logs.py) - same log
    file and format TopologyBuilder's own `logs` CLI command reads,
    just with a more generous default line count for a scrollable GUI
    panel instead of a terminal.
    """
    return logs.tail_log_lines(lines)


@app.get("/topology")
def topology() -> list[dict[str, Any]]:
    """The topology file's own structure - no live GNS3/ESXi state."""
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
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
            vm = app.state.esxi_connection.get_vm(node.name)
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
        raise HTTPException(404, str(exc))


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


class LayoutUpdate(BaseModel):
    name: str
    x: float
    y: float


@app.post("/layout")
def update_layout(update: LayoutUpdate) -> dict[str, str]:
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    if update.name not in app.state.graph.nodes:
        raise HTTPException(404, f"no node named {update.name!r}")
    save_position(update.name, update.x, update.y)
    return {"status": "ok"}


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
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.delete("/nodes/{name}")
def remove_node(name: str) -> dict[str, str]:
    try:
        topology_write.remove_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
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
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.delete("/links/{node1}/{node2}")
def remove_link(node1: str, node2: str) -> dict[str, str]:
    try:
        topology_write.remove_link(app, node1, node2)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.get("/links")
def live_links() -> list[dict[str, Any]]:
    """Every live GNS3 link, for the canvas's link-actions popup to know which ones support capture. See app/capture.py."""
    try:
        return capture.list_live_links(app)
    except NoLiveProject:
        return []


@app.post("/links/{node1}/{node2}/capture/start")
def start_link_capture(node1: str, node2: str) -> dict[str, Any]:
    try:
        return capture.start_capture(app, node1, node2) or {"status": "ok"}
    except (NoLiveProject, ValueError) as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))


@app.post("/links/{node1}/{node2}/capture/stop")
def stop_link_capture(node1: str, node2: str) -> dict[str, str]:
    try:
        capture.stop_capture(app, node1, node2)
    except (NoLiveProject, ValueError) as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.get("/links/{node1}/{node2}/capture/stream")
def stream_link_capture(node1: str, node2: str) -> StreamingResponse:
    """
    Proxies GNS3's own live pcap stream for this link straight through -
    the same mechanism GNS3's desktop client pipes into a local
    `wireshark -k -i -` process (see app/capture.py's own docstring for
    why there's no separate "finished file" to download instead).
    """
    try:
        upstream = capture.capture_stream(app, node1, node2)
    except (NoLiveProject, ValueError) as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))
    return StreamingResponse(
        upstream.iter_content(chunk_size=4096),
        media_type="application/vnd.tcpdump.pcap",
        headers={"Content-Disposition": f'attachment; filename="{node1}_{node2}.pcap"'},
    )


@app.post("/nodes/{name}/start")
def start_node(name: str) -> dict[str, str]:
    try:
        lifecycle.start_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.post("/nodes/{name}/stop")
def stop_node(name: str) -> dict[str, str]:
    try:
        lifecycle.stop_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


@app.post("/nodes/{name}/reload")
def reload_node(name: str) -> dict[str, str]:
    try:
        lifecycle.reload_node(app, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
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
    app.state.last_config_push = request.devices
    return push_all(request.devices, app)


class DrawingsRequest(BaseModel):
    drawings: list[dict[str, Any]]


@app.get("/drawings")
def get_drawings() -> list[dict[str, Any]]:
    """
    Canvas annotations (rectangles/ellipses/lines/text) - purely
    client-side data this app just stores and hands back, so a page
    reload doesn't lose them. Not validated/interpreted server-side,
    and not synced with GNS3's own real drawings.
    """
    return app.state.last_drawings


@app.post("/drawings")
def set_drawings(request: DrawingsRequest) -> dict[str, str]:
    app.state.last_drawings = request.drawings
    return {"status": "ok"}


# --- Offline design canvas (app/design.py) - a never-deployed sketch
# topology, reachable from the landing page. Same shapes as the live
# endpoints above so the frontend's one canvas renderer covers both,
# but nothing here ever touches ESXi/GNS3 or Settings.TOPOLOGY_FILE.


@app.get("/design/templates")
def design_templates() -> dict[str, Any]:
    try:
        return topology_write.list_templates()
    except TimeoutError as exc:
        raise HTTPException(503, str(exc))


@app.get("/design/topology")
def design_topology() -> list[dict[str, Any]]:
    return design.to_topology_view(app.state.design)


@app.post("/design/nodes")
def design_add_node(request: AddNodeRequest) -> dict[str, str]:
    try:
        design.add_node(app.state.design, request.name, request.role, request.image)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        # Environment.get_environment couldn't reach the template
        # service - same degraded-mode story as GET /templates above,
        # not a 500: the sketch itself is untouched, only classifying a
        # brand new node needs that service to be up.
        raise HTTPException(503, str(exc))
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.delete("/design/nodes/{name}")
def design_remove_node(name: str) -> dict[str, str]:
    try:
        design.remove_node(app.state.design, name)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.post("/design/links")
def design_add_link(request: AddLinkRequest) -> dict[str, str]:
    try:
        design.add_link(
            app.state.design, request.node1, request.if1, request.node2, request.if2
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.delete("/design/links/{node1}/{node2}")
def design_remove_link(node1: str, node2: str) -> dict[str, str]:
    try:
        design.remove_link(app.state.design, node1, node2)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.get("/design/layout")
def design_layout() -> dict[str, dict[str, float]]:
    return app.state.design.layout


@app.post("/design/layout")
def design_update_layout(update: LayoutUpdate) -> dict[str, str]:
    if update.name not in app.state.design.nodes:
        raise HTTPException(404, f"no node named {update.name!r}")
    app.state.design.layout[update.name] = {"x": update.x, "y": update.y}
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.get("/design/drawings")
def design_get_drawings() -> list[dict[str, Any]]:
    return app.state.design.drawings


@app.post("/design/drawings")
def design_set_drawings(request: DrawingsRequest) -> dict[str, str]:
    app.state.design.drawings = request.drawings
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.post("/design/reset")
def design_reset() -> dict[str, str]:
    """Clears the sketch and starts a fresh, empty one."""
    app.state.design = design.new_state()
    app.state.design_project_id = None
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.get("/design/project")
def current_design_project() -> dict[str, Any]:
    project_id = getattr(app.state, "design_project_id", None)
    return {
        "project": _settings_call(project_library.get, project_id)
        if project_id
        else None
    }


@app.post("/design/project/{project_id}/open")
def open_library_project(project_id: str) -> dict[str, Any]:
    try:
        state = project_library.load_design(project_id)
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, f"Template service unavailable: {exc}") from exc
    app.state.design = state
    app.state.design_project_id = project_id
    design_store.save(state)
    return {"project": _settings_call(project_library.get, project_id)}


@app.put("/design/project/{project_id}")
def save_library_project(project_id: str) -> dict[str, Any]:
    project = _settings_call(project_library.save_design, project_id, app.state.design)
    app.state.design_project_id = project_id
    design_store.save(app.state.design)
    return {"project": project}


@app.get("/design/export")
def design_export() -> Response:
    """
    Downloads the sketch as a `.topoproj` bundle carrying no ESXi/GNS3
    target - importable later against whichever host is active via the
    existing POST /project/import.
    """
    data = project_bundle.build_design_export(app.state.design, "design")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="design.topoproj"'},
    )


class GenerateRequest(BaseModel):
    prompt: str


@app.post("/design/generate")
def generate_design(request: GenerateRequest) -> dict[str, str]:
    """
    Kicks off a background job that asks the Topology Generator API (an
    LLM-backed service, see src.connections.topology_generator_client)
    for a topology matching the given natural-language prompt, retrying
    on an invalid result the same way TopologyBuilder's own `generate`
    CLI command does - and on success, REPLACES the current design
    sketch with the generated one (see app/generate_jobs.py). Poll
    GET /design/generate/{job_id} for progress.
    """
    if not request.prompt.strip():
        raise HTTPException(400, "prompt must not be empty")
    job = generate_jobs.start_job(app, request.prompt)
    return {"job_id": job.id}


@app.get("/design/generate/{job_id}")
def generate_design_status(job_id: str) -> dict[str, Any]:
    """
    Poll target for POST /design/generate's background job. "status" is
    "running", "done", or "error". While running: "attempt"/
    "max_attempts" and any "warnings" the most recent attempt reported.
    Once "error": "error" (message).
    """
    jobs: dict[str, generate_jobs.GenerateJob] = getattr(app.state, "generate_jobs", {})
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no generate job {job_id!r}")

    response: dict[str, Any] = {
        "status": job.status,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "warnings": job.warnings,
    }
    if job.status == "error":
        response["error"] = job.error
    return response


@app.get("/project/export")
def export_project() -> Response:
    """
    Downloads the current topology as a `.topoproj` bundle (a plain
    zip - topology.yaml, a sanitized settings.yml, configs.json from
    the last config-push, drawings.json, layout.json (node canvas
    positions), and a manifest) - see app/project_bundle.py.
    """
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")
    data = project_bundle.build_export(app)
    filename = f"{app.state.project_name or 'topology'}.topoproj"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/project/reload-gns3")
def reload_gns3_project() -> dict[str, str]:
    """
    Closes and reopens the live GNS3 project - refreshes GNS3's own
    in-memory state for it (e.g. after changes made directly in GNS3's
    web UI) without redeploying anything. Doesn't touch ESXi VMs'
    actual power state, but does briefly stop every GNS3-native node
    (GNS3 itself stops everything on close) - restarted automatically
    once the project reopens, so the net effect is a refresh, not a
    lasting shutdown. See gns3_state.reload_project's own docstring for
    the real bug this fixes.
    """
    if app.state.gns3_vm_ip is None or app.state.project_id is None:
        raise HTTPException(
            503, app.state.startup_warning or "no live GNS3 project resolved"
        )
    try:
        reload_project(app.state.gns3_vm_ip, Settings.GNS3.PORT, app.state.project_id)
    except (requests.RequestException, TimeoutError) as exc:
        raise HTTPException(502, str(exc))
    return {"status": "ok"}


class VerifyRequest(BaseModel):
    fix: bool = False


class OrphanItem(BaseModel):
    type: Literal["gns3_node", "esxi_vm"]
    name: str
    reason: str = ""


class DeleteOrphansRequest(BaseModel):
    orphans: list[OrphanItem]


def _require_live_topology() -> None:
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")


@app.post("/maintenance/verify")
def maintenance_verify(request: VerifyRequest | None = None) -> dict[str, Any]:
    """
    Structural health check of the live lab against the topology file
    (the same checks as TopologyBuilder's `verify`). With fix=true the
    low-risk automatic repairs run first (start stopped GNS3 nodes, power
    on stopped ESXi VMs, create missing port groups) and the checks
    afterwards, so the result shows what is still wrong.
    """
    _require_live_topology()
    fix = request.fix if request is not None else False
    try:
        return maintenance.run_verify(app, fix)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))


@app.get("/maintenance/orphans")
def maintenance_orphans() -> dict[str, Any]:
    """
    Live GNS3 nodes and tool-created ESXi VMs that no node in the topology
    file describes anymore (what a partially failed live delete leaves
    behind). Read-only.
    """
    _require_live_topology()
    try:
        return {"orphans": maintenance.find_orphans(app)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))


@app.post("/maintenance/orphans/delete")
def maintenance_delete_orphans(request: DeleteOrphansRequest) -> dict[str, int]:
    """Permanently deletes the given orphans (irreversible)."""
    _require_live_topology()
    try:
        deleted = maintenance.delete_orphans(
            app, [orphan.model_dump() for orphan in request.orphans]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))
    return {"deleted": deleted}


class DeployGns3VmRequest(BaseModel):
    delete_old_vm: bool = False


@app.post("/gns3-vm/deploy")
def deploy_gns3_vm(request: DeployGns3VmRequest | None = None) -> dict[str, str]:
    """
    Kicks off a background job that replaces the configured GNS3 VM
    with a freshly imported one from gns3_vm_ova_path - the same action
    the app's own startup takes automatically when that VM is found
    completely missing, available here as an explicit, on-demand GUI
    action (e.g. to recover from a corrupted GNS3 VM without restarting
    this whole process). An existing same-named VM is always powered off
    and renamed as a timestamped backup first; request.delete_old_vm
    additionally deletes that backup permanently, but only once the new
    VM has confirmed itself alive - see
    src.gns3_vm_deploy.deploy_fresh_gns3_vm's own docstring for why that
    ordering matters. Poll GET /gns3-vm/deploy/{job_id} for progress.
    """
    delete_old_vm = request.delete_old_vm if request is not None else False
    try:
        job = gns3_vm_deploy_jobs.start_job(app, delete_old_vm=delete_old_vm)
    except gns3_vm_deploy_jobs.NotConfigured as exc:
        raise HTTPException(400, str(exc))
    except gns3_vm_deploy_jobs.AlreadyRunning as exc:
        raise HTTPException(409, str(exc))
    return {"job_id": job.id}


@app.get("/gns3-vm/deploy/{job_id}")
def gns3_vm_deploy_status(job_id: str) -> dict[str, Any]:
    """
    Poll target for POST /gns3-vm/deploy's background job. "status" is
    "running", "done", or "error". While running: "stage", a short
    human-readable step name (there's no real percentage available -
    see gns3_vm_deploy_jobs's own docstring). Once "done": "ip_address".
    Once "error": "error" (message).
    """
    job = getattr(app.state, "gns3_vm_deploy_job", None)
    if job is None or job.id != job_id:
        raise HTTPException(404, f"no GNS3 VM deploy job {job_id!r}")

    response: dict[str, Any] = {"status": job.status, "stage": job.stage}
    if job.status == "error":
        response["error"] = job.error
    elif job.status == "done":
        response["ip_address"] = job.ip_address
    return response


@app.post("/project/import")
async def import_project(file: UploadFile) -> dict[str, Any]:
    """
    Kicks off a `.topoproj` bundle's live deploy as a background job and
    returns immediately - the deploy itself (incrementally, same as
    POST /nodes/POST /links: reuses anything already live by name, never
    deletes anything not in the bundle, unless the bundle is a full
    non-incremental one) can take minutes, so the caller polls GET
    /project/import/{job_id} for progress and the final result instead
    of blocking on one long request. See app/import_jobs.py for why the
    reported progress is an approximation, not a real callback from the
    deploy itself.
    """
    if not Settings.TOPOLOGY_FILE:
        raise HTTPException(
            409, "Select a project for this host before importing a deployment"
        )
    data = await file.read()
    job = import_jobs.start_job(app, data)
    return {"job_id": job.id, "total": job.total}


@app.get("/project/import/{job_id}")
def import_job_status(job_id: str) -> dict[str, Any]:
    """
    Poll target for POST /project/import's background job.
    "status" is "running", "done", or "error". While running: "total"
    and "completed" (see app/import_jobs.py's own docstring on how
    "completed" is derived). Once "done": also carries "manifest",
    "configs", "drawings", "auto_config_push", and - when the bundle's
    own manifest.json enabled it - "push_results", exactly the same
    shape the old synchronous endpoint used to return directly. Once
    "error": also carries "error" (message) and "error_kind"
    ("bad_bundle" for a malformed/mistargeted bundle, mirroring the old
    400; "deploy_failed" for a live deploy failure, mirroring the old
    502) - the real topology file is left untouched on either.
    """
    jobs: dict[str, import_jobs.ImportJob] = getattr(app.state, "import_jobs", {})
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no import job {job_id!r}")

    completed = import_jobs.poll_progress(app, job)
    response: dict[str, Any] = {
        "status": job.status,
        "total": job.total,
        "completed": completed,
    }
    if job.status == "error":
        response["error"] = job.error
        response["error_kind"] = job.error_kind
    elif job.status == "done" and job.result is not None:
        response.update(job.result)
    return response


@app.get("/settings-profiles")
def list_settings_profiles() -> list[dict[str, Any]]:
    """
    Every uploaded settings.yml profile (see app/settings_profiles.py),
    each with a best-effort preview (esxi_ip/gns3_vm_name/topology_file/
    project_name - None or an "error" string if that particular file
    doesn't parse) and whether it's the one this running process was
    actually started with.
    """
    return settings_profiles.list_profiles()


@app.post("/settings-profiles")
async def upload_settings_profile(file: UploadFile) -> dict[str, str]:
    """Saves an uploaded settings.yml under its own filename - doesn't activate it."""
    data = await file.read()
    try:
        settings_profiles.save_profile(file.filename, data)
    except settings_profiles.InvalidProfileName as exc:
        raise HTTPException(400, str(exc))
    return {"status": "ok"}


class ConnectionSettingsUpdate(BaseModel):
    fields: dict[str, str]


def _settings_call(function, *args):
    try:
        return function(*args)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (TypeError, ValueError, settings_profiles.InvalidProfileName) as exc:
        raise HTTPException(400, str(exc)) from exc


class CreateLibraryProject(BaseModel):
    name: str
    topology_yaml: str | None = None


@app.get("/projects")
def list_library_projects():
    return {"projects": _settings_call(project_library.list_projects)}


@app.post("/projects", status_code=201)
def create_library_project(body: CreateLibraryProject):
    return _settings_call(project_library.create, body.name, body.topology_yaml)


@app.get("/projects/{project_id}/download")
def download_library_project(project_id: str):
    path = _settings_call(project_library.topology_path, project_id)
    return Response(
        path.read_bytes(),
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="topology.yaml"'},
    )


@app.get("/projects/{project_id}/deploy-preview/{filename}")
def preview_library_deployment(project_id: str, filename: str):
    return _settings_call(project_library.deployment_preview, project_id, filename)


@app.post("/projects/{project_id}/deploy/{filename}")
def deploy_library_project(project_id: str, filename: str):
    preview = _settings_call(project_library.deployment_preview, project_id, filename)
    if not preview["host"]["active"]:
        raise HTTPException(409, "Activate this host before deploying")
    if not preview["selected"]:
        raise HTTPException(
            409, "Select this project for the active host and restart before deploying"
        )
    try:
        state = project_library.load_design(project_id)
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, f"Template service unavailable: {exc}") from exc
    bundle = project_bundle.build_design_export(state, preview["project"]["name"])
    job = import_jobs.start_job(app, bundle)
    return {"job_id": job.id, "total": job.total}


class ProjectSelectionRequest(BaseModel):
    project_id: str | None = None
    topology_file: str = ""
    project_name: str = ""


@app.get("/settings-profiles/{filename}/project")
def get_host_project(filename: str):
    return _settings_call(project_selection.get_selection, filename)


@app.put("/settings-profiles/{filename}/project")
def select_host_project(filename: str, body: ProjectSelectionRequest):
    return _settings_call(
        project_selection.save_selection,
        filename,
        body.topology_file.strip(),
        body.project_name,
        body.project_id,
    )


@app.get("/host-config/schema")
def host_config_schema():
    return {"groups": connection_settings.schema()}


@app.post("/host-config/preview")
def preview_host_config(body: ConnectionSettingsUpdate):
    return {"yaml": _settings_call(connection_settings.render_config, body.fields)}


class CreateHostConfig(ConnectionSettingsUpdate):
    filename: str


@app.post("/host-config/create")
def create_host_config(body: CreateHostConfig):
    try:
        return _settings_call(
            connection_settings.create_profile, body.filename, body.fields
        )
    except FileExistsError as exc:
        raise HTTPException(
            409, "A host profile with that filename already exists"
        ) from exc


@app.post("/host-config/test")
def test_host_config(body: ConnectionSettingsUpdate):
    return {
        "results": _settings_call(
            connection_settings.test_connections, None, body.fields
        )
    }


@app.get("/settings-profiles/{filename}/connections")
def get_connection_settings(filename: str):
    return {"fields": _settings_call(connection_settings.read_fields, filename)}


@app.put("/settings-profiles/{filename}/connections")
def save_connection_settings(filename: str, body: ConnectionSettingsUpdate):
    _settings_call(connection_settings.update_fields, filename, body.fields)
    return {"status": "saved", "restart_required": True}


@app.post("/settings-profiles/{filename}/connections/test")
def test_connection_settings(filename: str, body: ConnectionSettingsUpdate):
    return {
        "results": _settings_call(
            connection_settings.test_connections,
            filename,
            body.fields,
            getattr(app.state, "gns3_vm_ip", None),
        )
    }


@app.delete("/settings-profiles/{filename}")
def delete_settings_profile(filename: str) -> dict[str, str]:
    try:
        settings_profiles.delete_profile(filename)
    except settings_profiles.InvalidProfileName as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    return {"status": "ok"}


@app.post("/settings-profiles/{filename}/activate")
def activate_settings_profile(filename: str) -> dict[str, str]:
    """
    Restarts this same server process against the given profile - see
    settings_profiles.restart_into_profile's own docstring for why this
    is a real process re-exec (same PID), not an in-place config swap,
    and why every open console/screen connection drops when it happens.
    """
    try:
        settings_profiles.restart_into_profile(filename)
    except settings_profiles.InvalidProfileName as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    return {"status": "restarting"}


@app.get("/license")
def get_license() -> Response:
    """
    The repo-root LICENSE file as plain text, read fresh on every
    request rather than duplicated into app/static - for the landing
    page's own "View License" link.
    """
    with open(_LICENSE_PATH) as file:
        return Response(content=file.read(), media_type="text/plain")


# Mounted last, deliberately - Starlette tries explicit routes (above) in
# registration order before falling through to a mount, so this catch-all
# for "/" can't shadow the API/WS routes as long as it's registered after
# them.
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")
