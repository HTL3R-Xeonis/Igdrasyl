"""
The Operator's side of the update button in the admin panel (section Updates). Idynoun, the appliance's updater, does the work
on the host; this module only shows what it left and writes one small request for it.

The appliance mounts two folders into this container (the paths come from the environment, see ginnungagap compose.yml):
  * the update state, READ-ONLY (`TOPOLOGYOPERATOR_UPDATE_STATE_DIR`): `installed.json` (what is installed), `available.json` (what
    the last check found), `transaction.json` (exists only while an update runs: its phase) and `web-status.json` (how the last
    request from this panel ended) - all written by the host, never by this app;
  * a request folder (`TOPOLOGYOPERATOR_UPDATE_REQUEST_DIR`), the only place this app may write: ONE `request.json` at a time.
    The host's own systemd path unit sees it, re-validates it as root and runs `ginnungagap update` (a check, or an apply of
    exactly the release the admin confirmed). This app has no Docker socket and no root; it never installs anything itself.

Where the folders are not mounted (every installation that is not the appliance) the section says so and offers nothing: the
feature is off by default, like the update itself.

Everything read from those folders is treated as untrusted text: only known fields are used, strings are cut to a safe length,
files are read without following links and only up to a size limit.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import audit_log

STATE_ENV = "TOPOLOGYOPERATOR_UPDATE_STATE_DIR"
REQUEST_ENV = "TOPOLOGYOPERATOR_UPDATE_REQUEST_DIR"
REQUEST_FILE = "request.json"
MAX_STATE_BYTES = 1_000_000
PICKUP_OVERDUE_SECONDS = 60  # the host's path unit answers within a second; a request that stays this long is not being read
STALE_RUNNING_SECONDS = 600  # "running" with no update journal for this long belongs to a run that did not end
MAX_TEXT = 600
MAX_LOG_LINES = 30

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_USER = re.compile(r"^[A-Za-z0-9._-]{1,60}$")

# The parts of the appliance, by the role key the update manifest uses. The code name is shown next to the role (the
# admin panel is a place for them; the landing page and the connection-lost page are not).
COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("appliance", "Ginnungagap appliance", "tooling and setup wizard"),
    ("operator", "Igdrasyl", "web app (operator)"),
    ("builder", "Mjonyr", "deployment library (builder)"),
    ("template-services", "Hugyn and Munyn", "template services"),
    ("generator", "Mimyr", "topology generator"),
)
_COMPONENT_KEYS = {key: (name, role) for key, name, role in COMPONENTS}

# the phases of an update in the order the engine goes through them (ginnungagap/update.py), with the words the panel uses
PHASES: tuple[tuple[str, str], ...] = (
    ("started", "Checking and downloading"),
    ("downloaded", "Downloaded and verified"),
    ("backed_up", "Backup made"),
    ("unpacked", "Unpacked"),
    ("tooling", "Installing the new tooling"),
    ("switched", "Switched to the new release"),
    ("built", "Images built"),
    ("restarting", "Restarting the services"),
)
_PHASE_WORDS = dict(PHASES)
_PHASE_WORDS["rolling_back"] = "Rolling back"
_PHASE_WORDS["rollback_failed"] = "The rollback failed"


class PanelError(Exception):
    """Something this module refuses; `status` is the HTTP status the route answers with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _folder(name: str) -> Path | None:
    configured = os.getenv(name, "")
    if not configured:
        return None
    path = Path(configured)
    return path if path.is_absolute() and path.is_dir() else None


def state_dir() -> Path | None:
    return _folder(STATE_ENV)


def request_dir() -> Path | None:
    return _folder(REQUEST_ENV)


