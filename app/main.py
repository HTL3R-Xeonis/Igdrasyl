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
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import requests
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from src.connections.esxi_connection import ESXiConnection
from src.graph.environment import Environment
from src.settings import Settings
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import (
    capture,
    connection_settings,
    console,
    design,
    design_history,
    design_store,
    error_pages,
    esxi_serial,
    generate_jobs,
    gns3_vm_deploy_jobs,
    holds,
    import_jobs,
    lifecycle,
    link_options,
    live_lock,
    log_store,
    logging_setup,
    logs,
    maintenance,
    ova_templates,
    profile_secrets,
    project_bundle,
    project_library,
    project_selection,
    public_text,
    push_jobs,
    settings_profiles,
    startup_retry,
    template_health,
    topology_write,
    vm_screen,
)
from app.config_push import DeviceResult, DeviceSpec, push_all
from app.gns3_state import (
    NoLiveProject,
    list_live_nodes,
    list_node_ports,
    reload_project,
    resolve_project_id,
)
from app.layout import load_layout, save_position, save_positions
from app.logging_setup import configure_logging
from app.request_logging import RequestLoggingMiddleware
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


def _refresh_startup_state(app: FastAPI, allow_auto_deploy: bool = True) -> None:
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
        warnings.append(template_health.explain(str(exc)))
        logger.warning(f"Starting without a loaded topology: {exc}")

    app.state.gns3_vm_ip = None
    app.state.project_id = None
    try:
        # Auto-recovery: only when the configured GNS3 VM is genuinely
        # absent - never when one exists but just isn't reporting an IP
        # yet (e.g. still booting). See gns3_vm_deploy_jobs's own
        # docstring for the exact rule and why.
        if allow_auto_deploy:
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

        if holds.active_hold(app.state.project_name, _active_host_filename()):
            # Parked on purpose (see app/holds.py): its GNS3 project is closed
            # and nothing is wrong - the banner offers Resume.
            raise RuntimeError(
                f"Project '{app.state.project_name}' is on hold - resume it to "
                f"start it again."
            )
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


def _retry_startup_state(app: FastAPI) -> bool:
    """One background attempt to heal the startup warning; False when the lab was busy with another change."""
    with live_lock.try_exclusive() as got:
        if not got:
            return False
        # No auto-deploy here: replacing the GNS3 VM stays a decision of the startup / the user.
        _refresh_startup_state(app, allow_auto_deploy=False)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings_path = os.getenv(_SETTINGS_ENV_VAR)
    if settings_path:
        Settings.initialise_settings(settings_path)
        stored_secrets = profile_secrets.apply(Path(settings_path).name)
    else:
        stored_secrets = []
    ova_templates.initialise(settings_path)
    project_selection.initialise(settings_path)

    # After initialise_settings, so a settings.yml's own log_file_path
    # override (if any) is already in effect - see app/logging_setup.py.
    configure_logging()
    if stored_secrets:
        # Names only - never the values.
        logger.info(f"Using stored credentials for: {', '.join(stored_secrets)}")

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

    # Whatever was missing at startup (GNS3 VM still booting, template service down) is
    # retried in the background until the warning is gone - nobody has to press Retry.
    stop_retry = threading.Event()
    startup_retry.start(
        lambda: bool(app.state.startup_warning) and _held_project() is None,
        lambda: _retry_startup_state(app),
        stop_retry,
    )

    yield

    stop_retry.set()


app = FastAPI(title="TopologyOperator", lifespan=lifespan)
app.add_middleware(RequestLoggingMiddleware)
app.include_router(console.router)
app.include_router(vm_screen.router)


