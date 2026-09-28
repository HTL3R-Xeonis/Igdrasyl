"""
Tracks POST /gns3-vm/deploy (the GUI's manual "replace the GNS3 VM from
its OVA" action) as a background job, same reasoning as
app/import_jobs.py: importing an OVA and waiting for the new VM to boot
can take several minutes, far too long for one blocking request.

Unlike a project import, there's no set of target node names to
live-poll for a percentage - progress here is coarse, named stages
(see src.gns3_vm_deploy.deploy_fresh_gns3_vm's own on_stage callback),
not a 0-100 number.

Also provides auto_deploy_if_missing, called once from main.py's own
lifespan at startup (synchronously, blocking - startup already blocks
on the ESXi connection anyway, and the app can't do anything useful
without the GNS3 VM regardless) - the automatic counterpart to the
manual POST /gns3-vm/deploy action above.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from fastapi import FastAPI
from loguru import logger
from src.gns3_vm_deploy import deploy_fresh_gns3_vm
from src.settings import Settings

from app.gns3_state import resolve_project_id


class NotConfigured(Exception):
    """gns3_vm_ova_path and/or gns3_vm_mgmt_network aren't set - see settings.example.yml."""


class AlreadyRunning(Exception):
    """A GNS3 VM deploy job is already in progress for this app."""


@dataclass
class GNS3VMDeployJob:
    id: str
    status: str = "running"  # "running" | "done" | "error"
    stage: str = "starting"
    ip_address: str | None = None
    error: str | None = None


def _require_configured() -> None:
    if not Settings.ESXI.GNS3_VM_OVA_PATH or not Settings.ESXI.GNS3_VM_MGMT_NETWORK:
        raise NotConfigured(
            "gns3_vm_ova_path and gns3_vm_mgmt_network must both be set in "
            "settings.yml to deploy a GNS3 VM - see settings.example.yml"
        )


def auto_deploy_if_missing(app: FastAPI) -> None:
    """
    Deploys the configured GNS3 VM from its OVA, but ONLY when it's
    genuinely absent - no VM by that name exists on the host at all.
    Never fires just because get_vm_ip_address hasn't resolved one yet
    (e.g. a real VM that's simply still booting) - that distinction
    matters because deploy_fresh_gns3_vm powers off and renames-as-
    backup any existing same-named VM unconditionally, and this must
    never risk doing that to a VM that's merely slow, only one that
    isn't there. A no-op (not an error) when gns3_vm_ova_path/
    gns3_vm_mgmt_network aren't configured - the caller (main.py's own
    lifespan) falls through to its ordinary "not found" warning either
    way, this just sometimes fixes that before it happens.
    :raises RuntimeError/ConnectionError/TimeoutError/ValueError: propagated
        from deploy_fresh_gns3_vm on failure - the caller's own try/except
        (already there for the ordinary "VM not found" case) turns this
        into the same non-fatal startup_warning, not a crash.
    """
    if not Settings.ESXI.GNS3_VM_OVA_PATH or not Settings.ESXI.GNS3_VM_MGMT_NETWORK:
        return
    if app.state.esxi_connection.get_vm(Settings.ESXI.GNS3_VM_NAME) is not None:
        return

    logger.warning(
        f"GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}' not found at all - "
        f"auto-deploying from {Settings.ESXI.GNS3_VM_OVA_PATH}"
    )
    datastore_name = Settings.ESXI.DATASTORE
    if datastore_name is None:
        datastore_name = app.state.esxi_connection.find_biggest_datastore().name
    deploy_fresh_gns3_vm(
        app.state.esxi_connection,
        Settings.ESXI.GNS3_VM_NAME,
        Settings.ESXI.GNS3_VM_OVA_PATH,
        datastore_name,
        Settings.ESXI.GNS3_VM_MGMT_NETWORK,
        Settings.ESXI.TRUNK_PORT_GROUP,
        mac_range=Settings.ESXI.GNS3_VM_MAC_RANGE,
    )
    logger.info(f"Auto-deployed GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}'")


def run_deploy_job(
    app: FastAPI, job: GNS3VMDeployJob, delete_old_vm: bool = False
) -> None:
    try:
        datastore_name = Settings.ESXI.DATASTORE
        if datastore_name is None:
            datastore_name = app.state.esxi_connection.find_biggest_datastore().name

        ip_address = deploy_fresh_gns3_vm(
            app.state.esxi_connection,
            Settings.ESXI.GNS3_VM_NAME,
            Settings.ESXI.GNS3_VM_OVA_PATH,
            datastore_name,
            Settings.ESXI.GNS3_VM_MGMT_NETWORK,
            Settings.ESXI.TRUNK_PORT_GROUP,
            on_stage=lambda stage: setattr(job, "stage", stage),
            delete_old_vm=delete_old_vm,
            mac_range=Settings.ESXI.GNS3_VM_MAC_RANGE,
        )
        job.ip_address = ip_address
        app.state.gns3_vm_ip = ip_address

        # Same staleness fix project_bundle.apply_import already needs
        # after any action that can change what GNS3 project/state is
        # actually live - a fresh GNS3 VM has no project at all yet
        # until something gets deployed to it, so this is best-effort
        # and commonly stays None right after this job finishes.
        try:
            app.state.project_id = resolve_project_id(
                ip_address, app.state.project_name
            )
            if app.state.project_id is not None:
                app.state.startup_warning = None
        except Exception:
            pass

        job.status = "done"
        logger.info(f"GNS3 VM deploy job {job.id} finished: {ip_address}")
    except (RuntimeError, ConnectionError, TimeoutError, ValueError) as exc:
        job.status = "error"
        job.error = str(exc)
        logger.warning(f"GNS3 VM deploy job {job.id} failed: {exc}")


def start_job(app: FastAPI, delete_old_vm: bool = False) -> GNS3VMDeployJob:
    """
    :param delete_old_vm: forwarded to deploy_fresh_gns3_vm - permanently
        deletes the existing same-named VM (if any) once the new one has
        confirmed itself alive, instead of leaving it as a powered-off,
        timestamped backup indefinitely. See that function's own
        docstring for why this only ever happens after the new VM is
        proven up, never before.
    :raises NotConfigured: gns3_vm_ova_path/gns3_vm_mgmt_network aren't set
    :raises AlreadyRunning: a job for this app is already in progress
    """
    _require_configured()
    existing = getattr(app.state, "gns3_vm_deploy_job", None)
    if existing is not None and existing.status == "running":
        raise AlreadyRunning(f"deploy job {existing.id} is already running")

    job = GNS3VMDeployJob(id=str(uuid.uuid4()))
    app.state.gns3_vm_deploy_job = job
    thread = threading.Thread(
        target=run_deploy_job, args=(app, job, delete_old_vm), daemon=True
    )
    thread.start()
    return job
