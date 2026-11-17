"""
Login/logout for the signed-cookie sessions in app/auth.py, and GET /me.
The login page is a plain HTML form, rendered server-side (no JS, no
fetch()) - "no browser popups" is trivially true, and it works even if the
main app's own JS has a problem.

Gateway (app/gateway_main.py): these routes are registered on whichever
FastAPI app calls `register()` below, rather than decorating a fixed
`app` at import time like every other route module - they live on the
gateway exclusively there (see app/main.py's own comment on why it no
longer imports this module, and app/auth_middleware.py for how every
other route on a *single-process* deployment is still gated behind
having a valid session).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import audit_log, users
from app.auth import (
    SESSION_COOKIE,
    create_session_token,
    current_user,
    verify_session_token,
)

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
<link rel="stylesheet" href="/styles.css?v=ginnungagap-58">
<style>
  html, body {{ height: 100%; }}
  body {{
    display: flex; align-items: center; justify-content: center;
    position: relative; overflow: hidden; isolation: isolate;
  }}
  body::before {{
    content: ""; position: fixed; inset: -20px;
    background: url("/login-bg.jpg?v=ginnungagap-58") center center / cover no-repeat;
    filter: blur(3px) brightness(0.62) saturate(1.1);
    transform: scale(1.05);
    z-index: -2;
  }}
  body::after {{
    content: ""; position: fixed; inset: 0;
    background: radial-gradient(ellipse at center, rgba(43,43,43,0.35) 0%, rgba(20,22,20,0.72) 100%);
    z-index: -1;
  }}
  .login-brand {{
    position: fixed; top: 32px; left: 50%; transform: translateX(-50%);
    display: flex; align-items: center; gap: 10px;
    color: #f0f0f0; font-size: 15px; font-weight: 500;
    text-shadow: 0 1px 4px rgba(0,0,0,0.5);
  }}
  .login-brand img {{ width: 26px; height: 26px; }}
  .login-box {{
    display: block; flex: none; width: 340px; padding: 36px 32px; border-radius: 14px;
    background: rgba(51,51,51,0.62);
    border: 1px solid rgba(255,255,255,0.14);
    box-shadow: 0 20px 60px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06);
    backdrop-filter: blur(22px) saturate(1.2);
    -webkit-backdrop-filter: blur(22px) saturate(1.2);
  }}
  .login-box h1 {{
    font-size: 19px; font-weight: 500; margin: 0 0 22px; text-align: center;
    color: #f5f5f5;
  }}
  .login-box label {{
    display: block; margin-bottom: 14px; font-size: 12.5px;
    color: #c8c8c8; letter-spacing: 0.02em;
  }}
  .login-box input[type="text"], .login-box input[type="password"] {{
    width: 100%; margin-top: 6px; box-sizing: border-box;
    background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.14);
    color: #f0f0f0; border-radius: 7px; padding: 9px 11px; font-size: 14px;
    transition: border-color 0.15s ease, background 0.15s ease;
  }}
  .login-box input[type="text"]:focus, .login-box input[type="password"]:focus {{
    outline: none; border-color: #7cb342; background: rgba(0,0,0,0.35);
  }}
  .login-box .remember {{
    display: flex; align-items: center; gap: 7px; font-size: 13px; color: #d4d4d4;
  }}
  .login-box .remember input {{ margin: 0; }}
  .login-box button {{
    width: 100%; margin-top: 18px; padding: 10px; font-size: 14px;
    border-radius: 7px; border: none; cursor: pointer; font-weight: 500;
    background: #7cb342; color: #1c1c1c;
    transition: background 0.15s ease, transform 0.1s ease;
  }}
  .login-box button:hover {{ background: #8fc954; }}
  .login-box button:active {{ transform: scale(0.98); }}
  .login-error {{
    color: #ffb4b4; font-size: 13px; margin: 0 0 16px; text-align: center;
    background: rgba(224,90,90,0.15); border: 1px solid rgba(224,90,90,0.35);
    border-radius: 6px; padding: 8px 10px;
  }}
</style>
</head>
<body>
<div class="login-brand">
  <img src="/ginnungagap-mark.svg" alt="">
  Ginnungagap
</div>
<main class="login-box">
  <h1>Sign in to Ginnungagap</h1>
  {message}
  <form method="post" action="/login">
    <label>Username<input type="text" name="username" autocomplete="username" required autofocus></label>
    <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
    <label class="remember"><input type="checkbox" name="remember" value="1"> Stay logged in</label>
    <button type="submit">Sign in</button>
  </form>
</main>
</body>
</html>"""


def register(app: FastAPI, *, logout_cookies: tuple[str, ...] = ()) -> None:
    """Registers /login (GET+POST), /logout and /me onto the given app."""

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
            audit_log.record("login_failure", username)
            return RedirectResponse("/login?error=1", status_code=303)
        _record_success(username)
        audit_log.record("login_success", username)
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
    def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        user = verify_session_token(token) if token else None
        if user:
            audit_log.record("logout", user[0])
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        for cookie in logout_cookies:
            response.delete_cookie(cookie)
        return response

    @app.get("/me")
    def me(user: Annotated[tuple[str, str], Depends(current_user)]) -> dict[str, str]:
        username, role = user
        return {"username": username, "role": role}
