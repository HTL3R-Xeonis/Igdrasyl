"""
Is the template service - the thing that tells TopologyBuilder which
images exist - actually working? When it isn't, everything that
validates a topology fails with "Image X not found on ESXi or GNS3",
which points at the topology although the real problem is elsewhere.
That is exactly what happened when the ESXi password changed: the
service could no longer log in to ESXi, listed 0 templates, and the
only visible symptom was that misleading message.

check() asks both template APIs how many templates they list and, for
the GNS3 one, which of its GNS3 servers it could reach and why not
(its own /api/servers reports e.g. "incorrect user name or password").
explain() adds the problems found (up to three) to an "Image ... not
found" message. Results are cached briefly - the UI polls this.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests
from loguru import logger
from src.settings import Settings

_CACHE_SECONDS = 20
_TIMEOUT_SECONDS = 5
_NOT_FOUND_MARKER = "not found on ESXi or GNS3"

_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def _reason(exc: Exception) -> str:
    if isinstance(exc, requests.Timeout):
        return f"no answer within {_TIMEOUT_SECONDS} s"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.ConnectionError):
        return "connection refused or host down"
    return type(exc).__name__


def _get_json(url: str) -> Any:
    response = requests.get(url, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _template_count(label: str, base_url: str, problems: list[str]) -> int | None:
    host = urlparse(base_url).netloc or base_url
    try:
        templates = _get_json(f"{base_url.rstrip('/')}/api/templates").get(
            "templates", []
        )
    except (requests.RequestException, ValueError) as exc:
        problems.append(
            f"The {label} template service ({host}) is not reachable: {_reason(exc)}."
        )
        return None
    if not templates:
        problems.append(f"The {label} template service ({host}) lists 0 templates.")
    return len(templates)


def _gns3_server_problems(base_url: str, problems: list[str]) -> None:
    """Which GNS3 servers the template service could not read, and its own stated reason."""
    try:
        servers = _get_json(f"{base_url.rstrip('/')}/api/servers").get("servers", [])
    except (requests.RequestException, ValueError):
        return  # an older service without this endpoint - the counts say enough
    broken = [s for s in servers if s.get("status") != "ok"]
    for server in broken:
        detail = f" - {server['error']}" if server.get("error") else ""
        problems.append(
            f"GNS3 server '{server.get('name', '?')}': {server.get('status')}{detail}"
        )
    if any(str(s.get("status", "")).startswith("esxi_") for s in broken):
        problems.append(
            "The template service cannot log in to ESXi - typically a changed "
            "ESXi password in that service's own configuration."
        )


def _run_check() -> dict[str, Any]:
    if Settings.API.LITERAL_API_VALUES:
        return {
            "ok": True,
            "problems": [],
            "esxi_templates": None,
            "gns3_templates": None,
            "note": "literal template values are configured, the services are not asked",
        }

    problems: list[str] = []
    esxi = _template_count("ESXi", Settings.API.ESXI_TEMPLATE_SERVER_URL, problems)
    gns3 = _template_count("GNS3", Settings.API.GNS3_TEMPLATE_SERVER_URL, problems)
    if gns3 is not None:
        _gns3_server_problems(Settings.API.GNS3_TEMPLATE_SERVER_URL, problems)

    result = {
        "ok": not problems,
        "problems": problems,
        "esxi_templates": esxi,
        "gns3_templates": gns3,
        "note": None,
    }
    if problems:
        logger.warning(f"Template service check found problems: {problems}")
    return result


def check(force: bool = False) -> dict[str, Any]:
    """The template services' state: {ok, problems, esxi_templates, gns3_templates, note}."""
    global _cache
    with _lock:
        now = time.monotonic()
        if not force and _cache is not None and now - _cache[0] < _CACHE_SECONDS:
            return _cache[1]
        result = _run_check()
        _cache = (now, result)
        return result


def explain(message: str) -> str:
    """
    `message` with the template service's problems (up to three) appended,
    if it is an "Image ... not found" error and the service is unhealthy.
    Never raises - a diagnostic must not turn into a second failure.
    """
    if _NOT_FOUND_MARKER not in message:
        return message
    try:
        health = check()
    except Exception as exc:
        logger.debug(f"Template service check failed while explaining an error: {exc}")
        return message
    if health["ok"]:
        return message
    return f"{message} - possible cause: {'; '.join(health['problems'][:3])}"
