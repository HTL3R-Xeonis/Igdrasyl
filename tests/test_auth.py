"""Tests app/auth.py - the signed-cookie session tokens, no HTTP involved."""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from app import auth, users


@pytest.fixture(autouse=True)
def _secret_tmp_file(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SESSION_SECRET_FILE", str(tmp_path / "session_secret.key")
    )
    monkeypatch.setattr(auth, "_cached_secret", None)


def test_create_and_verify_round_trip():
    token = auth.create_session_token("alice", "admin")
    assert auth.verify_session_token(token) == ("alice", "admin")


def test_verify_rejects_a_tampered_token():
    token = auth.create_session_token("alice", "user")
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    assert auth.verify_session_token(tampered) is None


def test_verify_rejects_garbage():
    assert auth.verify_session_token("not-a-token") is None
    assert auth.verify_session_token("") is None


def test_verify_rejects_an_expired_token(monkeypatch):
    token = auth.create_session_token("alice", "user")
    future = time.time() + 13 * 60 * 60
    monkeypatch.setattr(time, "time", lambda: future)
    assert auth.verify_session_token(token) is None


def test_remember_extends_expiry_well_past_the_plain_session_window(monkeypatch):
    real_time = time.time()
    plain = auth.create_session_token("alice", "user", remember=False)
    remembered = auth.create_session_token("alice", "user", remember=True)
    monkeypatch.setattr(time, "time", lambda: real_time + 13 * 60 * 60)
    assert auth.verify_session_token(plain) is None
    assert auth.verify_session_token(remembered) == ("alice", "user")


def test_the_secret_is_stable_across_separate_reads(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SESSION_SECRET_FILE", str(tmp_path / "secret.key")
    )
    monkeypatch.setattr(auth, "_cached_secret", None)
    token = auth.create_session_token("alice", "user")
    monkeypatch.setattr(auth, "_cached_secret", None)  # force a fresh read from disk
    assert auth.verify_session_token(token) == ("alice", "user")


class _FakeRequest:
    def __init__(self, cookie: str | None):
        self.cookies = {auth.SESSION_COOKIE: cookie} if cookie else {}


def test_current_user_dependency_401s_without_a_cookie():
    with pytest.raises(HTTPException) as exc_info:
        auth.current_user(_FakeRequest(None))
    assert exc_info.value.status_code == 401


def test_current_user_dependency_returns_the_user_with_a_valid_cookie():
    users.create_user("alice", "a-long-enough-pass", "admin")
    token = auth.create_session_token("alice", "admin")
    assert auth.current_user(_FakeRequest(token)) == ("alice", "admin")


# --- a token only counts for an account that still exists, with the role it has now -----------------------------------


def test_a_token_of_a_deleted_account_is_refused():
    users.create_user("alice", "a-long-enough-pass", "user")
    token = auth.create_session_token("alice", "user")
    assert auth.verify_account(token) == ("alice", "user")

    users.delete_user("alice")

    assert auth.verify_account(token) is None
    with pytest.raises(HTTPException) as exc_info:
        auth.current_user(_FakeRequest(token))
    assert exc_info.value.status_code == 401


def test_the_stored_role_wins_over_the_role_in_the_token():
    users.create_user("alice", "a-long-enough-pass", "user")
    # a token minted while alice was still an admin
    old_admin_token = auth.create_session_token("alice", "admin")

    assert auth.verify_account(old_admin_token) == ("alice", "user")
    with pytest.raises(HTTPException) as exc_info:
        auth.require_admin(auth.current_user(_FakeRequest(old_admin_token)))
    assert exc_info.value.status_code == 403


def test_a_forged_or_unknown_token_is_refused():
    assert auth.verify_account("not-a-token") is None
    assert auth.verify_account(auth.create_session_token("nobody", "admin")) is None


def test_require_admin_403s_for_a_regular_user():
    with pytest.raises(HTTPException) as exc_info:
        auth.require_admin(("alice", "user"))
    assert exc_info.value.status_code == 403


def test_require_admin_passes_through_an_admin():
    assert auth.require_admin(("alice", "admin")) == ("alice", "admin")