def unavailable_reason() -> str | None:
    """Why the section can't work here, in words for the admin; None when both folders are mounted."""
    if not os.getenv(STATE_ENV) and not os.getenv(REQUEST_ENV):
        return "Updating from the web is only available on the Ginnungagap appliance."
    if state_dir() is None or request_dir() is None:
        return (
            "The update folders of the appliance are not mounted here. "
            "Run the appliance's update command on the machine, or apply the appliance setup again."
        )
    return None


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def _read_json(path: Path) -> dict[str, Any] | None:
    """A host-written JSON object, or None: missing, a link, not a regular file, too large, not JSON, not an object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            return None
        try:
            data = json.loads(handle.read(MAX_STATE_BYTES + 1))
        except (ValueError, UnicodeDecodeError):
            return None
    return data if isinstance(data, dict) else None


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )
    except ValueError:
        return None


def _version(value: Any) -> str | None:
    return value if isinstance(value, str) and _SEMVER.fullmatch(value) else None


def _component_version(entry: Any) -> str | None:
    version = entry.get("version") if isinstance(entry, dict) else None
    return _text(version, 40)


# ---- what is installed, what a check found ----------------------------------------------------------------------------


def _installed(folder: Path) -> dict[str, Any] | None:
    data = _read_json(folder / "installed.json")
    if data is None or not isinstance(data.get("appliance"), dict):
        return None
    components = (
        data.get("components") if isinstance(data.get("components"), dict) else {}
    )
    adopted = data.get("adopted") is True
    version = _version(data["appliance"].get("version"))
    return {
        "version": None if adopted else version,
        "adopted": adopted,
        "installed_at": _text(data.get("installed_at"), 40),
        "components": {
            key: _component_version(components.get(key)) for key in _COMPONENT_KEYS
        },
    }


def _change(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("component") not in _COMPONENT_KEYS:
        return None
    services = raw.get("services")
    services = (
        [s for s in services if isinstance(s, str) and len(s) < 40]
        if isinstance(services, list)
        else []
    )
    return {
        "component": raw["component"],
        "installed": _text(raw.get("installed"), 40),
        "available": _text(raw.get("available"), 40),
        "change": _text(raw.get("change"), 20) or "-",
        "services": services[:8],
    }


def _check(folder: Path) -> dict[str, Any] | None:
    data = _read_json(folder / "available.json")
    if data is None:
        return None
    available = data.get("available") if isinstance(data.get("available"), dict) else {}
    changes = (
        [c for c in (_change(raw) for raw in data.get("changes") or []) if c]
        if isinstance(data.get("changes"), list)
        else []
    )
    error = _text(data.get("error"))
    return {
        "checked_at": _text(data.get("checked_at"), 40),
        "source": _text(data.get("source"), 200),
        "channel": _text(data.get("channel"), 20),
        "update_available": data.get("update_available") is True and error is None,
        "version": _version(available.get("version")),
        "released": _text(available.get("released"), 40),
        "notes": _text(available.get("notes"), 400),
        "data_migration": available.get("data_migration") is True,
        "signature": _text(data.get("signature"), 120),
        "changes": changes,
        "error": error,
        "stale": False,
    }


def _installed_after(installed: dict[str, Any] | None, check: dict[str, Any]) -> bool:
    """True when the installed release was recorded after the last check (the check is out of date)."""
    installed_at = _parse_time((installed or {}).get("installed_at"))
    checked_at = _parse_time(check.get("checked_at"))
    return (
        installed_at is not None
        and checked_at is not None
        and installed_at > checked_at
    )


def _rows(
    installed: dict[str, Any] | None, check: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """One row per component: installed version, the version of the last check and what an update would restart."""
    by_component = {c["component"]: c for c in (check or {}).get("changes", [])}
    rows = []
    for key, name, role in COMPONENTS:
        change = by_component.get(key)
        have = (installed or {}).get("components", {}).get(key) or (change or {}).get(
            "installed"
        )
        services = (
            change["services"]
            if change and change["change"] not in ("same", "skipped")
            else []
        )
        rows.append(
            {
                "key": key,
                "name": name,
                "role": role,
                "installed": have,
                "available": change["available"] if change else None,
                "change": change["change"] if change else None,
                "services": services,
                "restarts_web_app": "ginnungagap" in services,
            }
        )
    return rows


# ---- what is happening now --------------------------------------------------------------------------------------------


def _progress(folder: Path) -> dict[str, Any] | None:
    data = _read_json(folder / "transaction.json")
    if data is None:
        return None
    phase = data.get("phase") if isinstance(data.get("phase"), str) else "started"
    order = [name for name, _ in PHASES]
    target = data.get("to") if isinstance(data.get("to"), dict) else {}
    target_version = (
        _version((target.get("appliance") or {}).get("version"))
        if isinstance(target.get("appliance"), dict)
        else None
    )
    return {
        "phase": _text(phase, 30),
        "label": _PHASE_WORDS.get(phase, "Working"),
        "step": order.index(phase) + 1 if phase in order else None,
        "steps": len(order),
        "rolling_back": phase in ("rolling_back", "rollback_failed"),
        "failed": phase == "rollback_failed",
        "to": target_version,
        "started": _text(data.get("started"), 40),
        "requested_by": _text(data.get("requester"), 80),
    }


def _result(folder: Path) -> dict[str, Any] | None:
    data = _read_json(folder / "web-status.json")
    if data is None:
        return None
    log = data.get("log") if isinstance(data.get("log"), list) else []
    return {
        "id": _text(data.get("id"), 64),
        "action": data.get("action")
        if data.get("action") in ("check", "apply")
        else None,
        "requested_by": _text(data.get("requested_by"), 80),
        "version": _version(data.get("version")),
        "state": data.get("state")
        if data.get("state") in ("running", "done", "failed", "refused")
        else "failed",
        "outcome": _text(data.get("outcome"), 30),
        "started": _text(data.get("started"), 40),
        "finished": _text(data.get("finished"), 40),
        "message": _text(data.get("message")) or "",
        "log": [t for t in (_text(line, 300) for line in log[-MAX_LOG_LINES:]) if t],
    }


def _pending(requests: Path) -> dict[str, Any] | None:
    """The request this app left that the host has not taken yet, if any."""
    path = requests / REQUEST_FILE
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return {"overdue": time.time() - info.st_mtime > PICKUP_OVERDUE_SECONDS}


def overview() -> dict[str, Any]:
    """Everything the panel shows, as one JSON-able dictionary."""
    reason = unavailable_reason()
    if reason is not None:
        return {"enabled": False, "reason": reason}
    state, requests = state_dir(), request_dir()
    if (
        state is None or requests is None
    ):  # (unavailable_reason() covers this; it keeps the types honest)
        return {"enabled": False, "reason": "The update folders are not mounted."}
    installed, check = _installed(state), _check(state)
    if check is not None and _installed_after(installed, check):
        # an update or a rollback ran after this check: what it found is no longer what is on offer
        check = {**check, "update_available": False, "stale": True, "changes": []}
    progress, result, pending = _progress(state), _result(state), _pending(requests)
    running = result is not None and result["state"] == "running"
    started = _parse_time(result["started"]) if result else None
    if (
        result is not None
        and running
        and progress is None
        and started is not None
        and time.time() - started > STALE_RUNNING_SECONDS
    ):
        result = {
            **result,
            "state": "failed",
            "outcome": "interrupted",
            "message": "The last request did not finish: the machine may have been restarted while it worked.",
        }
        running = False
    busy = bool(pending) or progress is not None or running
    can_apply = bool(
        check and check["update_available"] and check["version"] and not busy
    )
    return {
        "enabled": True,
        "reason": None,
        "release": {
            "version": installed["version"] if installed else None,
            "adopted": installed["adopted"] if installed else False,
            "installed_at": installed["installed_at"] if installed else None,
        },
        "components": _rows(installed, check),
        "check": check,
        "busy": busy,
        "request_pending": pending,
        "progress": progress,
        "result": result,
        "can_apply": can_apply,
    }


# ---- the request --------------------------------------------------------------------------------------------------------


def write_request(action: str, username: str, version: str | None = None) -> str:
    """
    Leaves the one request the host acts on and returns its id. The file appears all at once (written next to it, then
    renamed), so the host's path unit never reads half of it.
    """
    requests = request_dir()
    if requests is None:
        raise PanelError(
            unavailable_reason() or "Updating from the web is not available here.", 503
        )
    if action not in ("check", "apply") or not _USER.fullmatch(username):
        raise PanelError("The request is not valid.")
    body: dict[str, Any] = {
        "id": secrets.token_hex(8),
        "action": action,
        "requested_by": username,
        "role": "admin",
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if action == "apply":
        if version is None or _version(version) is None:
            raise PanelError("An update names the release to install.")
        body.update(version=version, confirmed=True)
    temp = None
    try:
        fd, temp = tempfile.mkstemp(dir=requests, prefix=".request-", suffix=".part")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle)
        os.chmod(temp, 0o644)
        os.replace(temp, requests / REQUEST_FILE)
    except OSError as exc:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass
        raise PanelError(
            "The request could not be left for the appliance. Check the update folder of the appliance.",
            503,
        ) from exc
    return body["id"]


def request_check(username: str) -> str:
    refuse_if_busy()
    request_id = write_request("check", username)
    audit_log.record("update_check_requested", username, f"request {request_id}")
    return request_id


def request_apply(username: str, version: str, confirmed: bool) -> str:
    """Installing is never implied: it needs the version the last check showed and the admin's explicit confirmation."""
    if confirmed is not True:
        raise PanelError("Confirm the update to install it.")
    view = refuse_if_busy()
    check = view["check"]
    if not check or not check["update_available"] or check["version"] != version:
        raise PanelError(
            "The release on screen is not the one the last check found. Check for updates again.",
            409,
        )
    request_id = write_request("apply", username, version)
    audit_log.record(
        "update_apply_requested", username, f"to {version}, request {request_id}"
    )
    return request_id


