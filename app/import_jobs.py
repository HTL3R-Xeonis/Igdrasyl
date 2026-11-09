"""
Tracks POST /project/import as a background job instead of one blocking
request - a non-incremental deploy of a handful of devices can easily
take minutes (OVA imports, ESXi VM clones, GNS3 node creation), and the
old synchronous endpoint gave the frontend nothing to show but a static
"may take a while" message for the whole duration.

The percentage this reports is a best-effort approximation, not a real
callback from the deploy itself - TopologyBuilder's own deploy_graph has
no progress hook at all (checked its source before building this: it's
a single, uninterrupted loop with no way to observe how far it's gotten
from outside). Instead, completed/total is computed by independently
polling live ESXi/GNS3 state for how many of the target topology's node
names exist right now - real infrastructure state, not a fabricated
timer, but an approximation: a non-incremental deploy resets the ESXi
vSwitch and can delete+recreate the whole GNS3 project before anything
gets redeployed, so the count can legitimately dip before it climbs.
"""

from __future__ import annotations

import io
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any

import yaml
from fastapi import FastAPI
from loguru import logger
from src.connections.gns3_connection import GNS3Connection
from src.settings import Settings

from app import template_health
from app.gns3_state import resolve_project_id
from app.live_lock import exclusive


@dataclass
class ImportJob:
    id: str
    total: int
    target_names: set[str] = field(default_factory=set)
    completed: int = 0
    status: str = "running"  # "running" | "done" | "error"
    error: str | None = None
    error_kind: str | None = (
        None  # "bad_bundle" | "deploy_failed", mirrors the old 400 vs 502 split
    )
    result: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    # Target names not seen live yet at the last poll - the devices the deploy is still working on.
    pending: list[str] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> int:
        return int(time.time() - self.started_at)

    @property
    def phase(self) -> str:
        """What the deploy is doing now, so a bar that stands still still says why (a Windows image takes minutes)."""
        if self.status != "running":
            return ""
        if self.pending:
            return "creating devices"
        return "finishing (network links, VLAN setup, checks)"


def peek_target_node_names(zip_bytes: bytes) -> set[str]:
    """
    Best-effort: every node name the bundle's topology.yaml declares,
    purely for progress tracking - never raises, since apply_import (run
    inside the job itself, see run_import_job) is what actually
    validates the bundle and reports a real error if it's malformed.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        topology = yaml.safe_load(zf.read("topology.yaml")) or {}
        names: set[str] = set()
        for group in topology.get("nodes", []):
            names.update(group.get("names", []))
        return names
    except Exception:
        return set()


def create_job(app: FastAPI, target_names: set[str]) -> ImportJob:
    job = ImportJob(
        id=str(uuid.uuid4()), total=len(target_names), target_names=target_names
    )
    if not hasattr(app.state, "import_jobs"):
        app.state.import_jobs = {}
    app.state.import_jobs[job.id] = job
    return job


def _apply_import_healing_templates(app: FastAPI, zip_bytes: bytes) -> dict:
    """
    apply_import, repeated once when it was refused with "Image ... not found" because the template service listed too
    little (a collector that hung, a restarted GNS3 VM): the service is asked to read again and, when it recovers, the
    import is tried again. Safe, because that check runs before anything is deleted.
    """
    from app import project_bundle

    try:
        return project_bundle.apply_import(app, zip_bytes)
    except ValueError as exc:
        if not template_health.is_missing_image_error(str(exc)):
            raise
        if not template_health.heal():
            raise
        logger.info("Import: the template service recovered, trying the import again")
        return project_bundle.apply_import(app, zip_bytes)


def run_import_job(app: FastAPI, job: ImportJob, zip_bytes: bytes) -> None:
    """
    The actual deploy, run on a plain background thread (not an asyncio
    task) since apply_import is synchronous, blocking pyvmomi/requests
    calls throughout - same execution model FastAPI's own sync `def`
    endpoints already use via its threadpool for every other endpoint in
    this app, just made explicit here instead of tied to a single
    request's lifetime.
    """
    from app.config_push import push_all_waiting

    started = time.monotonic()
    logger.info(
        f"Import job {job.id} started: {job.total} node(s) in a {len(zip_bytes):,} byte bundle"
    )
    try:
        with exclusive("Project import"):
            result = _apply_import_healing_templates(app, zip_bytes)
        if result["auto_config_push"] and result["configs"]:
            app.state.last_config_push = result["configs"]
            result["push_results"] = push_all_waiting(result["configs"], app)
        job.result = result
        job.completed = job.total
        job.status = "done"
        logger.info(
            f"Import job {job.id} finished in {time.monotonic() - started:.1f}s: "
            f"{job.total} node(s) deployed"
        )
    except ValueError as exc:
        job.status = "error"
        job.error = template_health.explain(str(exc))
        job.error_kind = "bad_bundle"
        logger.warning(f"Import job {job.id} rejected the bundle: {job.error}")
    except (RuntimeError, ConnectionError, TimeoutError) as exc:
        job.status = "error"
        job.error = str(exc)
        job.error_kind = "deploy_failed"
        logger.error(
            f"Import job {job.id} failed after {time.monotonic() - started:.1f}s: "
            f"{type(exc).__name__}: {exc}"
        )


def poll_progress(app: FastAPI, job: ImportJob) -> int:
    """
    Live count of how many of the job's target node names currently
    exist, checked directly against ESXi (by name, independent of any
    GNS3 project state) and the current live GNS3 project (re-resolved
    by name on every poll, since a non-incremental deploy can recreate
    it under a new id mid-run - see gns3_state.resolve_project_id).
    Every name is checked against both, rather than pre-classifying
    which environment each belongs to, since that classification itself
    needs a live template-catalog lookup (see topology_write.py) - an
    extra external dependency this doesn't need just to draw a progress
    bar, and one that's already proven flaky in practice.
    """
    if job.status != "running" or job.total == 0:
        return job.completed

    live_names: set[str] = set()

    esxi = getattr(app.state, "esxi_connection", None)
    if esxi is not None:
        for name in job.target_names:
            try:
                if esxi.get_vm(name) is not None:
                    live_names.add(name)
            except Exception:
                pass

    gns3_vm_ip = getattr(app.state, "gns3_vm_ip", None)
    project_name = getattr(app.state, "project_name", None)
    if gns3_vm_ip and project_name:
        try:
            project_id = resolve_project_id(gns3_vm_ip, project_name)
            if project_id is not None:
                for node in GNS3Connection.list_project_nodes(
                    gns3_vm_ip, Settings.GNS3.PORT, project_id
                ):
                    if node["name"] in job.target_names:
                        live_names.add(node["name"])
        except Exception:
            pass

    job.completed = min(len(live_names), job.total)
    job.pending = sorted(job.target_names - live_names)
    return job.completed


def start_job(app: FastAPI, zip_bytes: bytes) -> ImportJob:
    target_names = peek_target_node_names(zip_bytes)
    job = create_job(app, target_names)
    thread = threading.Thread(
        target=run_import_job, args=(app, job, zip_bytes), daemon=True
    )
    thread.start()
    return job
