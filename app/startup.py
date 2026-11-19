"""Startup of the process: resolving the topology and GNS3 project, the retry loop, the lifespan, and the banner state."""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from loguru import logger
from src.connections.esxi_connection import ESXiConnection
from src.settings import Settings

from app import (
    design,
    design_store,
    esxi_session,
    gns3_vm_deploy_jobs,
    gns3_vm_power,
    holds,
    live_lock,
    ova_templates,
    profile_secrets,
    project_selection,
    required_settings,
    settings_profiles,
    shared_state,
    startup_retry,
    template_health,
    users,
)
from app.application import app
from app.gns3_state import (
    resolve_project_id,
)
from app.logging_setup import configure_logging
from app.routes.common import SETTINGS_ENV_VAR
from app.topology import load_graph, resolve_project_name


def refresh_startup_state(app: FastAPI, allow_auto_deploy: bool = True) -> None:
    """
    (Re)computes app.state.graph and the live GNS3 project resolution -
    called once from lifespan at process start, and again from
    POST /reload for a manual retry once a previously-down dependency
    (the template-listing service load_graph() needs, or the GNS3 VM/
    project itself) comes back, without needing a full process restart.

    Never raises: both the topology-file load and the GNS3 resolution
    are independently best-effort here (a template-service outage
    shouldn't hide a GNS3 VM being reachable, or vice versa) - failures
    are collected into app.state.startup_warning instead, same spirit as
    the GNS3-resolution-only try/except this replaces. See /topology,
    /nodes, /layout and GET /project/export, which all now check
    app.state.graph is None and 503 instead of crashing when this
    couldn't load it (previously an unhandled exception here failed the
    whole FastAPI lifespan, refusing to start the process at all).
    """
    warnings: list[str] = []

    # No network dependency (just Settings.GNS3.PROJECT_NAME or the
    # topology file's own path stem) - resolved independently of
    # load_graph() below so a template-service outage there doesn't also
    # block the GNS3 project lookup that only needs this name.
    app.state.project_name = resolve_project_name()

    try:
        app.state.graph = load_graph()
    except Exception as exc:
        app.state.graph = None
        warnings.append(template_health.describe(exc))
        logger.warning(f"Starting without a loaded topology: {exc}")

    app.state.gns3_vm_ip = None
    app.state.project_id = None
    if active_host_filename() is None:
        # No host has been chosen (a fresh machine: no settings file, no activated host profile). The built-in lab address
        # of TopologyBuilder's defaults is not a host anybody configured - it must neither be tried (a warning about a
        # host the person never heard of) nor be the target of the GNS3 VM auto-recovery below.
        logger.info("No host is active yet - not looking for a live GNS3 project")
        app.state.startup_warning = "; ".join(warnings) or None
        return
    try:
        # Auto-recovery: only when the configured GNS3 VM is genuinely
        # absent - never when one exists but just isn't reporting an IP
        # yet (e.g. still booting). See gns3_vm_deploy_jobs's own
        # docstring for the exact rule and why.
        if allow_auto_deploy:
            gns3_vm_deploy_jobs.auto_deploy_if_missing(app)

        gns3_vm_ip = app.state.esxi_connection.get_vm_ip_address(
            Settings.ESXI.GNS3_VM_NAME
        )
        if gns3_vm_ip is None:
            raise RuntimeError(
                f"GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}' not found or has no "
                f"IP reported yet"
            )
        app.state.gns3_vm_ip = gns3_vm_ip

        if holds.active_hold(app.state.project_name, active_host_filename()):
            # Parked on purpose (see app/holds.py): its GNS3 project is closed
            # and nothing is wrong - the banner offers Resume.
            raise RuntimeError(
                f"Project '{app.state.project_name}' is on hold - resume it to "
                f"start it again."
            )
        project_id = (
            resolve_project_id(gns3_vm_ip, app.state.project_name)
            if app.state.project_name
            else None
        )
        if project_id is None and app.state.project_name:
            raise RuntimeError(
                f"No GNS3 project named '{app.state.project_name}' - has "
                f"this topology actually been deployed?"
            )
        app.state.project_id = project_id
    except Exception as exc:
        # Non-fatal: /topology (the static graph) still works without
        # live GNS3 state, so the app comes up in a degraded mode rather
        # than refusing to start entirely. /health surfaces this, and
        # /nodes returns a 503 with the same detail.
        warnings.append(str(exc))
        logger.warning(f"Starting without live GNS3 state: {exc}")

    app.state.startup_warning = "; ".join(warnings) or None


def retry_startup_state(app: FastAPI) -> bool:
    """One background attempt to heal the startup warning; False when the lab was busy with another change."""
    with live_lock.try_exclusive() as got:
        if not got:
            return False
        # No auto-deploy here: replacing the GNS3 VM stays a decision of the startup / the user.
        refresh_startup_state(app, allow_auto_deploy=False)
        return True


BACKEND_ENV_VAR = "TOPOLOGYOPERATOR_GATEWAY_BACKEND"


def check_shared_state_if_managed() -> None:
    """A backend behind the gateway must share the gateway's files (see app/shared_state.py); a standalone one need not."""
    if os.getenv(BACKEND_ENV_VAR) == "1":
        shared_state.require_shared_state_paths()


