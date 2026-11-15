"""Tests app/users.py - password hashing and the account store, no HTTP involved."""

from __future__ import annotations

import pytest

from app import users


@pytest.fixture(autouse=True)
def _users_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", str(tmp_path / "users.json"))


def test_hash_password_round_trips_and_salts_differently():
    first = users.hash_password("hunter2!!")
    second = users.hash_password("hunter2!!")
    assert first != second  # different random salt each time
    assert users.verify_password("hunter2!!", first)
    assert users.verify_password("hunter2!!", second)
    assert not users.verify_password("wrong", first)


def test_verify_password_rejects_garbage_without_raising():
    assert not users.verify_password("anything", "not-a-real-hash")
    assert not users.verify_password("anything", "")


def test_create_list_and_delete_user():
    users.create_user("alice", "correct-horse", "user")
    users.create_user("bob", "correct-horse", "admin")

    listed = {entry["username"]: entry["role"] for entry in users.list_users()}
    assert listed == {"alice": "user", "bob": "admin"}
    assert "password_hash" not in users.list_users()[0]

    users.delete_user("alice")
    assert [entry["username"] for entry in users.list_users()] == ["bob"]


def test_create_user_rejects_a_duplicate_username():
    users.create_user("alice", "correct-horse", "user")
    with pytest.raises(ValueError, match="already exists"):
        users.create_user("alice", "another-pass", "user")


@pytest.mark.parametrize(
    "username,password,role",
    [
        ("", "correct-horse", "user"),
        ("has space", "correct-horse", "user"),
        ("alice", "short", "user"),
        ("alice", "correct-horse", "superadmin"),
    ],
)
def test_create_user_rejects_bad_input(username, password, role):
    with pytest.raises(ValueError):
        users.create_user(username, password, role)


def test_authenticate_returns_role_on_success_and_none_otherwise():
    users.create_user("alice", "correct-horse", "user")
    assert users.authenticate("alice", "correct-horse") == "user"
    assert users.authenticate("alice", "wrong") is None
    assert users.authenticate("nope", "correct-horse") is None


def test_delete_and_demote_refuse_to_remove_the_last_admin():
    users.create_user("admin1", "correct-horse", "admin")
    with pytest.raises(ValueError, match="last remaining admin"):
        users.delete_user("admin1")
    with pytest.raises(ValueError, match="last remaining admin"):
        users.set_role("admin1", "user")

    users.create_user("admin2", "correct-horse", "admin")
    users.set_role("admin1", "user")  # fine now - admin2 still exists
    assert users.authenticate("admin1", "correct-horse") == "user"


def test_set_role_and_set_password_update_existing_user():
    users.create_user("alice", "correct-horse", "user")
    users.create_user("backup-admin", "correct-horse", "admin")

    updated = users.set_role("alice", "admin")
    assert updated["role"] == "admin"

    users.set_password("alice", "new-password-1")
    assert users.authenticate("alice", "new-password-1") == "admin"
    assert users.authenticate("alice", "correct-horse") is None


def test_set_role_and_delete_404_for_an_unknown_user():
    with pytest.raises(FileNotFoundError):
        users.set_role("ghost", "admin")
    with pytest.raises(FileNotFoundError):
        users.delete_user("ghost")
    with pytest.raises(FileNotFoundError):
        users.set_password("ghost", "whatever123")


def test_ensure_bootstrap_admin_only_creates_once():
    password = users.ensure_bootstrap_admin()
    assert password is not None
    assert len(password) >= 12
    assert users.authenticate("admin", password) == "admin"

    assert users.ensure_bootstrap_admin() is None  # already has accounts
