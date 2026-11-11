"""Background AI suggestions for a failed health check. This module never pushes."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests
from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel, Field, ValidationError, field_validator
from src.connections.topology_generator_client import (
    TopologyGeneratorClient as BuilderGeneratorClient,
)
from src.graph.graph import Graph
from src.settings import Settings

from app import public_text, push_directives

_MAX_FINISHED_JOBS = 50
_MAX_RUNNING_JOBS = 4
_LOCK = threading.Lock()


class AlreadyRunning(RuntimeError):
    """All suggestion slots are in use."""


class TopologyGeneratorClient(BuilderGeneratorClient):
    """Operator-side extension of the shared topology generator client."""

    @staticmethod
    def suggest_fix(payload: FixContext) -> Any:
        url = Settings.API.TOPOLOGY_GENERATOR_URL.rstrip("/")
        try:
            response = requests.post(
                f"{url}/api/suggest-fix",
                json=payload.model_dump(),
                timeout=Settings.API.TOPOLOGY_GENERATOR_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
            if response.status_code == 404:
                raise RuntimeError(
                    "The configured Topology Generator does not support fix suggestions."
                )
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError(
                    "The Topology Generator did not return a fix suggestion response."
                )
        except requests.Timeout as exc:
            raise TimeoutError("Fix suggestion request timed out.") from exc
        except requests.RequestException as exc:
            raise RuntimeError(
                "Fix suggestion request to the Topology Generator failed."
            ) from exc
        try:
            return response.json()
        except ValueError:
            return None


class FixContext(BaseModel):
    device_role: str
    device_image: str
    context: str
    goal: str


class FixReply(BaseModel, strict=True):
    valid: bool
    warnings: list[str] = Field(default_factory=list, max_length=20)
    commands: list[str] = Field(default_factory=list, max_length=100)
    explanation: str = Field(default="", max_length=4000)

    @field_validator("warnings")
    @classmethod
    def check_warnings(cls, warnings: list[str]) -> list[str]:
        if any(len(warning) > 1000 for warning in warnings):
            raise ValueError("warning is too long")
        return warnings

    @field_validator("commands")
    @classmethod
    def check_commands(cls, commands: list[str]) -> list[str]:
        if any(
            not command.strip()
            or len(command) > 4000
            or not command.isprintable()
            or push_directives.parse(command) is not None
            for command in commands
        ):
            raise ValueError("invalid command or unsupported push directive")
        return commands


@dataclass(frozen=True)
class FixTarget:
    topology_file: str | None
    project_name: str | None
    project_id: str | None
    host: str | None
    device: str
    role: str
    image: str
    links: tuple[tuple[str, str, str], ...]

    def context(self, check: str, goal: str) -> FixContext:
        links = ", ".join(
            f"{local} to {peer}:{port}" for local, peer, port in self.links
        )
        return FixContext(
            device_role=self.role,
            device_image=self.image,
            context=(
                "Propose configuration for this device only. If the check requires "
                "a deployment, hypervisor, wiring or power change rather than device "
                "configuration, return no commands and explain why. "
                "Do not include credentials or reboot directives.\n"
                f"Device: {self.device}\nHealth check: {public_text.redact_paths(check)}\n"
                f"Adjacent links: {links[:4000] or 'none'}"
            ),
            goal=goal.strip(),
        )


def check_devices(graph: Graph, description: str) -> list[str]:
    """Resolve device and link checks; infrastructure-wide failures have no target."""
    for name in graph.nodes:
        if description.startswith((f"GNS3 node '{name}': ", f"ESXi VM '{name}': ")):
            return [name]
    for left, left_port, right, right_port in graph.edges:
        if description.startswith(
            (
                f"{left}:{left_port} <-> {right}:{right_port}: ",
                f"{right}:{right_port} <-> {left}:{left_port}: ",
            )
        ):
            return sorted({left, right})
    return []


def current_target(app: FastAPI, device: str) -> FixTarget | None:
    graph = getattr(app.state, "graph", None)
    node = graph.nodes.get(device) if graph is not None else None
    if node is None:
        return None
    links = []
    for left, left_port, right, right_port in graph.edges:
        if left == device:
            links.append((left_port, right, right_port))
        if right == device:
            links.append((right_port, left, left_port))
    return FixTarget(
        topology_file=Settings.TOPOLOGY_FILE,
        project_name=getattr(app.state, "project_name", None),
        project_id=getattr(app.state, "project_id", None),
        host=getattr(app.state, "gns3_vm_ip", None),
        device=device,
        role=node.role,
        image=node.image,
        links=tuple(sorted(links)),
    )


@dataclass
class FixJob:
    id: str
    target: FixTarget
    max_attempts: int
    attempt: int = 0
    status: str = "running"
    warnings: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    explanation: str = ""
    error: str | None = None


def run_fix_job(job: FixJob, payload: FixContext) -> None:
    try:
        for attempt in range(1, job.max_attempts + 1):
            job.attempt = attempt
            try:
                reply = FixReply.model_validate(
                    TopologyGeneratorClient.suggest_fix(payload)
                )
            except ValidationError:
                job.warnings = ["The generator returned a malformed suggestion."]
                continue
            job.warnings = reply.warnings
            if not reply.valid:
                # A model may correctly decide this check needs an infrastructure fix.
                if reply.explanation.strip() and not reply.commands:
                    job.explanation = reply.explanation.strip()
                    job.status = "done"
                    return
                continue
            if not reply.commands or not reply.explanation.strip():
                job.warnings = [
                    "The generator did not return commands and an explanation."
                ]
                continue
            job.commands = reply.commands
            job.explanation = reply.explanation.strip()
            job.status = "done"
            return
        job.error = f"No usable suggestion after {job.max_attempts} attempt(s)."
    except (TimeoutError, RuntimeError) as exc:
        job.error = str(exc)
    except Exception as exc:
        logger.warning("Fix suggestion job {} failed ({})", job.id, type(exc).__name__)
        job.error = "Fix suggestion failed unexpectedly."
    job.status = "error"


def start_job(app: FastAPI, target: FixTarget, payload: FixContext) -> FixJob:
    with _LOCK:
        jobs = getattr(app.state, "fix_jobs", None)
        if jobs is None:
            app.state.fix_jobs = jobs = {}
        finished = [key for key, job in jobs.items() if job.status != "running"]
        if len(jobs) - len(finished) >= _MAX_RUNNING_JOBS:
            raise AlreadyRunning("Too many fix suggestions are already running.")
        # Reserve room for jobs that may finish while the browser is polling.
        for key in finished[: max(0, len(jobs) - _MAX_FINISHED_JOBS + 1)]:
            del jobs[key]
        job = FixJob(
            str(uuid.uuid4()), target, max(1, Settings.GENERATE_MAX_RETRIES + 1)
        )
        jobs[job.id] = job
        try:
            threading.Thread(
                target=run_fix_job, args=(job, payload), daemon=True
            ).start()
        except RuntimeError:
            del jobs[job.id]
            raise
        return job