def warn_if_literal_api_values() -> None:
    """A test switch of TopologyBuilder that hides the template services completely - nothing in production sets it."""
    if Settings.API.LITERAL_API_VALUES:
        logger.warning(
            "LITERAL_API_VALUES is set: the template lists are hardcoded, "
            "the template services are NOT asked."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_shared_state_if_managed()
    settings_path = os.getenv(SETTINGS_ENV_VAR)
    secret_problems: list[str] = []
    if settings_path:
        if not Settings.API.LITERAL_API_VALUES:
            try:
                required_settings.check(settings_path)
            except required_settings.MissingProfileSettings as exc:
                configure_logging()
                logger.error(str(exc))
                raise
        Settings.initialise_settings(settings_path)
        stored_secrets = profile_secrets.apply(Path(settings_path).name)
        secret_problems = profile_secrets.drop_environment_passwords(
            Path(settings_path).name, stored_secrets
        )
    else:
        stored_secrets = []
    ova_templates.initialise(settings_path)
    project_selection.initialise(settings_path)

    # After initialise_settings, so a settings.yml's own log_file_path
    # override (if any) is already in effect - see app/logging_setup.py.
    configure_logging()
    warn_if_literal_api_values()
    for problem in secret_problems:
        logger.error(problem)
    if stored_secrets:
        # Names only - never the values.
        logger.info(f"Using stored credentials for: {', '.join(stored_secrets)}")

    # The gateway owns initial accounts for units started by its launcher.
    # Standalone installs need their own admin on first boot.
    if os.getenv(BACKEND_ENV_VAR) != "1":
        bootstrap_password = users.ensure_bootstrap_admin()
        if bootstrap_password:
            logger.warning(
                f"Created initial admin account - username 'admin', password: "
                f"{bootstrap_password} (the default - change it via the admin "
                f"panel as soon as possible) - this message will not repeat"
            )

    # A real, persistent pyvmomi session, reused for the app's lifetime rather than opened per request (same reasoning
    # as TopologyBuilder's own CLI commands) - but opened on first use, so a host that is down or not set up yet does
    # not keep the app (landing page, design mode) from starting. See app/esxi_session.py.
    esxi_password = Settings.ESXI.PASSWORD
    esxi_args = (
        Settings.ESXI.IP,
        Settings.ESXI.PORT,
        Settings.ESXI.USERNAME,
        esxi_password,
    )
    app.state.esxi_connection = esxi_session.LazyESXiConnection(
        lambda: ESXiConnection(*esxi_args),
        host=Settings.ESXI.IP,
        has_password=bool(esxi_password),
    )

    # Most recent /config-push request body - project_bundle.py's own
    # GET /project/export includes this as the bundle's configs.json,
    # since nothing else persists config-push data anywhere.
    app.state.last_config_push = {}
    # Canvas drawings (rectangles/ellipses/lines/text annotations) -
    # client-side state the frontend pushes here via POST /drawings
    # whenever it changes, so a reload restores them and
    # project_bundle.py's own export can bundle them too. Not synced
    # with GNS3's own real drawings API - see app/static/index.html's
    # own notes on that.
    app.state.last_drawings = []

    # A separate, never-deployed sketch topology (see app/design.py),
    # reachable from the landing page independent of whichever host
    # profile this process happens to be running. Restored from disk
    # (see app/design_store.py) when a prior session saved one, so a
    # restart doesn't silently wipe an in-progress sketch - otherwise
    # cleared only by POST /design/reset. Never touches the template
    # service (see design.from_dict), so this always succeeds even
    # while that service is down.
    try:
        app.state.design = design_store.load() or design.new_state()
    except Exception as exc:
        logger.warning(f"Couldn't restore saved design sketch, starting empty: {exc}")
        app.state.design = design.new_state()
    app.state.design_project_id = None

    # The live-edit staging draft (see app/staging.py) - None whenever no
    # staging session is open. Deliberately not persisted across a
    # restart (unlike app.state.design): it is a draft of the live
    # topology taken at a point in time, so a restart's own topology
    # reload makes any stale draft meaningless anyway.
    app.state.staged_edit = None

    app.state.gns3_vm_deploy_job = None
    refresh_startup_state(app)

    # Whatever was missing at startup (GNS3 VM still booting, template service down) is
    # retried in the background until the warning is gone - nobody has to press Retry.
    stop_retry = threading.Event()
    startup_retry.start(
        lambda: bool(app.state.startup_warning) and held_project() is None,
        lambda: retry_startup_state(app),
        stop_retry,
    )

    yield

    stop_retry.set()


def active_host_filename() -> str | None:
    settings_path = os.getenv(SETTINGS_ENV_VAR)
    return Path(settings_path).name if settings_path else None


def held_project() -> dict[str, Any] | None:
    """The finished hold of the active host's selected project, for the banner's Resume button."""
    try:
        return holds.active_hold(app.state.project_name, active_host_filename())
    except holds.HoldError:
        return None


def deployable_project() -> dict[str, str] | None:
    """
    The library project selected for the active host when it has not been
    deployed to GNS3 yet (the GNS3 VM answers, but has no project of that
    name) - what the warning banner's Deploy button offers to deploy. None
    in every other state.
    """
    settings_path = os.getenv(SETTINGS_ENV_VAR)
    if (
        not settings_path
        or not app.state.project_name
        or app.state.project_id is not None
        or not app.state.gns3_vm_ip
    ):
        return None
    if held_project() is not None:
        return None  # parked, not missing - the banner offers Resume instead
    filename = Path(settings_path).name
    try:
        selection = project_selection.get_selection(filename)
    except (FileNotFoundError, ValueError, settings_profiles.InvalidProfileName):
        return None
    if not selection.get("project_id"):
        return None
    return {
        "id": selection["project_id"],
        "name": selection.get("project_name") or app.state.project_name,
        "filename": filename,
    }


def gns3_vm_power_state() -> dict[str, Any] | None:
    """The GNS3 VM's power state for the banner's Start/Restart button - only asked while something is wrong."""
    if app.state.startup_warning is None and app.state.gns3_vm_ip:
        return None
    return gns3_vm_power.status(app)
