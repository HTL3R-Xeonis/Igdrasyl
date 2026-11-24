"""
The Operator's side of the "Images" section in the admin panel: how the automated appliance image build is doing.

The image build runs on its own lab VM (the build worker, `image/build/` of the Ginnungagap-Appliance repository). When its optional,
read-only status page is switched on, it answers `GET /status.json` with a small document (schema 1). This module asks for that
document and turns it into one view for the admin panel: the last build, its age, its result, and whether a build is running or
paused. It is a health row, not monitoring: it reads, it shows, it never raises alarms and never changes anything on the worker.

The address comes from the environment (`TOPOLOGYOPERATOR_IMAGE_BUILD_STATUS_URL`, for example `http://<worker>:<port>`); where it is
not set the section says so and offers nothing: the feature is off by default.

Everything read from the worker is treated as untrusted text: only known fields are used, strings are cut to a safe length and
stripped of control characters, the answer is read up to a size limit, redirects are not followed, and a worker that is down, slow
or answering nonsense gives a plain sentence, never an exception or a stack trace.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import requests

ENV_URL = "TOPOLOGYOPERATOR_IMAGE_BUILD_STATUS_URL"
STATUS_PATH = "/status.json"
TIMEOUT_SECONDS = 3.0
MAX_BYTES = 256 * 1024
CACHE_SECONDS = 10.0
MAX_TEXT = 200
MAX_BUILDS = 20
SCHEMA = 1

RESULTS = (
    "published",
    "failed",
    "blocked",
    "deferred",
    "interrupted",
    "stopped",
    "dry-run",
)
BAD_RESULTS = ("failed", "blocked", "interrupted")
STATES = ("building", "paused", "idle")

_NAME = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,79}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


class ConfigError(Exception):
    """The configured address cannot be used."""


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def status_url() -> str | None:
    """The status page address from the environment, or None when the feature is off. Raises ConfigError for a bad value."""
    raw = os.getenv(ENV_URL, "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            "the address must start with http:// or https:// and name a host"
        )
    if parts.username or parts.password:
        raise ConfigError("the address must not contain a user name or password")
    if parts.query or parts.fragment:
        raise ConfigError("the address must not contain a query or a fragment")
    if parts.path not in ("", "/", STATUS_PATH):
        raise ConfigError(f"the address may only point to the host or to {STATUS_PATH}")
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError as exc:
        raise ConfigError("the port is not a number") from exc
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}{port}{STATUS_PATH}"


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL.sub(" ", value).strip()
    return cleaned[:limit] or None


def _name(value: Any) -> str | None:
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def _moment(value: Any) -> tuple[str | None, datetime | None]:
    text = _text(value, 40)
    if not text:
        return None, None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return text, parsed.astimezone(UTC)


def _acceptance(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    passed, total = value.get("passed"), value.get("total")
    for number in (passed, total):
        if isinstance(number, bool) or not isinstance(number, int):
            return None
    if not 0 <= passed <= total <= 1000:
        return None
    return {"passed": passed, "total": total}


def _build(value: Any, now: datetime) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    version = _name(value.get("version"))
    if version is None:
        return None
    result = value.get("result")
    started, _ = _moment(value.get("started_at"))
    finished, finished_at = _moment(value.get("finished_at"))
    age = None  # seconds since the build ended on THIS machine's clock; a time clearly in the future is not shown
    if finished_at is not None:
        seconds = int((now - finished_at).total_seconds())
        age = max(seconds, 0) if seconds >= -60 else None
    return {
        "version": version,
        "result": result if result in RESULTS else "unknown",
        "started_at": started,
        "finished_at": finished,
        "age_seconds": age,
        "template": _name(value.get("template")),
        "reason": _text(value.get("reason")),
        "acceptance": _acceptance(value.get("acceptance")),
    }


def _active(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    started, _ = _moment(value.get("started_at"))
    return {
        "run_id": _name(value.get("run_id")),
        "release": _name(value.get("release")),
        "phase": _text(value.get("phase"), 40),
        "started_at": started,
    }


def normalise(document: Any, now: datetime | None = None) -> dict[str, Any]:
    """The worker's document as the panel's data, or ValueError when it is not a schema-1 status document."""
    now = now or datetime.now(UTC)
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("not a status document of schema 1")
    state = document.get("state")
    builds = [
        b
        for b in (_build(v, now) for v in (document.get("builds") or [])[:MAX_BUILDS])
        if b is not None
    ]
    generated, _ = _moment(document.get("generated_at"))
    return {
        "worker": _text(document.get("worker"), 80),
        "state": state if state in STATES else "idle",
        "paused": document.get("paused") is True,
        "active": _active(document.get("active")),
        "latest": _build(document.get("latest"), now),
        "builds": builds,
        "generated_at": generated,
    }


def _fetch(url: str) -> dict[str, Any]:
    """One request to the worker. Raises RuntimeError with a plain sentence for everything that can go wrong."""
    try:
        with closing(
            requests.get(
                url,
                timeout=TIMEOUT_SECONDS,
                stream=True,
                allow_redirects=False,
                headers={"Accept": "application/json"},
            )
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    f"the status page answered HTTP {response.status_code}"
                )
            body = response.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.Timeout as exc:
        raise RuntimeError("the status page did not answer in time") from exc
    except requests.RequestException as exc:
        raise RuntimeError("the status page cannot be reached") from exc
    if len(body) > MAX_BYTES:
        raise ValueError("the answer is too large")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ValueError("the answer is not JSON") from exc


def _health(view: dict[str, Any]) -> tuple[str, str]:
    latest, state = view["latest"], view["state"]
    if latest is not None and latest["result"] in BAD_RESULTS:
        return (
            "attention",
            f"The last build, {latest['version']}, ended as {latest['result']}.",
        )
    if state == "building":
        active = view["active"] or {}
        release = active.get("release")
        return "info", "A build is running" + (f" for {release}." if release else ".")
    if state == "paused":
        return "info", "Builds are paused."
    if latest is None:
        return "unknown", "No build has run yet."
    if latest["result"] == "published":
        return "ok", f"The last build, {latest['version']}, was published."
    return "info", f"The last build, {latest['version']}, ended as {latest['result']}."


def overview(now: datetime | None = None) -> dict[str, Any]:
    """What the Images section shows. Never raises."""
    global _cache
    now = now or datetime.now(UTC)
    try:
        url = status_url()
    except ConfigError as exc:
        return _empty("invalid", f"{ENV_URL} is not usable: {exc}.", configured=True)
    if url is None:
        return _empty(
            "not_configured",
            "The image build status page is not set up on this installation.",
            configured=False,
        )
    with _lock:
        cached = _cache
    if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
        base = cached[1]
    else:
        try:
            base = {"document": _fetch(url)}
        except RuntimeError as exc:
            base = {"unreachable": str(exc)}
        except ValueError as exc:
            base = {"invalid": str(exc)}
        with _lock:
            _cache = (time.monotonic(), base)
    if "unreachable" in base:
        return _empty("unreachable", base["unreachable"] + ".", configured=True)
    if "invalid" in base:
        return _empty("invalid", base["invalid"] + ".", configured=True)
    try:
        view = normalise(base["document"], now)
    except ValueError as exc:
        return _empty(
            "invalid",
            f"The status page answered with something unexpected ({exc}).",
            configured=True,
        )
    health, message = _health(view)
    return {
        "configured": True,
        "reachable": True,
        "health": health,
        "message": message,
        **view,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _empty(health: str, message: str, *, configured: bool) -> dict[str, Any]:
    return {
        "configured": configured,
        "reachable": False,
        "health": health,
        "message": message,
        "worker": None,
        "state": None,
        "paused": False,
        "active": None,
        "latest": None,
        "builds": [],
        "generated_at": None,
        "checked_at": None,
    }
