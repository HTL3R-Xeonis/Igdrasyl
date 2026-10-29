"""
Config pushes as background jobs.

A push can be long - up to 10 000 commands per VM, and a guest-operations
command takes about a second - far too long for one HTTP request. The job runs
the push on a thread (same model as app/import_jobs.py) and exposes progress per
device; the browser polls it. Only one push runs at a time: two pushes into the
same device would interleave their commands.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from loguru import logger

from app.config_push import DeviceResult, normalize, push_all, push_all_waiting


class AlreadyRunning(Exception):
    """A config push is already in progress."""


@dataclass
class PushJob:
    id: str
    total_commands: int
    status: str = "running"  # "running" | "done" | "error"
    # device -> {"done": int, "total": int}
    devices: dict[str, dict[str, int]] = field(default_factory=dict)
    results: list[DeviceResult] | None = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)

    def progress(self) -> dict[str, Any]:
        done = sum(d["done"] for d in self.devices.values())
        return {
            "status": self.status,
            "total_commands": self.total_commands,
            "done_commands": done,
            "elapsed_seconds": round(time.monotonic() - self.started, 1),
            "devices": [
                {"device": name, **numbers} for name, numbers in self.devices.items()
            ],
        }


def _run(app: FastAPI, job: PushJob, devices: dict[str, Any], wait: bool) -> None:
    def on_progress(device: str, done: int, total: int) -> None:
        job.devices[device] = {"done": done, "total": total}

    try:
        runner = push_all_waiting if wait else push_all
        job.results = runner(devices, app, on_progress=on_progress)
        job.status = "done"
    except Exception as exc:
        # A thread that dies silently would leave the job "running" forever.
        job.status = "error"
        job.error = str(exc) or type(exc).__name__
        logger.opt(exception=exc).error(f"Config push job {job.id} failed: {exc}")


def start_job(app: FastAPI, devices: dict[str, Any], wait: bool = False) -> PushJob:
    """
    :param wait: retry devices that are still booting (see push_all_waiting) - for the
        automatic push after a resume
    :raises AlreadyRunning: another push is still running
    """
    existing: PushJob | None = getattr(app.state, "push_job", None)
    if existing is not None and existing.status == "running":
        raise AlreadyRunning(f"push job {existing.id} is still running")
    specs = normalize(devices)
    job = PushJob(
        id=uuid.uuid4().hex,
        total_commands=sum(len(spec.commands) for spec in specs.values()),
        devices={
            name: {"done": 0, "total": len(spec.commands)}
            for name, spec in specs.items()
        },
    )
    app.state.push_job = job
    app.state.last_config_push = devices
    threading.Thread(target=_run, args=(app, job, devices, wait), daemon=True).start()
    return job


def get_job(app: FastAPI, job_id: str) -> PushJob | None:
    job: PushJob | None = getattr(app.state, "push_job", None)
    return job if job is not None and job.id == job_id else None
