"""
Named settings.yml profiles TopologyOperator can switch between - each
one describes a different host/environment (ESXi IP, GNS3 VM name,
topology file, ...). Settings is a process-global, load-once class (see
TopologyBuilder's own Settings.initialise_settings and this app's own
lifespan) - there's no runtime-safe way to repoint a running server at
different infrastructure without actually restarting the process, so
"activating" a profile here means re-exec'ing this same process (same
PID, see restart_into_profile) with TOPOLOGYOPERATOR_SETTINGS pointed
at the newly chosen file, not a live in-place config swap.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

_PROFILES_DIR_ENV_VAR = "TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR"
_DEFAULT_PROFILES_DIR = "settings_profiles"
_SETTINGS_ENV_VAR = "TOPOLOGYOPERATOR_SETTINGS"
# Gives the HTTP response that triggered a restart time to actually
# reach the client before os.execve replaces this process image - see
# restart_into_profile's own docstring.
_RESTART_DELAY_SECONDS = 0.75

# Plain basename only - no path separators or ".." traversal, letters/
# digits/dot/underscore/hyphen, ending in .yml or .yaml.
_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.ya?ml$")


class InvalidProfileName(ValueError):
    """Not a safe, plain *.yml/*.yaml basename, or not parseable YAML."""


def _profiles_dir() -> Path:
    path = Path(os.getenv(_PROFILES_DIR_ENV_VAR, _DEFAULT_PROFILES_DIR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_name(filename: str) -> None:
    if not _SAFE_NAME_PATTERN.match(filename):
        raise InvalidProfileName(
            f"{filename!r} isn't a valid profile filename - only letters, "
            f"digits, '.', '_', '-', ending in .yml/.yaml, no path separators"
        )


def _active_settings_name() -> str | None:
    active = os.getenv(_SETTINGS_ENV_VAR)
    return Path(active).name if active else None


def _preview(path: Path) -> dict[str, Any]:
    """Best-effort summary for the picker UI - never raises, a preview isn't validation."""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError) as exc:
        return {"error": f"couldn't read/parse this file: {exc}"}
    esxi = data.get("esxi") or {}
    return {
        "error": None,
        "esxi_ip": esxi.get("ip"),
        "gns3_vm_name": esxi.get("gns3_vm_name"),
    }


def list_profiles() -> list[dict[str, Any]]:
    active_name = _active_settings_name()
    profiles = []
    paths = {
        p.name: p
        for p in sorted(_profiles_dir().glob("*.yml"))
        + sorted(_profiles_dir().glob("*.yaml"))
    }
    active = os.getenv(_SETTINGS_ENV_VAR)
    if active and Path(active).is_file():
        paths[Path(active).name] = Path(active)
    for path in paths.values():
        entry: dict[str, Any] = {
            "filename": path.name,
            "active": path.name == active_name,
        }
        entry.update(_preview(path))
        profiles.append(entry)
    return profiles


def save_profile(filename: str, content: bytes) -> None:
    """:raises InvalidProfileName: unsafe filename, or content that isn't parseable YAML."""
    _validate_name(filename)
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise InvalidProfileName(f"{filename!r} isn't valid YAML: {exc}") from exc
    (_profiles_dir() / filename).write_bytes(content)


def delete_profile(filename: str) -> None:
    """:raises InvalidProfileName: unsafe filename. :raises FileNotFoundError: no such profile."""
    _validate_name(filename)
    path = _profiles_dir() / filename
    if not path.exists():
        raise FileNotFoundError(f"no profile named {filename!r}")
    path.unlink()


def profile_path(filename: str) -> Path:
    """:raises InvalidProfileName: unsafe filename. :raises FileNotFoundError: no such profile."""
    _validate_name(filename)
    active = os.getenv(_SETTINGS_ENV_VAR)
    path = (
        Path(active)
        if active and Path(active).name == filename
        else _profiles_dir() / filename
    )
    if not path.exists():
        raise FileNotFoundError(f"no profile named {filename!r}")
    return path


def _restart_with_settings(settings_path: str) -> None:
    env = os.environ.copy()
    env[_SETTINGS_ENV_VAR] = settings_path
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def restart_into_profile(filename: str) -> None:
    """
    Re-execs this same process (same PID, same uvicorn invocation) with
    TOPOLOGYOPERATOR_SETTINGS pointed at the given profile, on a short
    delay so the HTTP response that triggered this has time to actually
    reach the client first - os.execve replaces the running process
    image immediately, which would otherwise drop the in-flight
    request's own connection before a response ever got sent. Every
    existing WebSocket connection (console, VM screen) drops when the
    restart actually happens - a real moment of downtime during the
    switch, not something recoverable in place.
    :raises InvalidProfileName: unsafe filename. :raises FileNotFoundError: no such profile.
    """
    path = profile_path(filename)
    threading.Timer(
        _RESTART_DELAY_SECONDS, _restart_with_settings, args=(str(path),)
    ).start()
