"""
Export/import a portable ".topoproj" project bundle - a plain zip
(same idea as .docx/.pptx/.jar: a real archive under its own
extension) holding the topology description, a sanitized settings
snapshot, the last-pushed device configs, canvas drawings, and node
canvas positions (layout.json - see app/layout.py). Import calls
TopologyBackend.deploy_topology (TopologyBuilder) to actually deploy
the bundled topology live, incrementally - see that method's own
docstring for its validate-before-write guarantee. Kept in its own
module, independently testable, same separation main.py already uses
for config_push.py/topology_write.py.

The settings snapshot is never auto-applied - Settings is a
process-global, load-once class (see TopologyBuilder's own
Settings.initialise_settings and this app's own lifespan), so there's
no runtime-safe way to repoint a running server at different
infrastructure anyway. Its only job here is a safety check: refuse to
import a bundle built for a different ESXi host/GNS3 VM than the one
this server is actually configured for.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

import requests
import yaml
from fastapi import FastAPI

from src.settings import Settings

from app.gns3_state import resolve_project_id
from app.layout import apply_imported_layout, load_layout
from app.topology import load_graph
from app.topology_write import get_or_create_backend

_FORMAT_VERSION = 1
_REQUIRED_MEMBERS = {"manifest.json", "topology.yaml", "settings.yml", "configs.json"}


class BundleValidationError(ValueError):
    """The uploaded file isn't a usable .topoproj bundle - never reaches TopologyBuilder at all."""


def build_export(app: FastAPI) -> bytes:
    with open(Settings.TOPOLOGY_FILE) as file:
        topology_yaml = file.read()

    settings_snapshot = {
        "esxi": {"ip": Settings.ESXI.IP, "gns3_vm_name": Settings.ESXI.GNS3_VM_NAME},
        "gns3": {"username": Settings.GNS3.USERNAME, "port": Settings.GNS3.PORT},
    }
    manifest = {
        "format_version": _FORMAT_VERSION,
        "project_name": app.state.project_name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        # Whether a future POST /project/import of *this* bundle should
        # also run its own configs.json through config-push right after
        # the deploy succeeds, instead of only loading it into the
        # Config Push panel for a human to review and push manually.
        # False here (a live export always starts from this default) -
        # edit the exported file's manifest.json, or set it when
        # building a bundle by hand, to opt a specific bundle in. Lives
        # here rather than in settings.yml on purpose: it's a property
        # of this project file (this exact set of commands, already
        # trusted enough to bundle), not a standing server policy.
        "auto_config_push": False,
    }
    configs = getattr(app.state, "last_config_push", {})
    drawings = getattr(app.state, "last_drawings", [])
    layout = load_layout(app.state.graph.nodes)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("topology.yaml", topology_yaml)
        zf.writestr("settings.yml", yaml.safe_dump(settings_snapshot, sort_keys=False))
        zf.writestr("configs.json", json.dumps({"devices": configs}, indent=2))
        zf.writestr("drawings.json", json.dumps({"drawings": drawings}, indent=2))
        zf.writestr("layout.json", json.dumps({"layout": layout}, indent=2))
    return buf.getvalue()


