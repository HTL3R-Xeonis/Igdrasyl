"""
Service-to-service endpoint: hands the ESXi login of one known host to the template collector (see
TopologyBuilder/handoff/plans/PLAN_collector_credentials_from_operator.md). The Operator's host profiles stay the only
store of a host's password; nothing here keeps a copy. Everything that could leak (token, password) stays out of logs,
audit events and error texts.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path

import yaml
from loguru import logger

from app import profile_secrets, settings_profiles

TOKEN_ENV = "XEONIS_COLLECTOR_TOKEN"
MIN_TOKEN_LENGTH = 32

DISABLED_TEXT = (
    f"The credentials endpoint is disabled: {TOKEN_ENV} is not set "
    f"(or shorter than {MIN_TOKEN_LENGTH} characters) on the Operator."
)
BAD_TOKEN_TEXT = "Missing or invalid service token."
MISSING_IP_TEXT = "Query parameter 'ip' is required."


def not_found_text(ip: str) -> str:
    return (
        f"No host profile with ESXi address {ip} has a stored ESXi password - "
        f"add the host in the Operator."
    )


def configured_token() -> str | None:
    token = os.getenv(TOKEN_ENV, "").strip()
    return token if len(token) >= MIN_TOKEN_LENGTH else None


def log_status() -> None:
    if configured_token() is None:
        logger.warning(DISABLED_TEXT)
    else:
        logger.info("The collector credentials endpoint is enabled.")


def token_matches(authorization: str | None, token: str) -> bool:
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied.strip():
        return False
    return hmac.compare_digest(supplied.strip().encode(), token.encode())


def _profile_login(path: Path) -> tuple[str, str] | None:
    """(ip, user name) of a readable profile, None otherwise."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    esxi = data.get("esxi") if isinstance(data, dict) else None
    if not isinstance(esxi, dict) or not esxi.get("ip"):
        return None
    return str(esxi["ip"]).strip().lower(), str(esxi.get("username") or "root")


def lookup(ip: str) -> tuple[str, str] | None:
    """(username, password) of the first profile - by file name - with this ESXi address and a stored ESXi password."""
    wanted = ip.strip().lower()
    for entry in settings_profiles.list_profiles():
        if entry.get("error"):
            continue
        login = _profile_login(settings_profiles.profile_path(entry["filename"]))
        if login is None or login[0] != wanted:
            continue
        password = profile_secrets.stored_value(entry["filename"], "esxi_password")
        if password:
            return login[1], password
    return None