@app.exception_handler(StarletteHTTPException)
async def path_safe_http_error(request: Request, exc: StarletteHTTPException):
    # Covers FastAPI's HTTPException and Starlette's own (unknown route,
    # wrong method, missing static file). Browsers navigating get a page.
    if error_pages.wants_html(request):
        return error_pages.render(
            exc.status_code, public_text.redact_paths(str(exc.detail))
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": public_text.redact_paths(exc.detail)},
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unexpected_error(request: Request, _exc: Exception):
    # Already logged with its traceback by RequestLoggingMiddleware.
    if error_pages.wants_html(request):
        return error_pages.render(500)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


def _active_host_filename() -> str | None:
    settings_path = os.getenv(_SETTINGS_ENV_VAR)
    return Path(settings_path).name if settings_path else None


def _held_project() -> dict[str, Any] | None:
    """The finished hold of the active host's selected project, for the banner's Resume button."""
    try:
        return holds.active_hold(app.state.project_name, _active_host_filename())
    except holds.HoldError:
        return None


def _deployable_project() -> dict[str, str] | None:
    """
    The library project selected for the active host when it has not been
    deployed to GNS3 yet (the GNS3 VM answers, but has no project of that
    name) - what the warning banner's Deploy button offers to deploy. None
    in every other state.
    """
    settings_path = os.getenv(_SETTINGS_ENV_VAR)
    if (
        not settings_path
        or not app.state.project_name
        or app.state.project_id is not None
        or not app.state.gns3_vm_ip
    ):
        return None
    if _held_project() is not None:
        return None  # parked, not missing - the banner offers Resume instead
    filename = Path(settings_path).name
    try:
        selection = project_selection.get_selection(filename)
    except (FileNotFoundError, ValueError, settings_profiles.InvalidProfileName):
        return None
    if not selection.get("project_id"):
        return None
    return {
        "id": selection["project_id"],
        "name": selection.get("project_name") or app.state.project_name,
        "filename": filename,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "deployable_project": _deployable_project(),
        "held_project": _held_project(),
        "esxi_ip": Settings.ESXI.IP,
        "gns3_vm_ip": app.state.gns3_vm_ip,
        "gns3_port": Settings.GNS3.PORT,
        "gns3_project_name": app.state.project_name,
        "gns3_project_id": app.state.project_id,
        "warning": public_text.redact_paths(app.state.startup_warning),
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


@app.get("/logs/entries")
def get_log_entries(
    lines: int = 500, level: str | None = None, search: str | None = None
) -> dict[str, Any]:
    """
    The most recent `lines` log entries (a traceback stays one entry),
    optionally only at/above `level` and containing `search`, plus how
    full the log is - for the standalone log page (/logs.html). Server
    file paths are redacted, same as everything else sent to a browser.
    """
    lines = max(1, min(lines, log_store.MAX_TOTAL_LINES))
    return {
        "entries": [
            {**entry, "message": public_text.redact_log_text(entry["message"])}
            for entry in log_store.read_entries(
                Settings.LOG_FILE_PATH, lines, level, search
            )
        ],
        "info": log_store.info(Settings.LOG_FILE_PATH),
    }


@app.delete("/logs")
def clear_logs() -> dict[str, str]:
    """Permanently deletes every retained log line (both rotation files)."""
    logging_setup.clear_logs()
    return {"status": "cleared"}


@app.get("/logs/download")
def download_logs() -> StreamingResponse:
    """The whole retained log (at most 100 000 lines) as plain text, paths redacted."""
    return StreamingResponse(
        (
            public_text.redact_log_text(line) + "\n"
            for line in log_store.iter_lines(Settings.LOG_FILE_PATH)
        ),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="topologyoperator.log"'},
    )


@app.get("/templates/health")
def templates_health() -> dict[str, Any]:
    """
    Whether the template service (which TopologyBuilder asks what images
    exist) works - see app/template_health.py. Cheap and cached for a few
    seconds, the UI polls it.
    """
    return public_text.redact_paths(template_health.check())


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


class LayoutPosition(BaseModel):
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


class PositionsUpdate(BaseModel):
    positions: dict[str, LayoutPosition]


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
        raise HTTPException(400, template_health.explain(str(exc)))
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


class LinkOptions(BaseModel):
    delay_ms: int = 0
    jitter_ms: int = 0
    loss_percent: int = 0
    corrupt_percent: int = 0
    suspended: bool = False


@app.put("/links/{node1}/{node2}/options")
def set_link_options(node1: str, node2: str, options: LinkOptions) -> dict[str, Any]:
    """Latency/jitter, packet loss, corruption and suspend on a live link - see app/link_options.py."""
    try:
        link_options.filters_from_options(
            options.delay_ms,
            options.jitter_ms,
            options.loss_percent,
            options.corrupt_percent,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        return link_options.set_options(app, node1, node2, **options.model_dump())
    except (NoLiveProject, ValueError) as exc:
        raise HTTPException(404, str(exc))
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc))


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


@app.get("/nodes/{name}/serial")
def serial_console_status(name: str) -> dict[str, Any]:
    """Whether an ESXi VM has a network serial port (and which), for the config push via serial."""
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
    try:
        return esxi_serial.enable(app, name)
    except esxi_serial.SerialSetupError as exc:
        raise HTTPException(409, str(exc)) from exc
    except _LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc


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
    devices: dict[str, list[str] | DeviceSpec]
    # How long a job keeps retrying devices that are still booting; omitted = 15 minutes, 0 = once.
    wait_seconds: int | None = Field(default=None, ge=0, le=3600)


@app.post("/config-push/jobs", status_code=202)
def start_config_push_job(request: PushRequest) -> dict[str, Any]:
    """
    Starts a push in the background and returns at once - use this for anything long
    (up to 10 000 commands per device; see app/push_jobs.py). Poll
    GET /config-push/jobs/{job_id}.
    """
    try:
        job = push_jobs.start_job(
            app, request.devices, wait_seconds=request.wait_seconds
        )
    except push_jobs.AlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job.id, "total_commands": job.total_commands}


