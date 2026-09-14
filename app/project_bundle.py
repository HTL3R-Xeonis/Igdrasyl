"""
Export/import a portable ".topoproj" project bundle - a plain zip
(same idea as .docx/.pptx/.jar: a real archive under its own
extension) holding the topology description, a sanitized settings
snapshot, and the last-pushed device configs. Import calls
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

import yaml
from fastapi import FastAPI

from src.settings import Settings

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
    }
    configs = getattr(app.state, "last_config_push", {})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("topology.yaml", topology_yaml)
        zf.writestr("settings.yml", yaml.safe_dump(settings_snapshot, sort_keys=False))
        zf.writestr("configs.json", json.dumps({"devices": configs}, indent=2))
    return buf.getvalue()


def apply_import(app: FastAPI, zip_bytes: bytes) -> dict[str, Any]:
    """
    :raises BundleValidationError: malformed bundle, wrong ESXi/GNS3 target, or invalid topology YAML.
    :raises RuntimeError: propagated from the live deploy on failure - the real topology file is left untouched.
    :returns: {"manifest": ..., "configs": {name: [cmd, ...]}} from the bundle.
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

    backend = get_or_create_backend(app)
    backend.deploy_topology(Settings.TOPOLOGY_FILE, topology_yaml_text)
    app.state.graph = load_graph()

    return {"manifest": manifest, "configs": configs.get("devices", {})}