def refuse_if_busy() -> dict[str, Any]:
    view = overview()
    if not view["enabled"]:
        raise PanelError(view["reason"], 503)
    if view["busy"]:
        raise PanelError(
            "A check or an update is already running. Wait until it has finished.", 409
        )
    return view


# ---- who did it: the result goes into the audit log, once ---------------------------------------------------------------


def _marker() -> Path:
    log = Path(os.getenv("TOPOLOGYOPERATOR_AUDIT_LOG_FILE", "audit.log"))
    return log.with_name("update-result-recorded")


def record_result(view: dict[str, Any]) -> None:
    """
    The request's outcome joins the admin audit log next to who asked for it. Done by whichever admin request first sees the
    finished result (the panel polls while it runs), and only once per request: the id of the last one recorded is kept
    next to the log.
    """
    result = view.get("result") if view.get("enabled") else None
    if not result or result["state"] == "running" or not result["id"]:
        return
    marker = _marker()
    try:
        if marker.read_text(encoding="utf-8").strip() == result["id"]:
            return
    except OSError:
        pass
    who = (
        result["requested_by"]
        if result["requested_by"] and _USER.fullmatch(result["requested_by"])
        else "unknown"
    )
    version = f" {result['version']}" if result["version"] else ""
    audit_log.record(
        "update_result",
        who,
        f"{result['action'] or 'request'}{version}: {result['outcome'] or result['state']} - {result['message']}"[
            :400
        ],
    )
    try:
        marker.write_text(result["id"], encoding="utf-8")
    except OSError:
        pass  # a second line for the same request is better than none
