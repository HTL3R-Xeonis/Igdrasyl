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

from app import ova_templates
from app.gns3_state import resolve_project_id

_JOB_LOCK = threading.Lock()


class NotConfigured(Exception):
    """An OVA resource and/or management network has not been selected."""


class AlreadyRunning(Exception):
    """A GNS3 VM deploy job is already in progress for this app."""


@dataclass
class GNS3VMDeployJob:
    id: str
    status: str = "running"  # "running" | "done" | "error"
    stage: str = "starting"
    ip_address: str | None = None
    error: str | None = None


def _find_existing_vm(app: FastAPI):
    """Prefer the configured name, otherwise adopt a unique inventory match."""
    esxi = app.state.esxi_connection
    configured = esxi.get_vm(Settings.ESXI.GNS3_VM_NAME)
    if configured is not None:
        return configured
    vm = esxi.find_gns3_vm()
    if vm is not None:
        logger.info(f"Detected existing GNS3 VM '{vm.name}'")
        Settings.ESXI.GNS3_VM_NAME = vm.name
    return vm


def _detect_management_network(app: FastAPI) -> str | None:
    """Reuse a standard port group on the ESXi management network."""
    network = app.state.esxi_connection.get_host_system().config.network
    names = {
        nic.portgroup
        for nic in network.vnic
        if nic.spec.ip.ipAddress == Settings.ESXI.IP
    }
    uplink_switches = {switch.name for switch in network.vswitch if switch.pnic}
    groups = {
        group.spec.name: (group.spec.vswitchName, group.spec.vlanId)
        for group in network.portgroup
        if group.spec.vswitchName in uplink_switches and group.spec.vlanId < 4095
    }
    matches = names & groups.keys()
    if len(matches) != 1:
        return None
    management = matches.pop()
    if groups.get("VM Network") == groups[management]:
        return "VM Network"
    return management


def _require_configured(
    app: FastAPI, *, detect_management: bool = True
) -> tuple[str, str]:
    template_id = ova_templates.selected_template_id()
    if not template_id:
        templates = ova_templates.list_public()
        if len(templates) == 1:
            template_id = templates[0]["id"]
    management = Settings.ESXI.GNS3_VM_MGMT_NETWORK
    if not management and detect_management:
        management = _detect_management_network(app)

    missing = []
    if not template_id:
        missing.append("GNS3 VM template")
    if not management:
        missing.append("management network")
    if missing:
        fields = " and ".join(missing)
        raise NotConfigured(f"Select the {fields} in host settings.")
    return template_id, management


def auto_deploy_if_missing(app: FastAPI) -> None:
    """
    Deploys the configured GNS3 VM from its OVA, but ONLY when it's
    genuinely absent - no VM by that name exists on the host at all.
    Never fires just because get_vm_ip_address hasn't resolved one yet
    (e.g. a real VM that's simply still booting) - that distinction
    matters because deploy_fresh_gns3_vm powers off and renames-as-
    backup any existing same-named VM unconditionally, and this must
    never risk doing that to a VM that's merely slow, only one that
    isn't there. The startup warning identifies any setting that could
    not be determined unambiguously.
    :raises RuntimeError/ConnectionError/TimeoutError/ValueError: propagated
        from deploy_fresh_gns3_vm on failure - the caller's own try/except
        (already there for the ordinary "VM not found" case) turns this
        into the same non-fatal startup_warning, not a crash.
    """
    if _find_existing_vm(app) is not None:
        return
    template_id, management = _require_configured(app)
    ova_filename = ova_templates.filename_for(template_id)

    logger.warning(
        f"GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}' not found at all - "
        "auto-deploying from the selected OVA template"
    )
    datastore_name = Settings.ESXI.DATASTORE
    if datastore_name is None:
        datastore_name = app.state.esxi_connection.find_biggest_datastore().name
    # Discovery and the catalog lookup can take time. Recheck before importing.
    if _find_existing_vm(app) is not None:
        return
    deploy_fresh_gns3_vm(
        app.state.esxi_connection,
        Settings.ESXI.GNS3_VM_NAME,
        "",
        datastore_name,
        management,
        Settings.ESXI.TRUNK_PORT_GROUP,
        mac_range=Settings.ESXI.GNS3_VM_MAC_RANGE,
        ova_filename=ova_filename,
    )
    logger.info(f"Auto-deployed GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}'")


def run_deploy_job(
    app: FastAPI,
    job: GNS3VMDeployJob,
    delete_old_vm: bool = False,
    *,
    inputs: tuple[str, str] | None = None,
) -> None:
    try:
        template_id, management = (
            inputs if inputs is not None else _require_configured(app)
        )
        datastore_name = Settings.ESXI.DATASTORE
        if datastore_name is None:
            datastore_name = app.state.esxi_connection.find_biggest_datastore().name

        job.stage = "looking up the GNS3 VM template"
        ova_filename = ova_templates.filename_for(template_id)
        # Imported by the OVA deploy service straight from the template share
        # onto the ESXi host: nothing multi-GB passes through this machine.
        ip_address = deploy_fresh_gns3_vm(
            app.state.esxi_connection,
            Settings.ESXI.GNS3_VM_NAME,
            "",
            datastore_name,
            management,
            Settings.ESXI.TRUNK_PORT_GROUP,
            on_stage=lambda stage: setattr(job, "stage", stage),
            delete_old_vm=delete_old_vm,
            mac_range=Settings.ESXI.GNS3_VM_MAC_RANGE,
            ova_filename=ova_filename,
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
    except Exception as exc:
        # Everything, not just the expected failures: this runs in a background
        # thread, and an exception nobody catches (a full disk, a bug) would end
        # the thread silently and leave the job "running" forever - which also
        # blocks every further deploy until the app restarts.
        job.status = "error"
        job.error = str(exc) or type(exc).__name__
        logger.opt(exception=exc).error(f"GNS3 VM deploy job {job.id} failed: {exc}")


def start_job(app: FastAPI, delete_old_vm: bool = False) -> GNS3VMDeployJob:
    """
    :param delete_old_vm: forwarded to deploy_fresh_gns3_vm - permanently
        deletes the existing same-named VM (if any) once the new one has
        confirmed itself alive, instead of leaving it as a powered-off,
        timestamped backup indefinitely. See that function's own
        docstring for why this only ever happens after the new VM is
        proven up, never before.
    :raises NotConfigured: an OVA resource or management network isn't selected
    :raises AlreadyRunning: a job for this app is already in progress
    """
    with _JOB_LOCK:
        existing = getattr(app.state, "gns3_vm_deploy_job", None)
        if existing is not None and existing.status == "running":
            raise AlreadyRunning(f"deploy job {existing.id} is already running")
        esxi = app.state.esxi_connection
        first_deployment = esxi.get_vm(Settings.ESXI.GNS3_VM_NAME) is None
        if first_deployment:
            try:
                candidate = esxi.find_gns3_vm()
            except ValueError as exc:
                raise NotConfigured(
                    "Select the existing GNS3 VM in host settings."
                ) from exc
            if candidate is not None:
                raise NotConfigured("Select the existing GNS3 VM in host settings.")
        inputs = _require_configured(app, detect_management=first_deployment)
        job = GNS3VMDeployJob(id=str(uuid.uuid4()))
        app.state.gns3_vm_deploy_job = job
        thread = threading.Thread(
            target=run_deploy_job,
            args=(app, job, delete_old_vm),
            kwargs={"inputs": inputs},
            daemon=True,
        )
        thread.start()
    return job