@app.get("/config-push/jobs/{job_id}")
def config_push_job_status(job_id: str) -> dict[str, Any]:
    """Progress per device while running; the per-device results once done."""
    job = push_jobs.get_job(app, job_id)
    if job is None:
        raise HTTPException(404, f"no config push job {job_id!r}")
    response = job.progress()
    if job.status == "done" and job.results is not None:
        response["results"] = [r.model_dump() for r in job.results]
    if job.status == "error":
        response["error"] = job.error
    return response


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
        with design_history.step(app):
            design.add_node(app.state.design, request.name, request.role, request.image)
    except ValueError as exc:
        raise HTTPException(400, template_health.explain(str(exc)))
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
        with design_history.step(app):
            design.remove_node(app.state.design, name)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.post("/design/links")
def design_add_link(request: AddLinkRequest) -> dict[str, str]:
    try:
        with design_history.step(app):
            design.add_link(
                app.state.design,
                request.node1,
                request.if1,
                request.node2,
                request.if2,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.delete("/design/links/{node1}/{node2}")
def design_remove_link(node1: str, node2: str) -> dict[str, str]:
    try:
        with design_history.step(app):
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
    with design_history.step(app):
        app.state.design.layout[update.name] = {"x": update.x, "y": update.y}
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.post("/design/layout/batch")
def design_update_layout_batch(update: PositionsUpdate) -> dict[str, str]:
    """Moves several nodes at once - one undo step for the whole group move."""
    unknown = [n for n in update.positions if n not in app.state.design.nodes]
    if unknown:
        raise HTTPException(404, f"no node named {unknown[0]!r}")
    with design_history.step(app):
        for name, pos in update.positions.items():
            app.state.design.layout[name] = {"x": pos.x, "y": pos.y}
    design_store.save(app.state.design)
    return {"status": "ok"}


class DesignDelete(BaseModel):
    nodes: list[str] = []
    links: list[tuple[str, str]] = []


@app.post("/design/delete")
def design_delete(request: DesignDelete) -> dict[str, str]:
    """Deletes a selection (nodes and/or links) as one undo step; unknown names change nothing."""
    try:
        with design_history.step(app):
            design.remove_many(app.state.design, request.nodes, request.links)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    design_store.save(app.state.design)
    return {"status": "ok"}


class PastedNode(BaseModel):
    name: str
    role: str
    image: str
    x: float
    y: float


class DesignPaste(BaseModel):
    nodes: list[PastedNode]
    links: list[AddLinkRequest] = []


@app.post("/design/paste")
def design_paste(request: DesignPaste) -> dict[str, str]:
    """Adds copied nodes and the links between them as one undo step; all or nothing."""
    try:
        with design_history.step(app):
            design.paste(
                app.state.design,
                [node.model_dump() for node in request.nodes],
                [(l.node1, l.if1, l.node2, l.if2) for l in request.links],
            )
    except ValueError as exc:
        raise HTTPException(400, template_health.explain(str(exc)))
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, str(exc))
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.get("/design/history")
def design_history_status() -> dict[str, bool]:
    return design_history.status(app)


