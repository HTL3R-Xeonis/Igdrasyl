"""
Login/logout for the signed-cookie sessions in app/auth.py, and GET /me.
The login page is a plain HTML form, rendered server-side (no JS, no
fetch()) - "no browser popups" is trivially true, and it works even if the
main app's own JS has a problem. See app/auth_middleware.py for how every
other route is gated behind having a valid session.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Annotated

from fastapi import Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import users
from app.application import app
from app.auth import SESSION_COOKIE, create_session_token, current_user

_failed_attempts: dict[str, tuple[int, float]] = {}
_attempts_lock = threading.Lock()
_MAX_TRACKED_USERNAMES = 1000


def _backoff_seconds(count: int) -> float:
    if count < 3:
        return 0
    if count < 6:
        return 5
    return 30


def _record_failure(username: str) -> None:
    with _attempts_lock:
        if len(_failed_attempts) > _MAX_TRACKED_USERNAMES:
            _failed_attempts.clear()
        count, _ = _failed_attempts.get(username, (0, 0.0))
        _failed_attempts[username] = (count + 1, time.time())


def _record_success(username: str) -> None:
    with _attempts_lock:
        _failed_attempts.pop(username, None)


def _pending_delay(username: str) -> float:
    with _attempts_lock:
        count, _ = _failed_attempts.get(username, (0, 0.0))
    return _backoff_seconds(count)


def _login_page(error: bool) -> str:
    message = (
        '<p class="login-error">Invalid username or password.</p>' if error else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ginnungagap - Sign in</title>
<link rel="icon" type="image/svg+xml" href="/ginnungagap-mark.svg">
<link rel="stylesheet" href="/styles.css?v=ginnungagap-48">
<style>
  body {{ display: flex; align-items: center; justify-content: center; height: 100vh; }}
  .login-box {{ width: 320px; padding: 28px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); }}
  .login-box h1 {{ font-size: 18px; font-weight: 500; margin: 0 0 18px; text-align: center; }}
  .login-box label {{ display: block; margin-bottom: 12px; font-size: 13px; color: var(--dim); }}
  .login-box input[type="text"], .login-box input[type="password"] {{ width: 100%; margin-top: 4px; }}
  .login-box .remember {{ display: flex; align-items: center; gap: 6px; font-size: 13px; }}
  .login-box .remember input {{ margin: 0; }}
  .login-box button {{ width: 100%; margin-top: 8px; }}
  .login-error {{ color: var(--bad); font-size: 13px; margin: 0 0 12px; }}
</style>
</head>
<body>
<main class="login-box">
  <h1>Sign in to Ginnungagap</h1>
  {message}
  <form method="post" action="/login">
    <label>Username<input type="text" name="username" autocomplete="username" required autofocus></label>
    <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
    <label class="remember"><input type="checkbox" name="remember" value="1"> Stay logged in</label>
    <button class="toolbar-btn primary" type="submit">Sign in</button>
  </form>
</main>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(error: Annotated[bool, Query()] = False) -> str:
    return _login_page(error)


@app.post("/login")
async def login_submit(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    remember: Annotated[str, Form()] = "",
) -> Response:
    delay = _pending_delay(username)
    if delay:
        await asyncio.sleep(delay)
    role = users.authenticate(username, password)
    if role is None:
        _record_failure(username)
        return RedirectResponse("/login?error=1", status_code=303)
    _record_success(username)
    token = create_session_token(username, role, remember=bool(remember))
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 60 * 60 if remember else None,
    )
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/me")
def me(user: Annotated[tuple[str, str], Depends(current_user)]) -> dict[str, str]:
    username, role = user
    return {"username": username, "role": role}
