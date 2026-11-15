"""
User accounts for logging into this installation - two roles, "admin" and
"user" (see app/auth.py for the session/login side). A single JSON store,
same convention as app/project_selection.py/app/settings_profiles.py (an
env-var-overridable path, a module-level Lock, atomic mkstemp+os.replace
writes) - a school-class install has few accounts, not enough to need
one-file-per-user.

Passwords are never stored or logged in the clear. hash_password/
verify_password use stdlib hashlib.pbkdf2_hmac (no new dependency - see the
handoff plan for why this project avoids adding passlib/bcrypt for this).
The iteration count travels inside the stored hash string, so it can be
raised later without a migration flag day - verify_password always uses
whatever count a given hash was created with.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from threading import Lock
from typing import Any

_LOCK = Lock()
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16
VALID_ROLES = ("admin", "user")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
MIN_PASSWORD_LENGTH = 8


def _store() -> Path:
    return Path(os.getenv("TOPOLOGYOPERATOR_USERS_FILE", "users.json"))


def _read() -> dict[str, Any]:
    path = _store()
    return json.loads(path.read_text()) if path.exists() else {}


def _write(data: dict[str, Any]) -> None:
    store = _store()
    store.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=store.parent, prefix=".users-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, store)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def _validate_username(username: str) -> str:
    if not _USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Username must be 1-60 characters: letters, digits, '.', '_', '-'"
        )
    return username


def _validate_role(role: str) -> str:
    if role not in VALID_ROLES:
        raise ValueError(f"Role must be one of {VALID_ROLES}")
    return role


def _validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def _public(username: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": username,
        "role": entry["role"],
        "created_at": entry.get("created_at"),
    }


def list_users() -> list[dict[str, Any]]:
    return sorted(
        (_public(name, entry) for name, entry in _read().items()),
        key=lambda item: item["username"].casefold(),
    )


def _admin_count(users: dict[str, Any], *, excluding: str | None = None) -> int:
    return sum(
        1
        for name, entry in users.items()
        if entry["role"] == "admin" and name != excluding
    )


def create_user(username: str, password: str, role: str) -> dict[str, Any]:
    """:raises ValueError: a bad username/password/role, or the username is already taken."""
    _validate_username(username)
    _validate_role(role)
    _validate_password(password)
    with _LOCK:
        users = _read()
        if username in users:
            raise ValueError(f"User {username!r} already exists")
        users[username] = {
            "password_hash": hash_password(password),
            "role": role,
            "created_at": time.time(),
        }
        _write(users)
        return _public(username, users[username])


def set_role(username: str, role: str) -> dict[str, Any]:
    """:raises ValueError: unknown role, or this would leave no admin account. :raises FileNotFoundError: no such user."""
    _validate_role(role)
    with _LOCK:
        users = _read()
        if username not in users:
            raise FileNotFoundError(f"No user named {username!r}")
        if (
            users[username]["role"] == "admin"
            and role != "admin"
            and _admin_count(users, excluding=username) == 0
        ):
            raise ValueError("Cannot remove the last remaining admin account")
        users[username]["role"] = role
        _write(users)
        return _public(username, users[username])


def set_password(username: str, password: str) -> None:
    """:raises ValueError: a bad password. :raises FileNotFoundError: no such user."""
    _validate_password(password)
    with _LOCK:
        users = _read()
        if username not in users:
            raise FileNotFoundError(f"No user named {username!r}")
        users[username]["password_hash"] = hash_password(password)
        _write(users)


def delete_user(username: str) -> None:
    """:raises ValueError: this would leave no admin account. :raises FileNotFoundError: no such user."""
    with _LOCK:
        users = _read()
        if username not in users:
            raise FileNotFoundError(f"No user named {username!r}")
        if (
            users[username]["role"] == "admin"
            and _admin_count(users, excluding=username) == 0
        ):
            raise ValueError("Cannot delete the last remaining admin account")
        del users[username]
        _write(users)


def authenticate(username: str, password: str) -> str | None:
    """Returns the user's role on success, None on a bad username/password - never raises for bad credentials."""
    entry = _read().get(username)
    if entry is None or not verify_password(password, entry["password_hash"]):
        return None
    return entry["role"]


def ensure_bootstrap_admin() -> str | None:
    """
    Creates a first 'admin' account with the default password 'admin' if no
    users exist yet at all - called once from app/startup.py's lifespan.
    Deliberately predictable rather than random: this is meant to be the
    very first login on a fresh install, changed via the admin panel right
    after - not a secret handed out once.
    :return: the password just set ("admin"), to be logged once by the
        caller, or None if accounts already exist and nothing was created.
    """
    with _LOCK:
        if _read():
            return None
        password = "admin"
        users = {
            "admin": {
                "password_hash": hash_password(password),
                "role": "admin",
                "created_at": time.time(),
            }
        }
        _write(users)
        return password