def apply_import(app: FastAPI, zip_bytes: bytes) -> dict[str, Any]:
    """
    :raises BundleValidationError: malformed bundle, wrong ESXi/GNS3 target, or invalid topology YAML.
    :raises RuntimeError: propagated from the live deploy on failure - the real topology file is left untouched.
    :returns: {"manifest": ..., "configs": {name: [cmd, ...]}, "drawings": [...], "auto_config_push": bool}
        from the bundle - the caller (main.py's POST /project/import) is responsible for actually running
        config-push when auto_config_push is true, this function only deploys the topology itself. Node
        canvas positions are applied as a side effect (written to the local layout file, same as a drag
        via POST /layout) rather than returned - the caller reloads them via GET /layout.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise BundleValidationError("not a valid .topoproj (zip) file")

    missing = _REQUIRED_MEMBERS - set(zf.namelist())
    if missing:
        raise BundleValidationError(f"bundle is missing: {', '.join(sorted(missing))}")

    try:
        manifest = json.loads(zf.read("manifest.json"))
    except json.JSONDecodeError as exc:
        raise BundleValidationError(f"manifest.json isn't valid JSON: {exc}")
    if manifest.get("format_version") != _FORMAT_VERSION:
        raise BundleValidationError(
            f"unsupported bundle format_version {manifest.get('format_version')!r}"
        )

    try:
        bundled_settings = yaml.safe_load(zf.read("settings.yml")) or {}
    except yaml.YAMLError as exc:
        raise BundleValidationError(f"settings.yml isn't valid YAML: {exc}")
    bundled_esxi = bundled_settings.get("esxi", {})
    if (
        bundled_esxi.get("ip") != Settings.ESXI.IP
        or bundled_esxi.get("gns3_vm_name") != Settings.ESXI.GNS3_VM_NAME
    ):
        raise BundleValidationError(
            f"this bundle targets ESXi {bundled_esxi.get('ip')!r}/"
            f"{bundled_esxi.get('gns3_vm_name')!r}, but this server is "
            f"configured for {Settings.ESXI.IP!r}/{Settings.ESXI.GNS3_VM_NAME!r} - refusing to import"
        )

    topology_yaml_text = zf.read("topology.yaml").decode()
    try:
        configs = json.loads(zf.read("configs.json"))
    except json.JSONDecodeError as exc:
        raise BundleValidationError(f"configs.json isn't valid JSON: {exc}")

    # Optional members - bundles exported before these existed won't have
    # them, and that's fine, not a validation failure.
    drawings = []
    if "drawings.json" in zf.namelist():
        try:
            drawings = json.loads(zf.read("drawings.json")).get("drawings", [])
        except json.JSONDecodeError as exc:
            raise BundleValidationError(f"drawings.json isn't valid JSON: {exc}")

    layout = {}
    if "layout.json" in zf.namelist():
        try:
            layout = json.loads(zf.read("layout.json")).get("layout", {})
        except json.JSONDecodeError as exc:
            raise BundleValidationError(f"layout.json isn't valid JSON: {exc}")

    backend = get_or_create_backend(app)
    backend.deploy_topology(Settings.TOPOLOGY_FILE, topology_yaml_text)
    app.state.graph = load_graph()
    app.state.last_drawings = drawings

    # A non-incremental deploy can delete and recreate the whole GNS3
    # project (not just its nodes) under a new id, even with the same
    # name - confirmed live: the server's project_id, cached once at
    # startup, then 404s on every GNS3 call until the process is
    # restarted. Re-resolve it here so that doesn't happen. Best-effort:
    # if the GNS3 VM itself is unreachable right now, keep the old id -
    # /nodes degrades gracefully rather than 500ing either way (see
    # gns3_state.list_live_nodes).
    if app.state.gns3_vm_ip is not None:
        try:
            app.state.project_id = resolve_project_id(app.state.gns3_vm_ip, app.state.project_name)
            # Same staleness bug as project_id itself: startup_warning is
            # otherwise set once in lifespan and never revisited, so a
            # perfectly healthy import still leaves /health (and the
            # frontend's warning banner) stuck showing whatever failure
            # reason applied at process start - e.g. "no GNS3 project
            # named X yet" from before this exact import created it.
            if app.state.project_id is not None:
                app.state.startup_warning = None
        except (requests.RequestException, TimeoutError):
            pass

    # Every post-deploy node gets a position: the bundle's own value
    # where it gave one, otherwise a freshly computed default - never
    # whatever a previous, structurally different topology happened to
    # leave behind for a reused node name (see apply_imported_layout's
    # own docstring for the real bug this fixes).
    apply_imported_layout(app.state.graph.nodes, layout)

    return {
        "manifest": manifest,
        "configs": configs.get("devices", {}),
        "drawings": drawings,
        "auto_config_push": bool(manifest.get("auto_config_push", False)),
    }
