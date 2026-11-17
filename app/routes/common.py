"""Shared route guards, live-call errors and settings helpers."""

from __future__ import annotations

import os

import requests
from fastapi import HTTPException
from loguru import logger

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


def has_active_host() -> bool:
    return bool(os.getenv(SETTINGS_ENV_VAR))


def require_active_host(operation: str) -> None:
    if not has_active_host():
        logger.warning(f"Refusing {operation}: no host profile is active.")
        raise HTTPException(
            409, "No host is active - choose one on the landing page first."
        )


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
