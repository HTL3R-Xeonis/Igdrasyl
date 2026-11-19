"""
Signed-cookie sessions for the login in app/users.py/app/routes/auth.py.
Stdlib-only (hmac/secrets/hashlib) - no itsdangerous/Starlette
SessionMiddleware, matching this project's lean dependency list.

A session token is "<username>:<role>:<expiry_unix_ts>" HMAC-SHA256 signed
with a secret generated once and persisted to disk (not an env var - it
must survive a process restart for existing sessions/cookies to keep
working, but nobody ever needs to type or configure it).
"""

from __future__ import annotations

import base64
import hmac
import os
import time
from hashlib import sha256
from pathlib import Path
from threading import Lock

from fastapi import Depends, HTTPException, Request

SESSION_COOKIE = "xeonis_session"
_SESSION_SECONDS = 12 * 60 * 60
_REMEMBER_SECONDS = 30 * 24 * 60 * 60

_lock = Lock()
_cached_secret: bytes | None = None


def _secret_store() -> Path:
    return Path(os.getenv("TOPOLOGYOPERATOR_SESSION_SECRET_FILE", "session_secret.key"))


def _secret() -> bytes:
    global _cached_secret
    if _cached_secret is not None:
        return _cached_secret
    with _lock:
        if _cached_secret is not None:
            return _cached_secret
        path = _secret_store()
        if path.exists():
            _cached_secret = bytes.fromhex(path.read_text().strip())
        else:
            import secrets as _secrets

            generated = _secrets.token_bytes(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(generated.hex())
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            _cached_secret = generated
        return _cached_secret


def _sign(payload: str) -> str:
    digest = hmac.new(_secret(), payload.encode("utf-8"), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_session_token(username: str, role: str, remember: bool = False) -> str:
    seconds = _REMEMBER_SECONDS if remember else _SESSION_SECONDS
    expiry = int(time.time()) + seconds
    payload = f"{username}:{role}:{expiry}"
    return f"{payload}.{_sign(payload)}"


def verify_session_token(token: str) -> tuple[str, str] | None:
    """Returns (username, role) if the token's signature and expiry are valid, else None - never raises."""
    try:
        payload, signature = token.rsplit(".", 1)
        username, role, expiry = payload.split(":")
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    if int(expiry) < time.time():
        return None
    return username, role


def current_user(request: Request) -> tuple[str, str]:
    """FastAPI dependency: the (username, role) of the logged-in caller. :raises HTTPException: 401 if not logged in."""
    token = request.cookies.get(SESSION_COOKIE)
    result = verify_session_token(token) if token else None
    if result is None:
        raise HTTPException(401, "Not logged in")
    return result


def require_admin(user: tuple[str, str] = Depends(current_user)) -> tuple[str, str]:
    """FastAPI dependency: like current_user, but 403s for a non-admin role."""
    if user[1] != "admin":
        raise HTTPException(403, "Admin access required")
    return user
