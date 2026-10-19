"""
"Hold": park a deployed project instead of deleting it, and bring it back later.

Holding a project powers off its ESXi VMs, closes its GNS3 project (GNS3 stops
every node as part of closing) and makes the VMs invisible to every later
deployment. Resuming reverses that: VMs get their names back, the GNS3 project
opens again and the same fix pass `verify --fix` runs starts the nodes, powers
on the VMs and recreates port groups a deployment of another project may have
removed in the meantime.

How the VMs are protected - without any change to TopologyBuilder: it only ever
deletes VMs whose annotation STARTS with "topologybuilder-image:"
(VMOrchestrator.delete_unused_vms, find_orphans) and finds the VMs of a node by
NAME (delete_stale_esxi_resources, deploy_virtual_machine). A held VM therefore
gets (1) the annotation prefix "topologyoperator-hold:<project id>" in front of
its original annotation and (2) the name "<name> [hold <id8>]". It matches
neither rule, so a later - even full - deployment of a different project cannot
touch or reuse it.

What is parked lives in a small registry file (holds.json, per deployment,
gitignored, env TOPOLOGYOPERATOR_HOLDS_FILE): per library project id the GNS3
project name, the host it belongs to and one record per VM (original name,
held name, original annotation). Should the file ever be lost, the renamed VMs
still carry the project id in their annotation.

Limits, on purpose not hidden: one project should run at a time (two projects
can use the same VLAN ids on the shared trunk), and the port groups of a held
project are recreated on resume (a full deployment of another project resets
the vSwitch).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from loguru import logger
from src.connections.gns3_connection import GNS3Connection
from src.graph.environment import Environment
from src.settings import Settings

from app import maintenance
from app.gns3_state import resolve_project_id

_HOLDS_FILE_ENV_VAR = "TOPOLOGYOPERATOR_HOLDS_FILE"
_DEFAULT_HOLDS_FILE = "holds.json"
HOLD_PREFIX = "topologyoperator-hold:"


class HoldError(Exception):
    """A hold/resume that cannot (or must not) be done; the message is for the user."""


def _path() -> Path:
    return Path(os.getenv(_HOLDS_FILE_ENV_VAR, _DEFAULT_HOLDS_FILE))


def _read() -> dict[str, dict[str, Any]]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HoldError(f"The hold registry {path.name} is unreadable: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".holds-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def entry(project_id: str) -> dict[str, Any] | None:
    return _read().get(project_id)


def all_entries() -> dict[str, dict[str, Any]]:
    return _read()


def is_held(project_id: str) -> bool:
    found = entry(project_id)
    return bool(found) and found.get("state") == "held"


def held_gns3_names(exclude_project_id: str | None = None) -> dict[str, str]:
    """GNS3 project name -> library project id, for every project on hold (optionally minus one)."""
    return {
        record["name"]: project_id
        for project_id, record in _read().items()
        if project_id != exclude_project_id
    }


def active_hold(project_name: str | None, host: str | None) -> dict[str, Any] | None:
    """The finished hold of the project the active host currently has selected, if any."""
    if not project_name or not host:
        return None
    for project_id, record in _read().items():
        if (
            record.get("state") == "held"
            and record.get("name") == project_name
            and record.get("host") == host
        ):
            return {"id": project_id, **public(record)}
    return None


def public(record: dict[str, Any]) -> dict[str, Any]:
    """What the browser may see: no VM annotations."""
    return {
        "name": record.get("name"),
        "host": record.get("host"),
        "held_at": record.get("held_at"),
        "state": record.get("state"),
        "vm_count": len(record.get("vms", [])),
    }


def _held_name(name: str, project_id: str) -> str:
    return f"{name} [hold {project_id[:8]}]"


def _gns3_base(app: FastAPI) -> str:
    return (
        f"http://{app.state.gns3_vm_ip}:{Settings.GNS3.PORT}"
        f"/v2/projects/{app.state.project_id}"
    )


def hold_project(app: FastAPI, project_id: str, host: str) -> dict[str, Any]:
    """
    Parks the live project of the active host (see the module docstring).
    A hold that stopped half way (state "holding") can simply be started again.
    :raises HoldError: nothing live to hold, or already on hold
    """
    if app.state.graph is None:
        raise HoldError("The topology is not loaded, nothing to hold.")
    existing = entry(project_id)
    if existing is not None and existing.get("state") == "held":
        raise HoldError("This project is already on hold.")
    if existing is None and app.state.project_id is None:
        raise HoldError("There is no live GNS3 project to put on hold.")

    esxi = app.state.esxi_connection
    if existing is None:
        vms = []
        for node in app.state.graph.nodes.values():
            if node.env != Environment.ON_ESXI:
                continue
            vm = esxi.get_vm(node.name)
            if vm is None:
                continue
            vms.append(
                {
                    "name": node.name,
                    "held_name": _held_name(node.name, project_id),
                    "annotation": vm.config.annotation or "",
                }
            )
        record = {
            "name": app.state.project_name,
            "host": host,
            "gns3_project_id": app.state.project_id,
            "held_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "state": "holding",
            "vms": vms,
        }
        _write({**_read(), project_id: record})
    else:
        record = existing

    logger.warning(
        f"Putting project '{record['name']}' on hold "
        f"({len(record['vms'])} ESXi VM(s), GNS3 project is closed)"
    )

    for vm_record in record["vms"]:
        vm = esxi.get_vm(vm_record["name"])
        if vm is not None and esxi.is_vm_powered_on(vm):
            esxi.power_off_vm(vm)

    if app.state.project_id is not None:
        GNS3Connection.post(f"{_gns3_base(app)}/close")

    for vm_record in record["vms"]:
        vm = esxi.get_vm(vm_record["name"])
        if vm is None:
            continue  # already renamed by an earlier, interrupted attempt
        esxi.set_vm_annotation(
            vm, f"{HOLD_PREFIX}{project_id}\n{vm_record['annotation']}"
        )
        esxi.rename_vm(vm, vm_record["held_name"])

    record["state"] = "held"
    _write({**_read(), project_id: record})
    app.state.project_id = None
    app.state.startup_warning = (
        f"Project '{record['name']}' is on hold - resume it to start it again."
    )
    logger.info(f"Project '{record['name']}' is on hold")
    return public(record)


def resume_project(app: FastAPI, project_id: str) -> dict[str, Any]:
    """
    Brings a held project back (see the module docstring) and returns the
    health-check report of the fix pass.
    :raises HoldError: not on hold, not the active host's selected project, a
        held VM missing, or a VM already using an original name
    """
    record = entry(project_id)
    if record is None or record.get("state") != "held":
        raise HoldError("This project is not on hold.")
    if app.state.project_name != record["name"]:
        raise HoldError(
            "Select this project for the active host (Project button on the host "
            "tile) and restart into it before resuming."
        )
    if app.state.graph is None or app.state.gns3_vm_ip is None:
        raise HoldError("The topology or the GNS3 VM is not available right now.")

    esxi = app.state.esxi_connection
    to_rename = []
    for vm_record in record["vms"]:
        held = esxi.get_vm(vm_record["held_name"])
        if held is None:
            if esxi.get_vm(vm_record["name"]) is not None:
                continue  # renamed back by an earlier, interrupted resume
            raise HoldError(
                f"The held VM '{vm_record['held_name']}' no longer exists on the ESXi host."
            )
        if esxi.get_vm(vm_record["name"]) is not None:
            raise HoldError(
                f"A VM named '{vm_record['name']}' already exists - it would clash "
                f"with the held one. Remove or rename it first."
            )
        to_rename.append((held, vm_record))

    gns3_id = resolve_project_id(app.state.gns3_vm_ip, record["name"])
    if gns3_id is None:
        raise HoldError(
            f"The GNS3 project '{record['name']}' no longer exists on the GNS3 VM."
        )

    logger.warning(f"Resuming project '{record['name']}' from hold")
    for held, vm_record in to_rename:
        esxi.rename_vm(held, vm_record["name"])
        esxi.set_vm_annotation(held, vm_record["annotation"])

    GNS3Connection.ensure_project_open(
        app.state.gns3_vm_ip, Settings.GNS3.PORT, gns3_id
    )
    app.state.project_id = gns3_id
    app.state.startup_warning = None

    # Starts nodes and VMs, recreates port groups - same pass as `verify --fix`.
    report = maintenance.run_verify(app, fix=True)

    _write({k: v for k, v in _read().items() if k != project_id})
    logger.info(f"Project '{record['name']}' resumed")
    return report
