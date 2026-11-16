"""
Phase 3 (gateway + per-profile backend): which TCP port each settings
profile's backend process listens on. A separate sidecar JSON file,
not a key inside the profile's own YAML - app/settings_profiles.py's
save_profile() does a verbatim raw-bytes overwrite with no merge, so a
re-uploaded profile would silently wipe a port stored inside it. Same
single-JSON-file-with-mkstemp-swap convention as
app/ai_snippets.py's stored topics and app/project_selection.py.

Only the gateway (app/gateway_main.py, app/backend_manager.py) reads
or writes this - a backend process never needs its own port from
here, since it's told its port directly on its own command line/
environment by whatever started it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

_PORTS_FILE_ENV_VAR = "TOPOLOGYOPERATOR_BACKEND_PORTS_FILE"
_DEFAULT_PORTS_FILE = "settings_profiles_ports.json"
_PORT_RANGE_ENV_VAR = "TOPOLOGYOPERATOR_BACKEND_PORT_RANGE"
_DEFAULT_PORT_RANGE = (9200, 9299)

_lock = threading.Lock()


class NoFreePort(RuntimeError):
    """Every port in the configured range is already assigned to some profile."""


def _store() -> Path:
    return Path(os.getenv(_PORTS_FILE_ENV_VAR, _DEFAULT_PORTS_FILE))


def _port_range() -> tuple[int, int]:
    raw = os.getenv(_PORT_RANGE_ENV_VAR, "")
    if raw and "-" in raw:
        low, _, high = raw.partition("-")
        try:
            return int(low), int(high)
        except ValueError:
            pass
    return _DEFAULT_PORT_RANGE


def _read() -> dict[str, int]:
    path = _store()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return (
        {k: v for k, v in data.items() if isinstance(v, int)}
        if isinstance(data, dict)
        else {}
    )


def _write(data: dict[str, int]) -> None:
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".backend-ports-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def all_ports() -> dict[str, int]:
    """Every profile filename that has ever been assigned a port, and which one."""
    with _lock:
        return _read()


def get_port(filename: str) -> int | None:
    with _lock:
        return _read().get(filename)


def assign_port(filename: str) -> int:
    """
    The profile's port, assigning the next free one in the configured
    range (first call for this profile) or returning the one already
    assigned (every later call - stable across restarts).
    :raises NoFreePort: every port in the range is already taken by
        some other profile.
    """
    with _lock:
        data = _read()
        existing = data.get(filename)
        if existing is not None:
            return existing
        low, high = _port_range()
        taken = set(data.values())
        for candidate in range(low, high + 1):
            if candidate not in taken:
                data[filename] = candidate
                _write(data)
                return candidate
        raise NoFreePort(
            f"no free port for {filename!r} in configured range {low}-{high} "
            f"({len(taken)} already assigned)"
        )


def forget_port(filename: str) -> None:
    """Removes a profile's port assignment (e.g. the profile itself was deleted) - never raises if it had none."""
    with _lock:
        data = _read()
        if filename in data:
            del data[filename]
            _write(data)