@app.post("/design/undo")
def design_undo() -> dict[str, bool]:
    if not design_history.undo(app):
        raise HTTPException(409, "nothing to undo")
    return design_history.status(app)


@app.post("/design/redo")
def design_redo() -> dict[str, bool]:
    if not design_history.redo(app):
        raise HTTPException(409, "nothing to redo")
    return design_history.status(app)


@app.get("/design/drawings")
def design_get_drawings() -> list[dict[str, Any]]:
    return app.state.design.drawings


@app.post("/design/drawings")
def design_set_drawings(request: DrawingsRequest) -> dict[str, str]:
    with design_history.step(app):
        app.state.design.drawings = request.drawings
    design_store.save(app.state.design)
    return {"status": "ok"}


@app.post("/design/reset")
def design_reset() -> dict[str, str]:
    """Clears the sketch and starts a fresh, empty one."""
    with design_history.step(app):
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
    with design_history.step(app):
        app.state.design = state
    app.state.design_project_id = project_id
    design_store.save(state)
    return {"project": _settings_call(project_library.get, project_id)}


class SaveDesignAs(BaseModel):
    name: str


@app.post("/design/project", status_code=201)
def create_library_project_from_design(request: SaveDesignAs) -> dict[str, Any]:
    """Saves the current sketch as a NEW library project under the given name."""
    project = _settings_call(
        project_library.create_from_design, request.name, app.state.design
    )
    app.state.design_project_id = project["id"]
    return {"project": project}


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
        response["error"] = public_text.redact_paths(job.error)
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
    fix_vlan_subinterfaces: bool = False


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
    afterwards, so the result shows what is still wrong. Re-creating the
    GNS3 VM's missing VLAN subinterfaces is a separate flag
    (fix_vlan_subinterfaces) because it restarts the Cloud nodes.
    """
    _require_live_topology()
    fix = request.fix if request is not None else False
    fix_subinterfaces = request.fix_vlan_subinterfaces if request is not None else False
    try:
        return maintenance.run_verify(app, fix, fix_subinterfaces)
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
    with a freshly imported one from the selected OVA template resource - the same action
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
        response["error"] = public_text.redact_paths(job.error)
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
        response["error"] = public_text.redact_paths(job.error)
        response["error_kind"] = job.error_kind
    elif job.status == "done" and job.result is not None:
        response.update(job.result)
    return response


@app.get("/settings-profiles")
def list_settings_profiles() -> list[dict[str, Any]]:
    """
    Every uploaded settings.yml profile (see app/settings_profiles.py),
    each with a path-free preview and whether it is active.
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
    # Write-only: entered passwords (see app/profile_secrets.py). Blank or
    # absent keeps what is stored; no endpoint ever returns a value.
    secrets: dict[str, str] = {}


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
    projects = _settings_call(project_library.list_projects)
    try:
        held = holds.all_entries()
    except holds.HoldError:
        held = {}
    return {
        "projects": [{**project, "held": project["id"] in held} for project in projects]
    }


