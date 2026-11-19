"""
Tracks POST /design/generate (the landing page's "Generate via LLM"
action) as a background job - the Topology Generator API can take
minutes per attempt (Settings.API.TOPOLOGY_GENERATOR_TIMEOUT_SECONDS
defaults to 1800s) and this retries up to Settings.GENERATE_MAX_RETRIES
times on an invalid result, so a blocking request isn't viable.

Mirrors TopologyBuilder's own CLI `generate` command
(src.cli._generate_topology) almost exactly - same retry-until-valid
loop, same two-stage validation (the API's own "valid" flag, then a
real TopologyFileValidation pass on the returned YAML) - but instead of
writing a topology file to disk, a successful result replaces
app.state.design (see app/design.py) with the generated topology, so
it opens straight into the offline design canvas for review/editing/
export rather than ever touching Settings.TOPOLOGY_FILE or deploying
anything.
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import requests
from fastapi import FastAPI
from loguru import logger
from src.connections.topology_generator_client import TopologyGeneratorClient
from src.settings import Settings
from src.topology_file_validation import TopologyFileValidation

from app import design, design_history, design_store, template_health


@dataclass
class GenerateJob:
    id: str
    status: str = "running"  # "running" | "done" | "error"
    attempt: int = 0
    max_attempts: int = 1
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def _load_generated_topology(
    nodes: list[dict], edges: list[list[str]], screenshot_layout: dict | None = None
) -> design.DesignState:
    """
    Builds a fresh design.DesignState from a validated topology's own
    {nodes, edges} - the same shape TopologyFileValidation.nodes/.edges
    already parse a topology YAML into.
    :raises ValueError: an image TopologyFileValidation accepted doesn't
        resolve to a known environment via Environment.get_environment
        (same underlying template data, so this would only disagree if
        that data changed between the two checks) - propagates as this
        job's own error/retry, same as any other failure below.
    """
    state = design.new_state()
    for group in nodes:
        role = group["role"]
        image = group["image"]
        for name in group["names"]:
            design.add_node(state, name, role, image)
    for node1, if1, node2, if2 in edges:
        design.add_link(state, node1, if1, node2, if2)
    if screenshot_layout is None:
        design.auto_layout(state)
    else:
        design.apply_screenshot_layout(state, screenshot_layout)
    return state


Attachment = tuple[str, bytes]


def _generate_from_attachments(
    prompt: str,
    attachments: list[Attachment],
    ollama_only: bool,
    match_screenshot_layout: bool = False,
) -> dict:
    data = {"prompt": prompt, "ollama_only": str(ollama_only).lower()}
    if match_screenshot_layout:
        data["match_screenshot_layout"] = "true"
    try:
        response = requests.post(
            f"{Settings.API.TOPOLOGY_GENERATOR_URL.rstrip('/')}/api/generate-topology/attachments",
            data=data,
            files=[("files", (name, contents)) for name, contents in attachments],
            timeout=Settings.API.TOPOLOGY_GENERATOR_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            raise RuntimeError(
                "The Topology Generator must be updated to support attachments."
            )
        if response.status_code in (400, 413, 415, 503):
            detail = response.json().get("detail")
            if isinstance(detail, str):
                raise RuntimeError(detail)
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise TimeoutError("Topology generation request timed out.") from exc
    except requests.RequestException as exc:
        raise RuntimeError("Topology generation with attachments failed.") from exc


def run_generate_job(
    app: FastAPI,
    job: GenerateJob,
    prompt: str,
    attachments: list[Attachment] | None = None,
    ollama_only: bool = False,
    match_screenshot_layout: bool = False,
) -> None:
    # The reason the most recent attempt was rejected, so the final error
    # can say WHY instead of only "no valid topology" - e.g. the LLM
    # reusing an interface name, which the user can't see otherwise.
    last_problem: str | None = None
    for attempt in range(1, job.max_attempts + 1):
        job.attempt = attempt
        try:
            result = (
                _generate_from_attachments(
                    prompt, attachments, ollama_only, match_screenshot_layout
                )
                if attachments
                else TopologyGeneratorClient.generate_topology(prompt)
            )
        except (TimeoutError, RuntimeError) as exc:
            job.status = "error"
            job.error = str(exc)
            logger.warning(
                f"Generate job {job.id} attempt {attempt}: request failed: {exc}"
            )
            return

        job.warnings = list(result.get("warnings", []))
        if not result.get("valid"):
            last_problem = (
                "; ".join(job.warnings) or "the generator reported it as invalid"
            )
            logger.warning(
                f"Generate job {job.id} attempt {attempt}: reported invalid, retrying"
            )
            continue

        screenshot_layout = (
            result.get("screenshot_layout") if match_screenshot_layout else None
        )
        if match_screenshot_layout and screenshot_layout is None:
            job.error = "The Topology Generator did not return screenshot positions. Check that it supports this mode."
            job.status = "error"
            return

        fd, tmp_name = tempfile.mkstemp(suffix=".yml", prefix="generated_topology_")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_text(result["yaml"])
            validator = TopologyFileValidation(str(tmp_path))
            validator.validate_file()
            generated = _load_generated_topology(
                validator.nodes, validator.edges, screenshot_layout
            )
            with design_history.step(app):
                app.state.design = generated
            app.state.design_project_id = None
            design_store.save(app.state.design)
            job.status = "done"
            logger.info(f"Generate job {job.id} succeeded on attempt {attempt}")
            return
        except (
            TimeoutError,
            RuntimeError,
            requests.exceptions.RequestException,
        ) as exc:
            # The template service (validate_file/Environment.get_environment
            # both depend on it) is unreachable - retrying won't help until
            # it's back, so stop immediately with a clear error instead of
            # burning through every remaining attempt against a known-down
            # dependency.
            job.status = "error"
            job.error = f"Template service unavailable: {exc}"
            logger.warning(
                f"Generate job {job.id} attempt {attempt}: template service unavailable: {exc}"
            )
            return
        except Exception as exc:
            last_problem = str(exc)
            logger.warning(
                f"Generate job {job.id} attempt {attempt}: "
                f"{type(exc).__name__}: {exc}, retrying"
            )
            continue
        finally:
            tmp_path.unlink(missing_ok=True)

    job.status = "error"
    job.error = f"No valid topology generated after {job.max_attempts} attempt(s)"
    if last_problem:
        job.error += f" - last problem: {template_health.explain(last_problem)}"
    logger.error(f"Generate job {job.id} failed: {job.error}")


_MAX_FINISHED_JOBS = 50


def _prune_finished(jobs: dict[str, GenerateJob]) -> None:
    """
    Caps how many finished jobs app.state.generate_jobs keeps, so a
    long-running process doesn't accumulate one entry per generation
    forever - same reasoning as app.import_jobs._prune_finished. Running
    jobs are never pruned.
    """
    finished = [job_id for job_id, job in jobs.items() if job.status != "running"]
    for job_id in finished[: len(finished) - _MAX_FINISHED_JOBS]:
        del jobs[job_id]


def start_job(
    app: FastAPI,
    prompt: str,
    *,
    attachments: list[Attachment] | None = None,
    ollama_only: bool = False,
    match_screenshot_layout: bool = False,
) -> GenerateJob:
    job = GenerateJob(
        id=str(uuid.uuid4()), max_attempts=Settings.GENERATE_MAX_RETRIES + 1
    )
    if not hasattr(app.state, "generate_jobs"):
        app.state.generate_jobs = {}
    _prune_finished(app.state.generate_jobs)
    app.state.generate_jobs[job.id] = job
    thread = threading.Thread(
        target=run_generate_job,
        args=(app, job, prompt, attachments, ollama_only, match_screenshot_layout),
        daemon=True,
    )
    thread.start()
    return job
