"""
A second, much smaller, admin-readable log: named security/admin events
only (who logged in, who created/deleted/changed a user, who activated or
restarted a backend, ...). app/request_logging.py's existing DEBUG-level
request log already records every request/response and stays exactly as
it is - that's for debugging; this one exists so "who did what" doesn't
need scrolling through that verbose stream to find it.

Scope is deliberately bounded to events that live on the gateway (or, in
today's not-yet-cut-over Phase 1/2 deployment, the single process) - see
the call sites in app/routes/auth.py, app/routes/admin.py,
app/routes/host_settings.py and app/routes_gateway.py. Backend-side
actions (config push, node/link add/remove, project deploy) are out of
scope for this first pass - auditing those would need a way for a backend
to ship events to wherever this log lives, a separate piece of design
work, not an extension of this.

Reuses app/log_store.py's LineLimitedFile for the same bounded-size-on-
disk rotation the main log already has, rather than inventing a second
one. One JSON object per line: {"ts": <unix time>, "event": ...,
"username": ..., "detail": ...}.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

from loguru import logger

from app import log_store

_lock = Lock()
_sink: log_store.LineLimitedFile | None = None
_sink_path: Path | None = None


def _path() -> Path:
    return Path(os.getenv("TOPOLOGYOPERATOR_AUDIT_LOG_FILE", "audit.log"))


def _sink_for(path: Path) -> log_store.LineLimitedFile:
    global _sink, _sink_path
    with _lock:
        if _sink is None or _sink_path != path:
            if _sink is not None:
                _sink.stop()
            _sink = log_store.LineLimitedFile(path)
            _sink_path = path
        return _sink


def record(event: str, username: str, detail: str = "") -> None:
    """
    Appends one audit event. Never raises - a disk-full/permission problem
    writing the audit log must not be allowed to break the actual action
    it's recording; the failure itself goes to the regular log instead.
    """
    line = json.dumps(
        {"ts": time.time(), "event": event, "username": username, "detail": detail}
    )
    try:
        _sink_for(_path()).write(line + "\n")
    except OSError as exc:
        logger.warning(f"Failed to write audit log event {event!r}: {exc}")


def recent(limit: int = 200) -> list[dict[str, Any]]:
    """The most recent audit events, newest first. A malformed/partial line is skipped, not fatal."""
    lines = log_store.read_lines(_path(), limit)
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
    events.reverse()
    return events