@app.delete("/projects/{project_id}")
def delete_library_project(project_id: str) -> dict[str, str]:
    """
    Removes a project from the library. Refused while a host has it selected or
    it is on hold; never touches GNS3 or ESXi - whatever was deployed stays.
    """
    _settings_call(project_library.get, project_id)  # 404/400 first
    if holds.entry(project_id) is not None:
        raise HTTPException(
            409, "This project is on hold - resume it first, then delete it."
        )
    hosts = project_selection.hosts_using(project_id)
    if hosts:
        raise HTTPException(
            409,
            "This project is selected for host(s) "
            + ", ".join(hosts)
            + " - select another project there first.",
        )
    _settings_call(project_library.delete, project_id)
    if getattr(app.state, "design_project_id", None) == project_id:
        app.state.design_project_id = None
    logger.info(f"Deleted library project {project_id}")
    return {"status": "deleted"}


def _hold_error(exc: Exception) -> HTTPException:
    if isinstance(exc, holds.HoldError):
        return HTTPException(409, str(exc))
    return HTTPException(502, public_text.redact_paths(str(exc)))


@app.post("/project/hold")
def hold_active_project() -> dict[str, Any]:
    """
    Parks the active host's live project (see app/holds.py): VMs off and
    protected, GNS3 project closed, everything kept for a later resume.
    """
    filename = _active_host_filename()
    selection = (project_selection.get_selection(filename) if filename else {}) or {}
    project_id = selection.get("project_id")
    if not filename or not project_id:
        raise HTTPException(
            409, "Only a library project selected for the active host can be held."
        )
    try:
        return {"hold": holds.hold_project(app, project_id, filename)}
    except (holds.HoldError, *_LIVE_WRITE_ERRORS) as exc:
        raise _hold_error(exc) from exc


@app.post("/projects/{project_id}/resume")
def resume_held_project(project_id: str) -> dict[str, Any]:
    try:
        report = holds.resume_project(app, project_id)
    except (holds.HoldError, *_LIVE_WRITE_ERRORS) as exc:
        raise _hold_error(exc) from exc
    response: dict[str, Any] = {"status": "resumed", "report": report}
    # A project that wants its configs pushed after a deploy wants them after a
    # resume too (the VMs/nodes were off, and a router may have lost its running config).
    try:
        saved = project_library.get_configs(project_id)
    except (ValueError, FileNotFoundError):
        saved = {"devices": {}, "auto_push": False}
    if saved["auto_push"] and saved["devices"]:
        try:
            # In the background (it can be long, and devices may still be booting):
            # the client follows it through GET /config-push/jobs/{id}.
            job = push_jobs.start_job(app, saved["devices"])
            response["push_job_id"] = job.id
        except push_jobs.AlreadyRunning:
            response["push_job_error"] = "another config push is still running"
    return response


@app.post("/projects", status_code=201)
def create_library_project(body: CreateLibraryProject):
    return _settings_call(project_library.create, body.name, body.topology_yaml)


class ProjectConfigs(BaseModel):
    devices: dict[str, list[str] | DeviceSpec] = {}
    auto_push: bool = False


@app.get("/projects/{project_id}/configs")
def get_library_project_configs(project_id: str) -> dict[str, Any]:
    """The device command lists saved with a library project (see project_library.save_configs)."""
    return _settings_call(project_library.get_configs, project_id)


@app.put("/projects/{project_id}/configs")
def put_library_project_configs(
    project_id: str, body: ProjectConfigs
) -> dict[str, Any]:
    devices = {
        name: entry.model_dump(exclude_none=True)
        if isinstance(entry, DeviceSpec)
        else entry
        for name, entry in body.devices.items()
    }
    return _settings_call(
        project_library.save_configs, project_id, devices, body.auto_push
    )


def _active_project_id() -> str:
    filename = _active_host_filename()
    selection = (project_selection.get_selection(filename) if filename else {}) or {}
    if not selection.get("project_id"):
        raise HTTPException(409, "Select a library project for the active host first.")
    return selection["project_id"]


@app.get("/project/configs")
def get_active_project_configs() -> dict[str, Any]:
    """Same as GET /projects/{id}/configs, for the project the active host has selected."""
    return get_library_project_configs(_active_project_id())


