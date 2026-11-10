"""Maintenance: health check, connectivity test, orphans and the GNS3 VM deploy/start."""

from __future__ import annotations

from typing import Any, Literal

import requests
from fastapi import HTTPException
from pydantic import BaseModel

from app import (
    connectivity_test,
    esxi_session,
    gns3_vm_deploy_jobs,
    gns3_vm_power,
    maintenance,
    public_text,
)
from app.application import app
from app.routes.common import (
    LIVE_WRITE_ERRORS,
    require_live_topology,
)


class VerifyRequest(BaseModel):
    fix: bool = False
    fix_vlan_subinterfaces: bool = False


class OrphanItem(BaseModel):
    type: Literal["gns3_node", "esxi_vm"]
    name: str
    reason: str = ""


class DeleteOrphansRequest(BaseModel):
    orphans: list[OrphanItem]


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
    require_live_topology()
    fix = request.fix if request is not None else False
    fix_subinterfaces = request.fix_vlan_subinterfaces if request is not None else False
    try:
        return maintenance.run_verify(app, fix, fix_subinterfaces)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc


class ConnectivityRequest(BaseModel):
    nodes: list[str] | None = None
    count: int = 10
    size: int = 56


@app.post("/maintenance/connectivity-test", status_code=202)
def start_connectivity_test(
    request: ConnectivityRequest | None = None,
) -> dict[str, Any]:
    """
    Puts real traffic on the lab network: the VMs that share a switch (or a direct link) get
    temporary addresses, every one pings every other, the addresses are removed again.
    Runs in the background (see app/connectivity_test.py); poll
    GET /maintenance/connectivity-test/{job_id}.
    """
    require_live_topology()
    request = request or ConnectivityRequest()
    try:
        job = connectivity_test.start_job(
            app, request.nodes, count=request.count, size=request.size
        )
    except connectivity_test.AlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except (connectivity_test.NothingToTest, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": job.id, "segments": job.segments, "skipped": job.skipped}


@app.get("/maintenance/connectivity-test/{job_id}")
def connectivity_test_status(job_id: str) -> dict[str, Any]:
    job = connectivity_test.get_job(app, job_id)
    if job is None:
        raise HTTPException(404, f"no connectivity test {job_id!r}")
    return job.snapshot()


@app.get("/maintenance/orphans")
def maintenance_orphans() -> dict[str, Any]:
    """
    Live GNS3 nodes and tool-created ESXi VMs that no node in the topology
    file describes anymore (what a partially failed live delete leaves
    behind). Read-only.
    """
    require_live_topology()
    try:
        return {"orphans": maintenance.find_orphans(app)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/maintenance/orphans/delete")
def maintenance_delete_orphans(request: DeleteOrphansRequest) -> dict[str, int]:
    """Permanently deletes the given orphans (irreversible)."""
    require_live_topology()
    try:
        deleted = maintenance.delete_orphans(
            app, [orphan.model_dump() for orphan in request.orphans]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LIVE_WRITE_ERRORS as exc:
        raise HTTPException(502, str(exc)) from exc
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
        raise HTTPException(400, str(exc)) from exc
    except gns3_vm_deploy_jobs.AlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job.id}


@app.post("/gns3-vm/start")
def start_gns3_vm() -> dict[str, str]:
    """
    Powers the configured GNS3 VM on (the warning banner's Start button), or resets it when it is on but reports no
    address (Restart). Returns at once; the page then polls POST /reload until the VM has an address.
    """
    try:
        return {"result": gns3_vm_power.start(app)}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        if isinstance(exc, esxi_session.EsxiUnavailable):
            raise
        raise HTTPException(502, f"ESXi could not start the GNS3 VM: {exc}") from exc
    except (OSError, requests.exceptions.RequestException) as exc:
        raise HTTPException(
            502, f"ESXi could not be reached to start the GNS3 VM: {exc}"
        ) from exc


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
