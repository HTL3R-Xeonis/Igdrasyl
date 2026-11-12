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
    credentials_problem = False
    connection_problem = False
    for server in broken:
        error = str(server.get("error") or "")
        detail = f" - {error}" if error else ""
        problems.append(
            f"GNS3 server '{server.get('name', '?')}': {server.get('status')}{detail}"
        )
        if str(server.get("status", "")).startswith("esxi_"):
            # The collector uses esxi_unreachable for both login failures and network errors.
            if any(
                hint in error.casefold()
                for hint in (
                    "incorrect user name or password",
                    "invalidlogin",
                    "no esxi password stored",
                )
            ):
                credentials_problem = True
            else:
                connection_problem = True
    if credentials_problem:
        problems.append(
            "Check the ESXi credentials configured for the GNS3 template service."
        )
    if connection_problem:
        problems.append(
            "Check whether ESXi is reachable from the GNS3 template service."
        )


def _run_check() -> dict[str, Any]:
    if Settings.API.LITERAL_API_VALUES:
        return {
            "ok": True,
            "problems": [],
            "refreshable": False,
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
        # At least one service answered, so asking it to read its sources again can help
        "refreshable": bool(problems) and (esxi is not None or gns3 is not None),
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


def invalidate() -> None:
    """Forget the cached result, so the next check() asks the services again."""
    global _cache
    with _lock:
        _cache = None


def _request_refresh(base_url: str) -> bool | None:
    """True when the service took the request, False when it has no such endpoint or is not set up for it, None when unreachable."""
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/refresh", timeout=_TIMEOUT_SECONDS
        )
    except requests.RequestException:
        return None
    return response.status_code == 202


def request_refresh() -> dict[str, bool | None]:
    """
    Asks both template services to read their sources again right now (POST /api/refresh) instead of at their next timer
    run: the ESXi one re-reads the template share, the GNS3 one asks its GNS3 servers.
    :returns: {"esxi", "gns3"} - True asked, False the service has no such endpoint (an older one) or is not set up for it,
        None not reachable.
    """
    result = {
        "esxi": _request_refresh(Settings.API.ESXI_TEMPLATE_SERVER_URL),
        "gns3": _request_refresh(Settings.API.GNS3_TEMPLATE_SERVER_URL),
    }
    invalidate()
    return result


def is_missing_image_error(message: str) -> bool:
    """True for the 'Image ... not found on ESXi or GNS3' message a topology check raises."""
    return _NOT_FOUND_MARKER in message


# A template service whose collector hangs is freed by the collector's own start timeout (120 s, see the
# collector units), so a wait has to be a bit longer than that to see the recovery.
HEAL_WAIT_SECONDS = 150


def heal(
    wait_seconds: float = HEAL_WAIT_SECONDS,
    interval_seconds: float = 5,
    sleep: Any = time.sleep,
) -> bool:
    """
    Tries to bring a template service that lists too little back: asks both services to read their sources again
    and waits until check() is healthy.
    :returns: True only when the services were unhealthy and are healthy now, i.e. a failed lookup is worth repeating.
        False when they were healthy all along (the missing image really is missing), cannot be asked to refresh
        (not reachable, an older service) or did not recover within `wait_seconds`. Never raises.
    """
    try:
        if check(force=True)["ok"]:
            return False
        if not check()["refreshable"]:
            return False
        logger.warning(
            "Template service lists too little - asking it to read its sources again"
        )
        request_refresh()
        waited = 0.0
        while waited < wait_seconds:
            sleep(interval_seconds)
            waited += interval_seconds
            if check(force=True)["ok"]:
                logger.info(f"Template service recovered after {waited:.0f} s")
                return True
            if waited % 30 == 0:
                request_refresh()  # a run that was busy when asked has not seen the request
        logger.warning(f"Template service did not recover within {wait_seconds:.0f} s")
    except Exception as exc:
        logger.debug(f"Template service heal failed: {exc}")
    return False


def explain(message: str) -> str:
    """
    `message` with the template service's problems (up to three) appended,
    if it is an "Image ... not found" error and the service is unhealthy.
    Never raises - a diagnostic must not turn into a second failure.
    """
    if not is_missing_image_error(message):
        return message
    try:
        health = check()
    except Exception as exc:
        logger.debug(f"Template service check failed while explaining an error: {exc}")
        return message
    if health["ok"]:
        return message
    return f"{message} - possible cause: {'; '.join(health['problems'][:3])}"


def describe(exc: Exception) -> str:
    """
    A readable text for a failed template lookup. When the template service could not be reached,
    its own state (which service, which host, why) replaces the raw connection error, whose URL
    and retry details mean nothing to someone using the GUI. Anything else goes through explain().
    Never raises.
    """
    message = explain(str(exc))
    if not isinstance(exc, (requests.RequestException, TimeoutError)):
        return message
    try:
        problems = check()["problems"]
    except Exception as check_exc:
        logger.debug(
            f"Template service check failed while describing an error: {check_exc}"
        )
        return message
    return " ".join(problems[:3]) if problems else message
