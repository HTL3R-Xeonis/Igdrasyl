"""
Per-host ESXi/GNS3 passwords entered in the host settings form.

TopologyBuilder's settings loader refuses a password in a settings file
on purpose (see its Settings.initialise_settings), so these never go into
the profile YAML. They live next to it instead, in
<profiles dir>/.secrets/<profile filename>.json, written with mode 0600
and replaced atomically. The browser can only ever *set* them: reads
report whether one is stored (status), never its value, and nothing here
logs a value. A stored password takes precedence over the ESXI_PASSWORD /
GNS3_PASSWORD environment variables for that host (see apply).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from src.settings import Settings

from app import settings_profiles

SECRET_KEYS = ("esxi_password", "gns3_password")
_MAX_LENGTH = 256


def _secrets_dir() -> Path:
    path = settings_profiles._profiles_dir() / ".secrets"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _secret_path(filename: str) -> Path:
    settings_profiles._validate_name(filename)
    return _secrets_dir() / f"{filename}.json"


def _read(filename: str) -> dict[str, str]:
    try:
        data = json.loads(_secret_path(filename).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in SECRET_KEYS and isinstance(v, str)}


def validate(secrets: dict[str, str]) -> dict[str, str]:
    """
    Drops blank entries (blank means "keep what is stored").
    :raises ValueError: unknown key, or a value that isn't one line of at most 256 characters.
    """
    result = {}
    for key, value in secrets.items():
        if key not in SECRET_KEYS or not isinstance(value, str):
            raise ValueError(f"Unsupported credential field: {key}")
        if not value:
            continue
        if len(value) > _MAX_LENGTH or "\n" in value or "\r" in value:
            raise ValueError(
                f"{key} must be a single line of at most {_MAX_LENGTH} characters"
            )
        result[key] = value
    return result


def status(filename: str) -> dict[str, bool]:
    """Which passwords are stored for this host - never their values."""
    stored = _read(filename)
    return {key: key in stored for key in SECRET_KEYS}


def save(filename: str, secrets: dict[str, str]) -> None:
    """Merges the non-blank values into the host's stored passwords."""
    updates = validate(secrets)
    if not updates:
        return
    merged = {**_read(filename), **updates}
    path = _secret_path(filename)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".secret-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(merged, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def delete(filename: str) -> None:
    _secret_path(filename).unlink(missing_ok=True)


def apply(filename: str) -> list[str]:
    """
    Puts the host's stored passwords into the process-wide Settings (call
    after Settings.initialise_settings).
    :return: which ones were applied, by key - for logging, never the values
    """
    stored = _read(filename)
    if "esxi_password" in stored:
        Settings.ESXI.PASSWORD = stored["esxi_password"]
    if "gns3_password" in stored:
        Settings.GNS3.PASSWORD = stored["gns3_password"]
    return sorted(stored)


def drop_environment_passwords(filename: str, stored: list[str]) -> list[str]:
    """
    A host's passwords come from its stored secrets only. TopologyBuilder's Settings also reads ESXI_PASSWORD/
    GNS3_PASSWORD from the environment, which would silently hand one host's password to another profile - so whatever
    the environment put there is cleared when this host has no stored one.
    :return: the human-readable problems to log (never containing a value)
    """
    problems = []
    if "esxi_password" not in stored:
        Settings.ESXI.PASSWORD = None
        problems.append(
            f"Host profile {filename!r} has no stored ESXi password - set one in the host settings; the ESXI_PASSWORD "
            f"environment variable is no longer used, so connections to this host will fail until then."
        )
    if "gns3_password" not in stored:
        Settings.GNS3.PASSWORD = None
        problems.append(
            f"Host profile {filename!r} has no stored GNS3 password - set one in the host settings; the GNS3_PASSWORD "
            f"environment variable is no longer used."
        )
    return problems
