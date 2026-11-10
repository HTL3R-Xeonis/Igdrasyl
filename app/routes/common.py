"""Pieces every route module needs: the live-call error tuples, the settings env var, two guards."""

from __future__ import annotations

import requests
from fastapi import HTTPException

from app import (
    settings_profiles,
)
from app.application import app

# Widened alongside (RuntimeError, ConnectionError, TimeoutError) on the
# live /nodes and /links endpoints below - a template-service outage
# surfaces as requests.exceptions.RequestException (e.g. ConnectionError,
# which is NOT the builtin ConnectionError), not just the three above.
LIVE_READ_ERRORS = (OSError, RuntimeError, requests.exceptions.RequestException)

LIVE_WRITE_ERRORS = (
    RuntimeError,
    ConnectionError,
    TimeoutError,
    requests.exceptions.RequestException,
)

SETTINGS_ENV_VAR = "TOPOLOGYOPERATOR_SETTINGS"


def require_live_topology() -> None:
    if app.state.graph is None:
        raise HTTPException(503, app.state.startup_warning or "topology unavailable")


def settings_call(function, *args):
    try:
        return function(*args)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (TypeError, ValueError, settings_profiles.InvalidProfileName) as exc:
        raise HTTPException(400, str(exc)) from exc
