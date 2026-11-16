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

from app.settings_profiles import _SAFE_NAME_PATTERN

_LOCK = Lock()
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16
VALID_ROLES = ("admin", "user", "readonly")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
MIN_PASSWORD_LENGTH = 8
_MAX_ALLOWED_HOSTS = 200


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


def _validate_allowed_hosts(value: Any) -> str | list[str]:
    """
    "ALL" (the default - every host, same as today's behavior) or a list of
    settings-profile filenames. Not checked against the current set of
    profiles - a restriction may be set before that profile exists, or
    outlive one that gets deleted and re-created later.
    """
    if value == "ALL":
        return "ALL"
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError('allowed_hosts must be "ALL" or a list of profile filenames')
    deduplicated = sorted(set(value))
    if len(deduplicated) > _MAX_ALLOWED_HOSTS:
        raise ValueError(
            f"allowed_hosts cannot list more than {_MAX_ALLOWED_HOSTS} hosts"
        )
    for filename in deduplicated:
        if not _SAFE_NAME_PATTERN.match(filename):
            raise ValueError(f"{filename!r} is not a valid settings-profile filename")
    return deduplicated


def _public(username: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": username,
        "role": entry["role"],
        "created_at": entry.get("created_at"),
        "allowed_hosts": entry.get("allowed_hosts", "ALL"),
    }


def get_user(username: str) -> dict[str, Any] | None:
    """The public record for one user (role, allowed_hosts, ...), or None - no password hash."""
    entry = _read().get(username)
    return None if entry is None else _public(username, entry)


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


def create_user(
    username: str, password: str, role: str, allowed_hosts: str | list[str] = "ALL"
) -> dict[str, Any]:
    """:raises ValueError: a bad username/password/role/allowed_hosts, or the username is already taken."""
    _validate_username(username)
    _validate_role(role)
    _validate_password(password)
    allowed_hosts = _validate_allowed_hosts(allowed_hosts)
    with _LOCK:
        users = _read()
        if username in users:
            raise ValueError(f"User {username!r} already exists")
        users[username] = {
            "password_hash": hash_password(password),
            "role": role,
            "created_at": time.time(),
            "allowed_hosts": allowed_hosts,
        }
        _write(users)
        return _public(username, users[username])


def set_allowed_hosts(username: str, allowed_hosts: str | list[str]) -> dict[str, Any]:
    """:raises ValueError: a malformed allowed_hosts value. :raises FileNotFoundError: no such user."""
    allowed_hosts = _validate_allowed_hosts(allowed_hosts)
    with _LOCK:
        users = _read()
        if username not in users:
            raise FileNotFoundError(f"No user named {username!r}")
        users[username]["allowed_hosts"] = allowed_hosts
        _write(users)
        return _public(username, users[username])


def can_access_host(
    user: str | dict[str, Any], filename: str, *, role: str | None = None
) -> bool:
    """
    True if this user may activate/see the given settings-profile filename.
    Admins always can, regardless of their own stored allowed_hosts - "admin"
    already means install-wide access everywhere else in this project.
    `user` is either a username (looked up) or an already-fetched public/raw
    record - whichever the caller already has at hand.
    """
    if isinstance(user, dict):
        if role is None:
            role = user.get("role")
        allowed_hosts = user.get("allowed_hosts", "ALL")
    else:
        entry = _read().get(user)
        if entry is None:
            return False
        if role is None:
            role = entry.get("role")
        allowed_hosts = entry.get("allowed_hosts", "ALL")
    if role == "admin":
        return True
    return allowed_hosts == "ALL" or filename in allowed_hosts


def create_users_bulk(
    text: str, role: str, allowed_hosts: str | list[str] = "ALL"
) -> list[dict[str, Any]]:
    """
    Parses "username;password" lines (one per account, blank lines skipped,
    split on the first ';' only so a password containing ';' still works)
    and creates each with the same shared role/allowed_hosts - best-effort,
    not all-or-nothing: one bad line does not stop the rest of the batch.
    """
    allowed_hosts = _validate_allowed_hosts(allowed_hosts)
    _validate_role(role)
    results: list[dict[str, Any]] = []
    seen_in_batch: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ";" not in line:
            results.append(
                {
                    "username": line,
                    "status": "error",
                    "detail": "Missing ';' between username and password",
                }
            )
            continue
        username, password = line.split(";", 1)
        username = username.strip()
        if username in seen_in_batch:
            results.append(
                {
                    "username": username,
                    "status": "error",
                    "detail": "Duplicate username within this batch",
                }
            )
            continue
        seen_in_batch.add(username)
        try:
            created = create_user(username, password, role, allowed_hosts)
            results.append({"username": username, "status": "created", **created})
        except ValueError as exc:
            results.append(
                {"username": username, "status": "error", "detail": str(exc)}
            )
    return results


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
