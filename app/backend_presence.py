"""
Tracks, in memory only, which users have recently been routed through the
gateway to which settings-profile backend - shown on app/routes_gateway.py's
pick-backend page so an admin can see who's using a shared host before
restarting it.

Sessions are stateless signed cookies (app/auth.py) - there is no
server-side session table to query "who is logged in right now" from, so
this is its own small, explicitly ephemeral tracker: nothing here is
persisted, and a gateway restart clears it, which is fine since "who's
here right now" is inherently a right-now fact, not history worth keeping.
"""

from __future__ import annotations

import time
from threading import Lock

DEFAULT_WINDOW_SECONDS = 300.0

_lock = Lock()
_presence: dict[str, dict[str, float]] = {}


def record(filename: str, username: str) -> None:
    with _lock:
        _presence.setdefault(filename, {})[username] = time.monotonic()


def active_users(
    filename: str, within_seconds: float = DEFAULT_WINDOW_SECONDS
) -> list[str]:
    """Usernames seen on this backend within the window, pruning anything older."""
    cutoff = time.monotonic() - within_seconds
    with _lock:
        seen = _presence.get(filename)
        if not seen:
            return []
        for stale in [name for name, last_seen in seen.items() if last_seen < cutoff]:
            del seen[stale]
        return sorted(seen)


def count(filename: str, within_seconds: float = DEFAULT_WINDOW_SECONDS) -> int:
    return len(active_users(filename, within_seconds))
