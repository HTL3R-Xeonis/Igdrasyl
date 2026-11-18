"""
Gateway + per-profile backend: starting, checking and
restarting a settings profile's own backend process, as a systemd
`--user` unit (`xeonis-backend@<profile-stem>.service`, see
deploy/xeonis-backend@.service) under one dedicated, unprivileged
service account - never sudoers/polkit. A user can always control
their own `--user` units without elevation, since starting/stopping
them never crosses a privilege boundary.

Every function here takes its process-running/inspecting callable as
a parameter (same `Runner`/`Inspect` shape Ginnungagap-Appliance's own
ginnungagap/operate.py uses) so tests never actually touch systemd -
they pass a fake that records what would have been run.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

import requests

from app import backend_ports

Runner = Callable[[list[str]], int]
Inspect = Callable[[list[str]], str]

_UNIT_TEMPLATE = "xeonis-backend@{stem}.service"


def default_runner(args: list[str]) -> int:
    return subprocess.run(args, check=False).returncode


def default_inspect(args: list[str]) -> str:
    try:
        return subprocess.run(
            args, check=False, capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return ""


def _stem(filename: str) -> str:
    # Profile filenames are already restricted (app/settings_profiles.py's
    # _SAFE_NAME_PATTERN) to letters/digits/./_/-, ending .yml/.yaml -
    # exactly the characters a systemd instance name allows unescaped, so
    # no systemd-escape round trip is needed.
    for suffix in (".yml", ".yaml"):
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def unit_name(filename: str) -> str:
    return _UNIT_TEMPLATE.format(stem=_stem(filename))


def is_active(filename: str, *, inspect: Inspect = default_inspect) -> bool:
    output = inspect(["systemctl", "--user", "is-active", unit_name(filename)])
    return output == "active"


def state(filename: str, *, inspect: Inspect = default_inspect) -> str:
    """ "running", "starting" (systemd is bringing the unit up or restarting it) or "stopped" - what a person is told when it is not reachable."""
    output = inspect(["systemctl", "--user", "is-active", unit_name(filename)])
    if output == "active":
        return "running"
    if output in ("activating", "reloading"):
        return "starting"
    return "stopped"


def start(filename: str, *, runner: Runner = default_runner) -> None:
    runner(["systemctl", "--user", "start", unit_name(filename)])


def stop(filename: str, *, runner: Runner = default_runner) -> None:
    runner(["systemctl", "--user", "stop", unit_name(filename)])


def restart(filename: str, *, runner: Runner = default_runner) -> None:
    """
    Explicit admin action, distinct from `ensure_running` below - restarts
    only this one profile's backend, affecting only the users currently on
    it (not a process-wide restart like Phase 1/2's `restart_into_profile`
    used to be). The caller (the admin UI) is responsible for warning about
    any open console/VM-screen WebSocket sessions on that backend before
    calling this - this module has no visibility into those itself.
    """
    runner(["systemctl", "--user", "restart", unit_name(filename)])


def ensure_running(
    filename: str,
    *,
    runner: Runner = default_runner,
    inspect: Inspect = default_inspect,
) -> int:
    """
    Assigns this profile a port (first time only - stable after that) and
    starts its backend unit if it isn't active yet. Returns the port -
    does not wait for the backend's own startup (ESXi/GNS3 resolution) to
    finish; see wait_healthy for that.
    """
    port = backend_ports.assign_port(filename)
    if not is_active(filename, inspect=inspect):
        start(filename, runner=runner)
    return port


def wait_healthy(
    port: int,
    *,
    timeout_seconds: float = 90.0,
    http_get: Callable[..., requests.Response] = requests.get,
) -> bool:
    return (
        wait_for_health(port, timeout_seconds=timeout_seconds, http_get=http_get)
        is not None
    )


def wait_for_health(
    port: int,
    *,
    timeout_seconds: float = 90.0,
    http_get: Callable[..., requests.Response] = requests.get,
) -> requests.Response | None:
    """
    Polls the backend's own GET /health until it answers 200 or the
    timeout elapses. systemd reporting the unit "active (running)" only
    means the process started - not that FastAPI's lifespan (ESXi/GNS3
    resolution, template health checks) has finished, which is what
    actually matters before sending a user to it.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = http_get(f"http://127.0.0.1:{port}/health", timeout=3)
            if response.status_code == 200:
                return response
        except requests.RequestException:
            pass
        time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
    return None