@app.put("/project/configs")
def put_active_project_configs(body: ProjectConfigs) -> dict[str, Any]:
    return put_library_project_configs(_active_project_id(), body)


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
    preview = _settings_call(project_library.deployment_preview, project_id, filename)
    return {**preview, "held": holds.entry(project_id) is not None}


@app.post("/projects/{project_id}/deploy/{filename}")
def deploy_library_project(project_id: str, filename: str):
    preview = _settings_call(project_library.deployment_preview, project_id, filename)
    if not preview["host"]["active"]:
        raise HTTPException(409, "Activate this host before deploying")
    if not preview["selected"]:
        raise HTTPException(
            409, "Select this project for the active host and restart before deploying"
        )
    if holds.entry(project_id) is not None:
        raise HTTPException(
            409,
            "This project is on hold - resume it instead of deploying it again "
            "(its VMs and GNS3 project are kept).",
        )
    clash = holds.held_gns3_names(exclude_project_id=project_id).get(
        app.state.project_name
    )
    if clash is not None:
        raise HTTPException(
            409,
            "A project on hold already uses this GNS3 project name - rename one "
            "of them, or resume the held one.",
        )
    try:
        state = project_library.load_design(project_id)
    except (TimeoutError, RuntimeError, requests.exceptions.RequestException) as exc:
        raise HTTPException(503, f"Template service unavailable: {exc}") from exc
    saved = _settings_call(project_library.get_configs, project_id)
    bundle = project_bundle.build_design_export(
        state,
        preview["project"]["name"],
        configs=saved["devices"],
        auto_config_push=saved["auto_push"],
    )
    job = import_jobs.start_job(app, bundle)
    return {"job_id": job.id, "total": job.total}


class ProjectSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    project_name: str = ""


@app.get("/settings-profiles/{filename}/project")
def get_host_project(filename: str):
    return _settings_call(project_selection.get_selection, filename)


@app.put("/settings-profiles/{filename}/project")
def select_host_project(filename: str, body: ProjectSelectionRequest):
    return _settings_call(
        project_selection.save_selection,
        filename,
        body.project_name,
        body.project_id,
    )


@app.get("/host-config/schema")
def host_config_schema():
    return {"groups": connection_settings.schema()}


@app.get("/ova-templates")
def list_ova_templates():
    try:
        return {"templates": ova_templates.list_public()}
    except (RuntimeError, TypeError) as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/host-config/preview")
def preview_host_config(body: ConnectionSettingsUpdate):
    return {"yaml": _settings_call(connection_settings.render_config, body.fields)}


class CreateHostConfig(ConnectionSettingsUpdate):
    filename: str


@app.post("/host-config/create")
def create_host_config(body: CreateHostConfig):
    _settings_call(profile_secrets.validate, body.secrets)
    try:
        created = _settings_call(
            connection_settings.create_profile, body.filename, body.fields
        )
    except FileExistsError as exc:
        raise HTTPException(
            409, "A host profile with that filename already exists"
        ) from exc
    _settings_call(profile_secrets.save, body.filename, body.secrets)
    return created


@app.post("/host-config/test")
def test_host_config(body: ConnectionSettingsUpdate):
    return {
        "results": _settings_call(
            connection_settings.test_connections, None, body.fields
        )
    }


@app.get("/settings-profiles/{filename}/connections")
def get_connection_settings(filename: str):
    return {
        "fields": _settings_call(connection_settings.read_fields, filename),
        "passwords_set": _settings_call(profile_secrets.status, filename),
    }


@app.put("/settings-profiles/{filename}/connections")
def save_connection_settings(filename: str, body: ConnectionSettingsUpdate):
    _settings_call(profile_secrets.validate, body.secrets)
    _settings_call(connection_settings.update_fields, filename, body.fields)
    _settings_call(profile_secrets.save, filename, body.secrets)
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
        profile_secrets.delete(filename)
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
